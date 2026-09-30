import json
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import archive, media, state
from app.main import app

HASH = 'a' * 40


def setup(configured, monkeypatch):
    settings = configured.model_copy(update={'qbit_archive_enabled': True, 'qbit_source_path': 'D:/raw', 'qbit_archive_path': 'D:/processed'})
    with state.db() as c:
        c.execute('UPDATE settings SET body=?', (settings.model_dump_json(),))
    source = Path(settings.source_path) / 'Book.m4b'
    source.write_bytes(b'original source')
    library = Path(settings.library_path) / 'Book'
    library.mkdir()
    output = library / 'Book.m4b'
    output.write_bytes(b'processed library copy')
    (library / 'funnel.json').write_text(json.dumps({'job_id': 'job', 'sha256': media.digest(output)}))
    body = dict(files=[{'path': str(source), 'size': source.stat().st_size, 'mtime': source.stat().st_mtime_ns}],
                publication={'library_path': settings.library_path, 'relative': 'Book', 'filename': 'Book.m4b'})
    with state.db() as c:
        c.execute('INSERT INTO packages VALUES (?,?,?,?,?)', ('package', str(source), 'INSPECTED', None, time.time()))
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', ('job', 'package', 'COMPLETE', json.dumps(body), None, str(library), time.time()))
    torrent = dict(hash=HASH, name='Book', save_path='D:/raw', state='stoppedUP', progress=1, amount_left=0,
                   seeding_time_limit=-2, seeding_time=86400, ratio_limit=-1, auto_tmm=False)
    torrents = [torrent]
    preferences = {'max_seeding_time_enabled': True, 'max_seeding_time': 1440}
    calls = []
    real_client = httpx.Client
    def handler(request):
        method = request.url.path.rsplit('/', 1)[1]
        if method == 'login':
            return httpx.Response(200, text='Ok.')
        if method == 'info':
            selected = [t for t in torrents if not request.url.params.get('hashes') or t['hash'] == request.url.params['hashes']]
            return httpx.Response(200, json=selected)
        if method == 'preferences':
            return httpx.Response(200, json=preferences)
        if method == 'files':
            return httpx.Response(200, json=[{'name': 'Book.m4b', 'size': len(b'original source'), 'progress': 1}])
        values = parse_qs(request.content.decode())
        calls.append((method, values))
        assert values['hashes'] == [HASH]
        if method == 'setLocation':
            target = Path(settings.archive_path) / HASH
            target.mkdir()
            shutil.move(source, target / source.name)
            torrent['save_path'] = values['location'][0]
        elif method == 'delete':
            assert values['deleteFiles'] == ['true']
            (Path(settings.archive_path) / HASH / source.name).unlink()
            torrents.clear()
        else:
            raise AssertionError(method)
        return httpx.Response(200)
    monkeypatch.setattr(archive.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return settings, source, output, torrent, torrents, preferences, calls


def row():
    with state.db() as c:
        return dict(c.execute('SELECT * FROM archives WHERE hash=?', (HASH,)).fetchone())


def test_move_verify_explicit_clear_and_restart(configured, monkeypatch):
    settings, source, output, torrent, torrents, prefs, calls = setup(configured, monkeypatch)
    archive.poll(settings)
    assert row()['status'] == 'MOVING' and not source.exists()
    state.init()  # persisted plan survives restart; do not move twice
    archive.poll(settings)
    assert row()['status'] == 'ARCHIVED'
    archive.poll(settings)
    assert len(calls) == 1
    assert output.read_bytes() == b'processed library copy'
    with TestClient(app) as client:
        assert client.post(f'/api/archives/{HASH}/clear', json={}).status_code == 400
        assert client.post(f'/api/archives/{HASH}/clear', json={'confirmed': True}).status_code == 200
        assert client.post(f'/api/archives/{HASH}/clear', json={'confirmed': True}).status_code == 409
    archive.poll(settings)
    assert row()['status'] == 'DELETING'
    archive.poll(settings)
    assert row()['status'] == 'CLEARED'
    assert output.exists() and len(calls) == 2


@pytest.mark.parametrize('change', [{'state': 'stalledUP'}, {'state': 'stoppedDL'}, {'seeding_time': 86399}, {'progress': .9}, {'amount_left': 1}])
def test_seeding_waits(configured, monkeypatch, change):
    settings, source, _, torrent, _, _, calls = setup(configured, monkeypatch)
    torrent.update(change)
    archive.poll(settings)
    assert row()['status'] == 'WAITING' and source.exists() and not calls


def test_global_limit_changes_and_per_torrent_override(configured, monkeypatch):
    settings, source, _, torrent, _, prefs, calls = setup(configured, monkeypatch)
    prefs['max_seeding_time'] = 2880
    archive.poll(settings)
    assert not calls
    torrent['seeding_time_limit'] = 1440
    archive.poll(settings)
    assert calls[0][0] == 'setLocation'


@pytest.mark.parametrize('status', ['REVIEW', 'ERROR', 'ALREADY_EXISTS', 'DISMISSED', 'PROCESSING'])
def test_unresolved_job_blocks_whole_package(configured, monkeypatch, status):
    settings, source, _, _, _, _, calls = setup(configured, monkeypatch)
    with state.db() as c:
        c.execute('INSERT INTO jobs SELECT ?,package_id,?,body,NULL,NULL,updated FROM jobs WHERE id=?', ('other', status, 'job'))
    archive.poll(settings)
    assert row()['status'] == 'BLOCKED' and not calls and source.exists()


@pytest.mark.parametrize('problem', ['shared', 'auto_tmm', 'destination', 'changed_source', 'missing_library'])
def test_unsafe_moves_blocked(configured, monkeypatch, problem):
    settings, source, output, torrent, torrents, _, calls = setup(configured, monkeypatch)
    if problem == 'shared':
        torrents.append(dict(torrent, hash='b' * 40))
    elif problem == 'auto_tmm':
        torrent['auto_tmm'] = True
    elif problem == 'destination':
        (Path(settings.archive_path) / HASH).mkdir()
    elif problem == 'changed_source':
        source.write_bytes(b'changed source!')
    else:
        output.unlink()
    archive.poll(settings)
    assert row()['status'] == 'BLOCKED' and not calls and source.exists()


@pytest.mark.parametrize('problem', ['resumed', 'library_changed', 'archive_changed', 'moved_elsewhere', 'limit_extended'])
def test_clear_revalidates_live_state(configured, monkeypatch, problem):
    settings, _, output, torrent, _, prefs, calls = setup(configured, monkeypatch)
    archive.poll(settings)
    archive.poll(settings)
    with TestClient(app) as client:
        assert client.post(f'/api/archives/{HASH}/clear', json={'confirmed': True}).status_code == 200
    if problem == 'resumed':
        torrent['state'] = 'uploading'
    elif problem == 'library_changed':
        output.write_bytes(b'changed')
    elif problem == 'archive_changed':
        (Path(settings.archive_path) / HASH / 'Book.m4b').write_bytes(b'changed')
    elif problem == 'limit_extended':
        prefs['max_seeding_time'] = 2880
    else:
        torrent['save_path'] = 'D:/elsewhere'
    archive.poll(settings)
    assert len(calls) == 1 and row()['error']


def test_duplicate_confirmation_and_archive_lock(configured, monkeypatch):
    settings, _, _, _, _, _, calls = setup(configured, monkeypatch)
    state.update_job('job', 'ALREADY_EXISTS')
    archive.poll(settings)
    assert not calls
    with TestClient(app) as client:
        assert client.post('/api/jobs/job/confirm-duplicate', json={}).status_code == 400
        assert client.post('/api/jobs/job/confirm-duplicate', json={'confirmed': True}).status_code == 200
        archive.poll(settings)
        assert row()['status'] == 'MOVING'
        assert client.post('/api/jobs/job/review-existing', json={}).status_code == 409


@pytest.mark.parametrize('name', ['../book.mp3', '/book.mp3', 'C:/book.mp3', 'folder/../../book.mp3'])
def test_unsafe_torrent_paths(name):
    with pytest.raises(ValueError):
        archive.relative_name(name)


def test_windows_remote_mapping_is_case_insensitive():
    assert archive.remote_path('d:\\RAW\\Book') .relative_to(archive.remote_path('D:/raw')).parts == ('Book',)


def test_lost_move_response_recovers_without_second_move(configured, monkeypatch):
    settings, source, _, _, _, _, calls = setup(configured, monkeypatch)
    original = archive.post
    def lost(client, method, **data):
        original(client, method, **data)
        raise httpx.ReadTimeout('response lost')
    monkeypatch.setattr(archive, 'post', lost)
    archive.poll(settings)
    assert row()['status'] == 'MOVING' and row()['error']
    assert not source.exists()
    archive.poll(settings)
    assert row()['status'] == 'ARCHIVED' and not row()['error']
    assert len(calls) == 1


def test_unrelated_unresolved_job_in_same_package_blocks(configured, monkeypatch):
    settings, source, _, _, _, _, calls = setup(configured, monkeypatch)
    with state.db() as c:
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', ('other', 'package', 'REVIEW', json.dumps({'files': [{'path': str(source.parent / 'Other.m4b')}]}), None, None, time.time()))
    archive.poll(settings)
    assert 'every book' in row()['error'] and not calls


def test_unexpected_archive_file_blocks_deletion(configured, monkeypatch):
    settings, _, _, _, _, _, calls = setup(configured, monkeypatch)
    archive.poll(settings)
    archive.poll(settings)
    extra = Path(settings.archive_path) / HASH / 'unrelated.txt'
    extra.write_text('keep me')
    with TestClient(app) as client:
        assert client.post(f'/api/archives/{HASH}/clear', json={'confirmed': True}).status_code == 200
    archive.poll(settings)
    assert 'unexpected' in row()['error'] and len(calls) == 1 and extra.exists()
