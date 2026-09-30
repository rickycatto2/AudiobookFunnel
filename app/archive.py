"""qBittorrent owns every source move/delete. Both source mounts stay read-only.

Persist intent before external operations; verify their outcome on later polls.
Never infer torrent ownership from titles, and never treat a collision as consent.
"""
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx

from app import media, state

LOCKED = {'MOVING', 'ARCHIVED', 'DELETE_REQUESTED', 'DELETING', 'CLEARED'}
STOPPED = {'stoppedUP', 'pausedUP'}


def remote_path(value):
    cls = PureWindowsPath if '\\' in value or re.match(r'^[A-Za-z]:', value) else PurePosixPath
    path = cls(value)
    if not path.is_absolute() or '..' in path.parts or path == cls(path.anchor):
        raise ValueError('Use an absolute qBittorrent folder, not a drive root or traversal path')
    return path


def relative_name(value):
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or not path.parts or any(x in {'.', '..'} or ':' in x for x in path.parts):
        raise ValueError('Unsafe torrent filename')
    return path


@contextmanager
def connection(settings):
    with httpx.Client(base_url=settings.qbit_url.rstrip('/'), timeout=20) as client:
        response = client.post('/api/v2/auth/login', data={'username': settings.qbit_username, 'password': settings.qbit_password})
        response.raise_for_status()
        if response.text.strip() != 'Ok.':
            raise ValueError('qBittorrent login failed; save the Web UI login in Settings')
        yield client


def get(client, method, **params):
    response = client.get('/api/v2/' + method, params=params)
    response.raise_for_status()
    return response.json()


def post(client, method, **data):
    response = client.post('/api/v2/torrents/' + method, data=data)
    response.raise_for_status()


def seed_ready(torrent, preferences):
    if torrent.get('state') not in STOPPED or torrent.get('progress') != 1 or torrent.get('amount_left', -1) != 0:
        return False
    # qBittorrent's limit fields and preferences are minutes, elapsed time seconds.
    minutes = torrent.get('seeding_time_limit', -1)
    if minutes == -2:
        minutes = preferences.get('max_seeding_time', -1) if preferences.get('max_seeding_time_enabled') else -1
    ratio = torrent.get('ratio_limit', -1)
    if ratio == -2:
        ratio = preferences.get('max_ratio', -1) if preferences.get('max_ratio_enabled') else -1
    if minutes >= 0:
        return torrent.get('seeding_time', -1) >= minutes * 60
    return ratio >= 0 and torrent.get('ratio', -1) >= ratio


def save(hash_, name, status, body, error=None):
    with state.db() as c:
        previous = c.execute('SELECT status,error FROM archives WHERE hash=?', (hash_,)).fetchone()
        c.execute('INSERT OR REPLACE INTO archives VALUES (?,?,?,?,?,?)', (hash_, name, status, json.dumps(body), error, time.time()))
        if not previous or previous['status'] != status or previous['error'] != error:
            state.event(c, None, f'Archive {name}: {status}' + (f' — {error}' if error else ''))


def locked(c, job_id):
    return any(job_id in json.loads(r['body']).get('jobs', []) for r in c.execute(
        "SELECT body FROM archives WHERE status IN ('MOVING','ARCHIVED','DELETE_REQUESTED','DELETING','CLEARED')"))


def outputs(job, settings):
    """Verify a recoverable library copy before allowing source disposal."""
    if job['status'] == 'DUPLICATE_CONFIRMED':
        files = job['body'].get('duplicate_outputs', [])
    elif job['status'] == 'COMPLETE':
        folder = media.contained(job['output'], settings.library_path)
        manifest = json.loads((folder / 'funnel.json').read_text('utf-8'))
        plan = job['body']['publication']
        if manifest.get('job_id') != job['id']:
            raise ValueError('Library manifest no longer belongs to this book')
        files = [{'path': str(folder / plan['filename']), 'sha256': manifest['sha256']}]
    else:
        raise ValueError('Waiting for every book to finish or have its duplicate confirmed')
    if not files:
        raise ValueError('No verified library audio')
    for file in files:
        path = media.contained(file['path'], settings.library_path)
        if not path.is_file() or media.digest(path) != file['sha256']:
            raise ValueError('Library audio is missing or changed; archive cleanup blocked')


def inventory(client, torrent):
    files = get(client, 'torrents/files', hash=torrent['hash'])
    if not files or any(f.get('progress') != 1 for f in files):
        raise ValueError('Waiting for all torrent files to download')
    result = [{'name': str(relative_name(f['name'])), 'size': f['size']} for f in files]
    if len({f['name'].casefold() for f in result}) != len(result):
        raise ValueError('Ambiguous torrent filenames')
    return sorted(result, key=lambda f: f['name'])


def associate(files, torrent, settings):
    base = remote_path(torrent['save_path']).relative_to(remote_path(settings.qbit_source_path))
    mapped = {str(media.contained(Path(settings.source_path).joinpath(*base.parts, *relative_name(f['name']).parts), settings.source_path)): f for f in files}
    audio = {p for p in mapped if Path(p).suffix.lower() in media.AUDIO}
    with state.db() as c:
        jobs = [state.unpack(r) for r in c.execute('SELECT * FROM jobs')]
    matched = [j for j in jobs if any(f['path'] in audio for f in j['body']['files'])]
    covered = [f['path'] for j in matched for f in j['body']['files']]
    if not audio or set(covered) != audio or len(covered) != len(set(covered)):
        raise ValueError('Torrent audio must map exactly to inspected book jobs; excluded or unknown audio needs review')
    # A source package can hold several books, including files absent from this torrent.
    packages = {j['package_id'] for j in matched}
    if any(j['status'] not in {'COMPLETE', 'DUPLICATE_CONFIRMED'} for j in jobs if j['package_id'] in packages):
        raise ValueError('Waiting for every book in the source package to finish')
    for job in matched:
        outputs(job, settings)
        for file in job['body']['files']:
            stat = Path(file['path']).stat()
            if stat.st_size != file['size'] or stat.st_mtime_ns != file['mtime']:
                raise ValueError('Source changed since inspection; cleanup blocked')
    for path, file in mapped.items():
        if not Path(path).is_file() or Path(path).stat().st_size != file['size']:
            raise ValueError('Source mapping or file size differs from qBittorrent')
    return matched, mapped


def shared_files(client, torrent, files, torrents):
    own = {remote_path(torrent['save_path']).joinpath(*relative_name(f['name']).parts) for f in files}
    for other in torrents:
        if other['hash'] == torrent['hash']:
            continue
        # Query potentially overlapping save roots only; never ignore an API failure.
        base, ours = remote_path(other['save_path']), remote_path(torrent['save_path'])
        if not (base.is_relative_to(ours) or ours.is_relative_to(base)):
            continue
        other_files = get(client, 'torrents/files', hash=other['hash'])
        if any(base.joinpath(*relative_name(f['name']).parts) in own for f in other_files):
            raise ValueError('Another torrent uses these files; cleanup blocked')


def verify_archive(body, settings, full=True):
    root = media.contained(body['local_destination'], settings.archive_path)
    expected = {str(relative_name(f['name'])) for f in body['files']}
    present = set()
    for item in root.rglob('*'):
        media.contained(item, root)
        if item.is_file():
            present.add(item.relative_to(root).as_posix())
    if present != expected:
        raise ValueError('Archive contains missing or unexpected files; inspect manually')
    for file in body['files']:
        path = media.contained(root.joinpath(*relative_name(file['name']).parts), root)
        if not path.is_file() or path.stat().st_size != file['size'] or (full and media.digest(path) != file['sha256']):
            raise ValueError('Archived files are not yet verified; nothing will be cleared')


def advance(client, row, torrent, settings, torrents, preferences):
    body = json.loads(row['body'])
    status, hash_ = row['status'], row['hash']
    if status == 'CLEARED':
        return
    if status == 'DELETING' and torrent is None:
        root = media.contained(body['local_destination'], settings.archive_path)
        if any(root.joinpath(*relative_name(f['name']).parts).exists() for f in body['files']):
            raise ValueError('Torrent removed but archived files remain; inspect manually')
        save(hash_, row['name'], 'CLEARED', body)
        return
    if torrent is None:
        raise ValueError('Torrent is no longer in qBittorrent; inspect manually')
    if remote_path(torrent['save_path']) != remote_path(body['destination']):
        if status == 'MOVING' and remote_path(torrent['save_path']) == remote_path(body['original_save_path']):
            if not seed_ready(torrent, preferences):
                raise ValueError('Waiting for torrent to stop and meet its seeding limit')
            if torrent.get('auto_tmm'):
                raise ValueError('Turn off Automatic Torrent Management for this torrent before archiving')
            if inventory(client, torrent) != [{k: f[k] for k in ('name', 'size')} for f in body['files']]:
                raise ValueError('Torrent inventory changed; inspect manually')
            local = media.contained(body['local_destination'], settings.archive_path)
            if local.exists():
                raise ValueError('Archive destination exists before move; inspect manually')
            for path, file in zip(body['sources'], body['files']):
                source = media.contained(path, settings.source_path)
                if not source.is_file() or media.digest(source) != file['sha256']:
                    raise ValueError('Source changed after archive planning; cleanup blocked')
            for job_id in body['jobs']:
                outputs(state.job(job_id), settings)
            shared_files(client, torrent, body['files'], torrents)
            post(client, 'setLocation', hashes=hash_, location=body['destination'])
            return
        raise ValueError('Torrent location changed outside Funnel; cleanup blocked')
    if torrent.get('state') not in STOPPED:
        raise ValueError('Waiting for archived torrent to finish moving and remain stopped')
    if inventory(client, torrent) != [{k: f[k] for k in ('name', 'size')} for f in body['files']]:
        raise ValueError('Torrent inventory changed; cleanup blocked')
    if torrent.get('auto_tmm'):
        raise ValueError('Automatic Torrent Management must remain off for cleanup')
    verify_archive(body, settings, full=status != 'ARCHIVED' or bool(row['error']))
    if status == 'MOVING':
        if any(Path(p).exists() for p in body['sources']):
            raise ValueError('Waiting for original source files to leave RAW')
        save(hash_, row['name'], 'ARCHIVED', body)
    elif status in {'DELETE_REQUESTED', 'DELETING'}:
        if not seed_ready(torrent, preferences):
            raise ValueError('Seeding limit changed or is not met; clear request is waiting')
        for job_id in body['jobs']:
            outputs(state.job(job_id), settings)
        shared_files(client, torrent, body['files'], torrents)
        save(hash_, row['name'], 'DELETING', body)
        post(client, 'delete', hashes=hash_, deleteFiles='true')
    elif row['error']:
        save(hash_, row['name'], 'ARCHIVED', body)


def poll(settings):
    if not settings.qbit_archive_enabled:
        return
    # No local source deletion/move, even though qBittorrent itself is authorized.
    with connection(settings) as client:
        torrents = get(client, 'torrents/info')
        preferences = get(client, 'app/preferences')
        by_hash = {t['hash']: t for t in torrents}
        with state.db() as c:
            rows = {r['hash']: dict(r) for r in c.execute('SELECT * FROM archives')}
        for hash_, row in rows.items():
            if row['status'] in LOCKED:
                try:
                    advance(client, row, by_hash.get(hash_), settings, torrents, preferences)
                except (ValueError, OSError, httpx.HTTPError, KeyError) as exc:
                    save(hash_, row['name'], row['status'], json.loads(row['body']), str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        for torrent in torrents:
            hash_ = torrent['hash']
            if not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', hash_) or (hash_ in rows and rows[hash_]['status'] in LOCKED):
                continue
            if not remote_path(torrent['save_path']).is_relative_to(remote_path(settings.qbit_source_path)):
                continue
            body = {}
            try:
                if not seed_ready(torrent, preferences):
                    save(hash_, torrent['name'], 'WAITING', body, 'Waiting for completed download, stopped state, and total seeding-time or ratio limit')
                    continue
                if torrent.get('auto_tmm'):
                    raise ValueError('Turn off Automatic Torrent Management for this torrent before archiving')
                files = inventory(client, torrent)
                jobs, mapped = associate(files, torrent, settings)
                shared_files(client, torrent, files, torrents)
                destination = remote_path(settings.qbit_archive_path) / hash_
                local = media.contained(Path(settings.archive_path) / hash_, settings.archive_path)
                if local.exists():
                    raise ValueError('Archive destination already exists; nothing overwritten')
                # Hash every original, including sidecars, before requesting the move.
                for path, file in mapped.items():
                    file['sha256'] = media.digest(Path(path))
                body = dict(files=files, jobs=[j['id'] for j in jobs], sources=list(mapped), destination=str(destination),
                            local_destination=str(local), original_save_path=torrent['save_path'])
                with state.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    for job in jobs:
                        current = c.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()
                        if not current or current['status'] not in {'COMPLETE', 'DUPLICATE_CONFIRMED'}:
                            raise ValueError('Book status changed; waiting for review')
                    c.execute('INSERT OR REPLACE INTO archives VALUES (?,?,?,?,?,?)', (hash_, torrent['name'], 'MOVING', json.dumps(body), None, time.time()))
                    state.event(c, None, 'Archive move planned: ' + torrent['name'])
                # Re-read before mutation; retries always use this persisted plan.
                fresh = get(client, 'torrents/info', hashes=hash_)
                advance(client, dict(hash=hash_, name=torrent['name'], status='MOVING', body=json.dumps(body), error=None), fresh[0] if fresh else None, settings, torrents, preferences)
            except (ValueError, OSError, httpx.HTTPError, KeyError) as exc:
                with state.db() as c:
                    planned = c.execute('SELECT status FROM archives WHERE hash=?', (hash_,)).fetchone()
                status = 'MOVING' if planned and planned['status'] == 'MOVING' else 'BLOCKED'
                save(hash_, torrent['name'], status, body, str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
