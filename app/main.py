import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app import media, metadata, state, worker


@asynccontextmanager
async def lifespan(app):
    state.init()
    yield


app = FastAPI(title='AudiobookFunnel', lifespan=lifespan)
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.middleware('http')
async def guard(request: Request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': 'Cross-origin changes are not allowed'}, 403)
        if not request.headers.get('content-type', '').startswith('application/json'):
            return JSONResponse({'detail': 'Use application/json'}, 415)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': str(exc)}, 400)


@app.exception_handler(ValidationError)
async def validation_error(request, exc):
    return JSONResponse({'detail': '; '.join(e['msg'] for e in exc.errors(include_input=False))}, 400)


@app.exception_handler(KeyError)
async def missing(request, exc):
    return JSONResponse({'detail': str(exc)}, 404)


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/health')
def health():
    with state.db() as c:
        runtime = dict(c.execute('SELECT key,value FROM runtime').fetchall())
    return {'status': 'ok', 'runtime': runtime}


@app.get('/api/settings')
def settings():
    return {'values': state.public_settings(), 'roots': {k: str(v) for k, v in state.ROOTS.items()}}


@app.put('/api/settings')
def save_settings(data: dict):
    old = state.settings().model_dump()
    for key in state.SECRET_FIELDS:
        if data.get(key) == '':
            data.pop(key)
    old.update({k: v for k, v in data.items() if k in state.Settings.model_fields})
    validated = state.Settings(**old)
    with state.db() as c:
        c.execute('UPDATE settings SET body=? WHERE id=1', (validated.model_dump_json(),))
    return {'saved': True}


@app.get('/api/jobs')
def jobs():
    with state.db() as c:
        return {'jobs': [state.unpack(r) for r in c.execute('SELECT * FROM jobs ORDER BY updated DESC')],
                'packages': [dict(r) for r in c.execute('SELECT * FROM packages ORDER BY created DESC')],
                'scans': [dict(r) for r in c.execute('SELECT * FROM scan_requests ORDER BY id DESC LIMIT 10')]}


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    obj = state.job(job_id)
    obj['body']['candidates'] = metadata.ranked(obj['body']['embedded'], obj['body']['candidates'])
    obj['automation'] = metadata.automation_decision(obj['body']['embedded'], obj['body']['candidates'], state.settings(), obj['body']['grouping_confirmed'])
    with state.db() as c:
        obj['events'] = [dict(r) for r in c.execute('SELECT * FROM events WHERE job_id=? ORDER BY id', (job_id,))]
    return obj


@app.post('/api/discover')
def discover(data: dict):
    worker.discover(state.settings())
    return {'queued': True}


@app.post('/api/packages/{package_id}/retry')
def retry_package(package_id: str, data: dict):
    with state.db() as c:
        c.execute("UPDATE packages SET status='PENDING',error=NULL WHERE id=? AND status='ERROR'", (package_id,))
    return {'queued': True}


def editable(job):
    if job['status'] not in ('REVIEW', 'ERROR'):
        raise HTTPException(409, 'Only review/error jobs can be edited. Return a queued job to review first.')


def edit_transaction(job_id, change):
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = state.unpack(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        editable(job)
        # An existing published output must be recovered, not retagged in place.
        plan = job['body'].get('publication')
        manifest = Path(plan['library_path']) / plan['relative'] / 'funnel.json' if plan else None
        if manifest and manifest.is_file():
            try:
                published = json.loads(manifest.read_text('utf-8')).get('job_id') == job_id
            except (ValueError, OSError):
                published = False
            if published:
                raise HTTPException(409, 'Output already published; retry recovery to restore its completed state')
        job['body'].pop('publication', None)
        change(job['body'])
        c.execute("UPDATE jobs SET body=?,status='REVIEW',error=NULL,updated=? WHERE id=?", (json.dumps(job['body']), time.time(), job_id))
        state.event(c, job_id, 'Review updated')
    return state.job(job_id)


class Edit(BaseModel):
    metadata: dict[str, str] = Field(default_factory=dict)
    cover_choice: str = 'embedded'
    grouping_confirmed: bool = False


@app.put('/api/jobs/{job_id}')
def edit(job_id: str, data: Edit):
    def change(body):
        for key, value in data.metadata.items():
            if key in metadata.FIELDS and body['metadata'].get(key, '') != value:
                body['metadata'][key] = value
                body['provenance'][key] = 'Manual'
        if data.cover_choice not in ('embedded', 'provider', 'none'):
            if not data.cover_choice.startswith('local:') or not 0 <= int(data.cover_choice[6:]) < len(body['covers']):
                raise ValueError('Unknown cover selection')
        body['cover_choice'] = data.cover_choice
        body['grouping_confirmed'] = data.grouping_confirmed
    return edit_transaction(job_id, change)


class Search(BaseModel):
    provider: str = 'Audible'
    query: str = ''
    author: str = ''
    asin: str = ''


@app.post('/api/jobs/{job_id}/search')
def search(job_id: str, data: Search):
    job = state.job(job_id)
    editable(job)
    try:
        settings = state.settings()
        candidates = metadata.ranked(job['body']['embedded'], metadata.search(data.provider, data.query, data.author, settings.audible_region, data.asin, settings.google_books_api_key))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = 'Provider quota/rate limit reached. Try later or use another source.' if status == 429 else f'Provider returned HTTP {status}. Try another region/source or manual editing.'
        if status == 429 and data.provider == 'Google Books':
            detail += ' You can supply a Google Books API key in Settings.'
        raise HTTPException(502, detail) from exc
    except ValueError:
        raise
    except Exception as exc:
        raise HTTPException(502, f'Provider lookup failed ({type(exc).__name__}). Try ASIN, another region/source, or manual editing.') from exc
    def change(body):
        body['candidates'] = candidates
        body['lookup_error'] = ''
    return edit_transaction(job_id, change)


class Selection(BaseModel):
    index: int = Field(ge=0)


@app.post('/api/jobs/{job_id}/select')
def select(job_id: str, data: Selection):
    def change(body):
        body['candidates'] = metadata.ranked(body['embedded'], body['candidates'])
        if data.index >= len(body['candidates']):
            raise ValueError('Candidate no longer exists; search again')
        candidate = body['candidates'][data.index]
        for key in metadata.FIELDS:
            if candidate.get(key):
                body['metadata'][key] = str(candidate[key])
                body['provenance'][key] = candidate['provider']
        if candidate.get('cover_url'):
            body['cover_choice'] = 'provider'
    return edit_transaction(job_id, change)


@app.post('/api/jobs/{job_id}/auto-match')
def auto_match(job_id: str, data: dict):
    settings = state.settings()
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = state.unpack(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        if job['status'] != 'REVIEW':
            raise HTTPException(409, 'Only review jobs can be matched automatically')
        body = job['body']
        body['candidates'] = metadata.ranked(body['embedded'], body['candidates'])
        decision = metadata.automation_decision(body['embedded'], body['candidates'], settings, body['grouping_confirmed'])
        if not decision['eligible']:
            raise HTTPException(409, '; '.join(decision['reasons']))
        for file in body['files']:
            source = media.contained(file['path'], settings.source_path)
            if not source.is_file():
                raise HTTPException(409, 'Source file is missing; restore it or dismiss the stale error')
        # Preserve deliberate edits; this action fills untouched fields only.
        best = body['candidates'][0]
        for field in metadata.FIELDS:
            if best.get(field) and body['provenance'].get(field) != 'Manual':
                body['metadata'][field] = str(best[field])
                body['provenance'][field] = best['provider']
        if best.get('cover_url') and body.get('cover_choice') == 'embedded':
            body['cover_choice'] = 'provider'
        media.names(body['metadata'], settings)
        body['force_now'] = False
        c.execute("UPDATE jobs SET status='READY',body=?,error=NULL,updated=? WHERE id=?", (json.dumps(body), time.time(), job_id))
        state.event(c, job_id, 'User requested automatic match recheck: approved with current scoring rules')
    return {'queued': True}


@app.post('/api/jobs/{job_id}/dismiss')
def dismiss_job(job_id: str, data: dict):
    with state.db() as c:
        result = c.execute("UPDATE jobs SET status='DISMISSED',updated=? WHERE id=? AND status='ERROR'", (time.time(), job_id))
        if result.rowcount != 1:
            raise HTTPException(409, 'Only error jobs can be dismissed')
        state.event(c, job_id, 'Error dismissed; files and original error retained')
    return {'saved': True}


@app.post('/api/jobs/{job_id}/restore')
def restore_job(job_id: str, data: dict):
    with state.db() as c:
        result = c.execute("UPDATE jobs SET status='ERROR',updated=? WHERE id=? AND status='DISMISSED'", (time.time(), job_id))
        if result.rowcount != 1:
            raise HTTPException(409, 'Only dismissed jobs can be restored')
        state.event(c, job_id, 'Dismissed error restored; no processing queued')
    return {'saved': True}


@app.post('/api/packages/{package_id}/dismiss')
def dismiss_package(package_id: str, data: dict):
    with state.db() as c:
        result = c.execute("UPDATE packages SET status='DISMISSED' WHERE id=? AND status='ERROR'", (package_id,))
        if result.rowcount != 1:
            raise HTTPException(409, 'Only failed inspections can be dismissed')
        state.event(c, None, 'Package error dismissed: ' + package_id)
    return {'saved': True}


@app.post('/api/packages/{package_id}/restore')
def restore_package(package_id: str, data: dict):
    with state.db() as c:
        result = c.execute("UPDATE packages SET status='ERROR' WHERE id=? AND status='DISMISSED'", (package_id,))
        if result.rowcount != 1:
            raise HTTPException(409, 'Only dismissed packages can be restored')
        state.event(c, None, 'Package error restored: ' + package_id)
    return {'saved': True}


@app.post('/api/jobs/{job_id}/embedded')
def restore_embedded(job_id: str, data: dict):
    def change(body):
        body['metadata'] = body['embedded'].copy()
        body['provenance'] = {k: 'Embedded / filename' for k, v in body['metadata'].items() if v}
        body['cover_choice'] = 'embedded'
    return edit_transaction(job_id, change)


@app.post('/api/jobs/{job_id}/approve')
def approve(job_id: str, data: dict):
    settings = state.settings()
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = state.unpack(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        editable(job)
        if not job['body']['grouping_confirmed']:
            raise ValueError('Confirm these files represent one book in the correct order')
        media.names(job['body']['metadata'], settings)
        job['body']['force_now'] = bool(data.get('force_now', False))
        c.execute("UPDATE jobs SET status='READY',body=?,error=NULL,updated=? WHERE id=?", (json.dumps(job['body']), time.time(), job_id))
        state.event(c, job_id, 'Approved for finalization' + (' (processing window overridden)' if data.get('force_now') else ''))
    return {'queued': True}


@app.post('/api/jobs/{job_id}/review')
def return_review(job_id: str, data: dict):
    with state.db() as c:
        result = c.execute("UPDATE jobs SET status='REVIEW',updated=? WHERE id=? AND status='READY'", (time.time(), job_id))
        if result.rowcount != 1:
            raise HTTPException(409, 'Only queued jobs can return to review')
    return {'saved': True}


@app.post('/api/jobs/{job_id}/recover')
def recover(job_id: str, data: dict):
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = state.unpack(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        if job['status'] != 'ERROR' or not job['body'].get('publication'):
            raise HTTPException(409, 'Only interrupted publication errors can be recovered')
        c.execute("UPDATE jobs SET status='READY',error=NULL,updated=? WHERE id=?", (time.time(), job_id))
        state.event(c, job_id, 'Retrying unchanged publication')
    return {'queued': True}


class Group(BaseModel):
    title: str
    files: list[str]


class Regroup(BaseModel):
    groups: list[Group]
    excluded: list[str] = Field(default_factory=list)


@app.post('/api/packages/{package_id}/regroup')
def regroup(package_id: str, data: Regroup):
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        jobs = [state.unpack(r) for r in c.execute('SELECT * FROM jobs WHERE package_id=?', (package_id,))]
        if not jobs:
            raise ValueError('No inspected jobs in package')
        for job in jobs:
            editable(job)
        all_files = {f['path']: f for j in jobs for f in j['body']['files']}
        # Keep excluded files in package runtime data so they can be restored later.
        key = 'excluded:' + package_id
        prior = c.execute('SELECT value FROM runtime WHERE key=?', (key,)).fetchone()
        if prior:
            all_files.update({f['path']: f for f in json.loads(prior[0])})
        assigned = [p for group in data.groups for p in group.files] + data.excluded
        if not data.groups or any(not g.files for g in data.groups) or len(assigned) != len(set(assigned)) or set(assigned) != set(all_files):
            raise ValueError('Assign every source file exactly once to a group or explicitly exclude it')
        covers = sorted({p for j in jobs for p in j['body']['covers']})
        c.execute('DELETE FROM jobs WHERE package_id=?', (package_id,))
        for group in data.groups:
            files = [all_files[p] for p in group.files]
            meta = media.embedded(files, group.title)
            meta['title'] = group.title
            body = dict(files=files, embedded=meta.copy(), metadata=meta, provenance={k: 'Embedded / manual grouping' for k, v in meta.items() if v}, candidates=[],
                        grouping_confirmed=True, cover_choice='embedded', covers=covers, lookup_error='', force_now=False)
            job_id = str(uuid.uuid4())
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (job_id, package_id, 'REVIEW', json.dumps(body), None, None, time.time()))
            state.event(c, job_id, f'User confirmed grouping/order. {len(data.excluded)} files explicitly excluded from package.')
        c.execute('INSERT OR REPLACE INTO runtime VALUES (?,?)', (key, json.dumps([all_files[p] for p in data.excluded])))
    return {'saved': True}


@app.get('/api/packages/{package_id}/excluded')
def excluded(package_id: str):
    with state.db() as c:
        row = c.execute('SELECT value FROM runtime WHERE key=?', ('excluded:' + package_id,)).fetchone()
    return json.loads(row[0]) if row else []


@app.get('/api/jobs/{job_id}/cover/{index}')
def local_cover(job_id: str, index: int):
    job = state.job(job_id)
    if not 0 <= index < len(job['body']['covers']):
        raise HTTPException(404)
    path = media.contained(job['body']['covers'][index], state.settings().source_path)
    media.validate_cover(path)
    return FileResponse(path)


@app.get('/api/jobs/{job_id}/embedded-cover')
def embedded_cover(job_id: str):
    job = state.job(job_id)
    source = job['body']['files'][0]
    if not source.get('cover'):
        raise HTTPException(404, 'No embedded cover')
    settings = state.settings()
    path = media.contained(source['path'], settings.source_path)
    cache = media.contained(Path(settings.work_path) / job_id / 'preview-cover', settings.work_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        # A unique temporary prevents concurrent browser requests from reading half an image.
        temp = cache.with_name('preview-' + uuid.uuid4().hex)
        media.run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-map', '0:v:0', '-frames:v', '1', '-c:v', 'copy', '-f', 'image2', '-update', '1', str(temp)], timeout=120)
        media.validate_cover(temp)
        temp.replace(cache)
    return FileResponse(cache, media_type='image/png' if cache.read_bytes().startswith(b'\x89PNG') else 'image/jpeg')


@app.post('/api/preview-name')
def preview(data: dict):
    folder, filename = media.names(data, state.settings())
    return {'path': str(folder / filename)}
