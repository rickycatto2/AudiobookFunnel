import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app import media, metadata, state

log = logging.getLogger('funnel')


def discover(settings):
    root = Path(settings.source_path)
    if not root.is_dir():
        raise ValueError('Source directory does not exist. Check the Docker mount and Settings.')
    with state.db() as c:
        for path in sorted(root.iterdir()):
            if path.name.startswith('.') or (path.is_file() and path.suffix.lower() not in media.AUDIO):
                continue
            media.contained(path, root)
            c.execute('INSERT OR IGNORE INTO packages VALUES (?,?,?,?,?)', (str(uuid.uuid4()), str(path), 'PENDING', None, time.time()))


def inspect_pending(settings):
    with state.db() as c:
        package = c.execute("SELECT * FROM packages WHERE status='PENDING' ORDER BY created LIMIT 1").fetchone()
    if not package:
        return
    try:
        files = media.inspect_package(package['source'], settings.source_path)
        groups = defaultdict(list)
        for f in files:
            # Album disagreements inside a folder are never automatically flattened.
            groups[(str(Path(f['relative']).parent), f['tags'].get('album', ''))].append(f)
        prepared = []
        for (folder, album), members in groups.items():
            source = Path(package['source'])
            title = album or (Path(folder).name if folder != '.' else source.stem)
            meta = media.embedded(members, title)
            try:
                candidates = metadata.ranked(meta, metadata.search('Audible', meta['title'], meta['author'], settings.audible_region))
                lookup_error = ''
            except Exception as exc:
                candidates, lookup_error = [], f'Metadata lookup unavailable ({type(exc).__name__}); retry search or edit manually.'
            confirmed = len(members) == 1 and not members[0]['warning']
            covers = []
            for parent in {Path(members[0]['path']).parent, source if source.is_dir() else source.parent}:
                for p in parent.iterdir():
                    if p.suffix.lower() in ('.jpg', '.jpeg', '.png') and p.is_file() and len(covers) < 20:
                        covers.append(str(media.contained(p, settings.source_path)))
            body = dict(files=members, embedded=meta.copy(), metadata=meta, provenance={k: 'Embedded / filename' for k, v in meta.items() if v}, candidates=candidates,
                        grouping_confirmed=confirmed, cover_choice='embedded', covers=sorted(set(covers)), lookup_error=lookup_error, force_now=False)
            status = 'REVIEW'
            if metadata.may_automate(meta, candidates, settings, confirmed):
                selected = candidates[0]
                for key in metadata.FIELDS:
                    if selected.get(key):
                        body['metadata'][key] = selected[key]
                        body['provenance'][key] = selected['provider']
                body['cover_choice'] = 'provider' if selected.get('cover_url') else 'embedded'
                status = 'READY'
            prepared.append((str(uuid.uuid4()), body, status))
        with state.db() as c:
            for job_id, body, status in prepared:
                c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (job_id, package['id'], status, json.dumps(body), None, None, time.time()))
                state.event(c, job_id, 'Inspected source; grouping proposed; metadata lookup attempted')
            c.execute("UPDATE packages SET status='INSPECTED',error=NULL WHERE id=?", (package['id'],))
    except Exception as exc:
        with state.db() as c:
            c.execute("UPDATE packages SET status='ERROR',error=? WHERE id=?", (str(exc), package['id']))


def in_window(settings):
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    start, end = settings.heavy_start, settings.heavy_end
    return start == end or (start <= hour < end if start < end else hour >= start or hour < end)


def claim(settings):
    with state.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute("SELECT * FROM jobs WHERE status='READY' ORDER BY updated").fetchall():
            job = state.unpack(row)
            if not (job['body'].get('force_now') or media.copy_mode(job['body']['files']) or in_window(settings)):
                continue
            c.execute("UPDATE jobs SET status='PROCESSING',error=NULL,updated=? WHERE id=?", (time.time(), job['id']))
            state.event(c, job['id'], 'Processing started')
            return job


def finalize_one(settings):
    job = claim(settings)
    if not job:
        return False
    try:
        output = media.process(job, settings)
        with state.db() as c:
            c.execute("UPDATE jobs SET status='COMPLETE',output=?,error=NULL,updated=? WHERE id=?", (output, time.time(), job['id']))
            state.event(c, job['id'], 'Verified audio, tags and sidecars; published to library')
            if settings.abs_enabled:
                if not c.execute("SELECT id FROM scan_requests WHERE status='PENDING'").fetchone():
                    c.execute("INSERT INTO scan_requests(status,updated) VALUES ('PENDING',?)", (time.time(),))
    except Exception as exc:
        state.update_job(job['id'], 'ERROR', error=str(exc), message='Processing failed; source and working copies retained')
    return True


def scan_library(settings):
    if not settings.abs_enabled:
        return
    with state.db() as c:
        # Defer until currently runnable finalization batch has drained.
        ready = [state.unpack(r) for r in c.execute("SELECT * FROM jobs WHERE status='READY'")]
        if any(j['body'].get('force_now') or media.copy_mode(j['body']['files']) or in_window(settings) for j in ready):
            return
        scan = c.execute("SELECT * FROM scan_requests WHERE status='PENDING' ORDER BY id LIMIT 1").fetchone()
    if not scan or (scan['error'] and time.time() - scan['updated'] < 60):
        return
    try:
        if not settings.abs_url or not settings.abs_token or not settings.abs_library_id:
            raise ValueError('Audiobookshelf URL, token and library ID are required')
        response = httpx.post(settings.abs_url.rstrip('/') + '/api/libraries/' + settings.abs_library_id + '/scan', headers={'Authorization': 'Bearer ' + settings.abs_token}, timeout=30)
        response.raise_for_status()
        status, error = 'ACCEPTED', None
    except Exception as exc:
        status, error = 'PENDING', f'Scan failed ({type(exc).__name__}); retrying in 60 seconds'
    with state.db() as c:
        c.execute('UPDATE scan_requests SET status=?,error=?,updated=? WHERE id=?', (status, error, time.time(), scan['id']))


def submit_torrents(settings):
    if not settings.qbit_enabled:
        return
    paths = list(Path(settings.torrent_path).glob('*.torrent'))
    pending = []
    with state.db() as c:
        for path in paths:
            media.contained(path, settings.torrent_path)
            digest = media.digest(path)
            if not c.execute('SELECT 1 FROM torrents WHERE digest=?', (digest,)).fetchone():
                pending.append((path, digest))
    if not pending:
        return
    with httpx.Client(base_url=settings.qbit_url.rstrip('/'), timeout=30) as client:
        response = client.post('/api/v2/auth/login', data={'username': settings.qbit_username, 'password': settings.qbit_password})
        response.raise_for_status()
        if response.text.strip() != 'Ok.':
            raise ValueError('qBittorrent authentication failed')
        for path, digest in pending:
            with path.open('rb') as f:
                response = client.post('/api/v2/torrents/add', data={'savepath': settings.qbit_save_path, 'autoTMM': 'false'}, files={'torrents': (path.name, f, 'application/x-bittorrent')})
            response.raise_for_status()
            if response.text.strip() != 'Ok.':
                raise ValueError('qBittorrent did not accept torrent')
            with state.db() as c:
                c.execute('INSERT OR IGNORE INTO torrents VALUES (?,?,?)', (digest, path.name, time.time()))


def worker_lock():
    state.DATA.mkdir(parents=True, exist_ok=True)
    handle = (state.DATA / 'worker.lock').open('a+b')
    handle.write(b'0')
    handle.flush()
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main():
    logging.basicConfig(level=logging.INFO)
    state.init()
    lock = worker_lock()
    with state.db() as c:
        for row in c.execute("SELECT id FROM jobs WHERE status='PROCESSING'").fetchall():
            c.execute("UPDATE jobs SET status='READY' WHERE id=?", (row['id'],))
            state.event(c, row['id'], 'Recovered interrupted job on worker restart')
    while lock:
        state.runtime('worker_heartbeat', time.time())
        settings = state.settings()
        for operation in ([discover] if settings.monitor_enabled else []) + [submit_torrents, inspect_pending, finalize_one, scan_library]:
            try:
                operation(settings)
                state.runtime(operation.__name__ + '_error', '')
            except Exception as exc:
                log.error('%s failed: %s', operation.__name__, type(exc).__name__)
                state.runtime(operation.__name__ + '_error', f'{type(exc).__name__}: {str(exc)[:500]}' if isinstance(exc, ValueError) else type(exc).__name__)
        time.sleep(5)


if __name__ == '__main__':
    main()
