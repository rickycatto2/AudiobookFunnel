import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4
from PIL import Image

from app import media, metadata, state, worker
from app.main import app


def audio(path, codec='aac', title='Test Book', author='Test Author'):
    path.parent.mkdir(parents=True, exist_ok=True)
    media.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.5', '-c:a', codec,
               '-metadata', 'album=' + title, '-metadata', 'album_artist=' + author, str(path)])


def import_book(settings, monkeypatch, codec='aac'):
    path = Path(settings.source_path) / 'Book' / ('01.m4a' if codec == 'aac' else '01.mp3')
    audio(path, codec)
    monkeypatch.setattr(metadata, 'search', lambda *args, **kwargs: [])
    worker.discover(settings)
    worker.inspect_pending(settings)
    with state.db() as c:
        job = state.unpack(c.execute('SELECT * FROM jobs').fetchone())
    return path, job


@pytest.mark.parametrize('codec', ['aac', 'libmp3lame'])
def test_real_media_end_to_end(configured, monkeypatch, codec):
    src, job = import_book(configured, monkeypatch, codec)
    original = media.digest(src)
    cover = src.parent / 'cover.png'
    Image.new('RGB', (50, 50), 'green').save(cover)
    with TestClient(app) as client:
        body = job['body']
        body['covers'] = [str(cover)]
        state.update_job(job['id'], 'REVIEW', body)
        edit = client.put('/api/jobs/' + job['id'], json={'metadata': {'title': 'A <Book>', 'author': 'The Author', 'description': '<script>bad()</script>'}, 'grouping_confirmed': True, 'cover_choice': 'local:0'})
        assert edit.status_code == 200
        assert client.post(f"/api/jobs/{job['id']}/approve", json={'force_now': True}).status_code == 200
        assert worker.finalize_one(configured)
        done = state.job(job['id'])
        assert done['status'] == 'COMPLETE', done['error']
        folder = Path(done['output'])
        output = next(folder.glob('*.m4b'))
        assert MP4(output)['\xa9nam'] == ['A <Book>']
        assert MP4(output)['aART'] == ['The Author']
        assert 'covr' in MP4(output)
        assert (folder / 'desc.txt').read_text() == '<script>bad()</script>'
        assert '<script>' not in (folder / 'reader.html').read_text()
        assert len(media.probe(output)['chapters']) == 1
        assert media.digest(src) == original
        assert media.process(done, configured) == done['output']  # crash after publish recovery
        assert client.put('/api/jobs/' + job['id'], json={'metadata': {'title': 'Changed'}}).status_code == 409


def test_multi_book_grouping_and_order(configured, monkeypatch):
    root = Path(configured.source_path) / 'Collection'
    for folder in ('First', 'Second'):
        for part in ('10', '2'):
            audio(root / folder / (part + '.m4a'), title=folder)
    monkeypatch.setattr(metadata, 'search', lambda *a, **k: [])
    worker.discover(configured)
    worker.discover(configured)
    worker.inspect_pending(configured)
    with TestClient(app) as client:
        data = client.get('/api/jobs').json()
        assert len(data['packages']) == 1 and len(data['jobs']) == 2
        for job in data['jobs']:
            assert not job['body']['grouping_confirmed']
            assert [Path(f['path']).name for f in job['body']['files']] == ['2.m4a', '10.m4a']
            assert client.post(f"/api/jobs/{job['id']}/approve", json={}).status_code == 400
        files = [f['path'] for j in data['jobs'] for f in j['body']['files']]
        package = data['packages'][0]['id']
        invalid = {'groups': [{'title': 'Merged', 'files': [files[0], files[0]]}], 'excluded': files[1:]}
        assert client.post(f'/api/packages/{package}/regroup', json=invalid).status_code == 400
        valid = {'groups': [{'title': 'First', 'files': list(reversed(files[:2]))}, {'title': 'Second', 'files': files[2:3]}], 'excluded': files[3:]}
        assert client.post(f'/api/packages/{package}/regroup', json=valid).status_code == 200
        new = client.get('/api/jobs').json()['jobs']
        first = next(j for j in new if j['body']['metadata']['title'] == 'First')
        assert [f['path'] for f in first['body']['files']] == list(reversed(files[:2]))
        assert len(client.get(f'/api/packages/{package}/excluded').json()) == 1


def test_source_change_and_collision(configured, monkeypatch):
    src, job = import_book(configured, monkeypatch)
    src.write_bytes(src.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='Source changed'):
        media.process(job, configured)
    relative, _ = media.names(job['body']['metadata'], configured)
    destination = Path(configured.library_path) / relative
    destination.mkdir(parents=True)
    (destination / 'keep.txt').write_text('keep')
    with pytest.raises(ValueError, match='already exists'):
        media.process(job, configured)
    assert (destination / 'keep.txt').read_text() == 'keep'


def test_settings_secrets_and_paths(configured):
    with TestClient(app) as client:
        assert client.put('/api/settings', json={'qbit_password': 'secret'}).status_code == 200
        assert client.put('/api/settings', json={'qbit_password': ''}).status_code == 200
        result = client.get('/api/settings').json()['values']
        assert result['qbit_password'] == '' and result['qbit_password_configured']
        assert state.settings().qbit_password == 'secret'
        assert client.put('/api/settings', json={'library_path': configured.source_path}).status_code == 400
        assert client.put('/api/settings', json={'folder_template': '../{title}'}).status_code == 400
        assert client.post('/api/discover', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/discover', content='{}').status_code == 415


def test_confidence_and_asin(configured):
    source = {'title': 'Dune', 'author': 'Frank Herbert', 'duration': 72000, 'asin': 'B012345678'}
    candidate = dict(source, provider='Audible')
    result = metadata.score(source, candidate)
    assert result['total'] == 80  # no narrator/series evidence
    assert metadata.score({'title': 'Dune'}, candidate)['total'] == 35
    assert metadata.score(source, dict(candidate, asin='B987654321'))['conflict']
    assert metadata.asin_from('https://www.audible.com/pd/Dune-Audiobook/B012345678?x=1') == 'B012345678'
    with pytest.raises(ValueError):
        metadata.asin_from('https://evil.example/B012345678')
    assert not metadata.may_automate(source, metadata.ranked(source, [candidate]), configured, True)


def test_naming_escapes_components(configured):
    folder, name = media.names({'title': '../Bad: Book', 'author': 'CON', 'series': '', 'year': ''}, configured)
    assert '..' not in folder.parts and folder.parts[0] == '_CON'
    assert '[]' not in name and '()' not in name
    with pytest.raises(ValueError):
        media.contained(Path(configured.source_path).parent / 'escape', configured.source_path)


def test_scan_retry_does_not_touch_completed_jobs(configured, monkeypatch):
    settings = configured.model_copy(update={'abs_enabled': True, 'abs_url': 'http://abs', 'abs_token': 'token', 'abs_library_id': 'library'})
    with state.db() as c:
        c.execute("INSERT INTO scan_requests(status,updated) VALUES ('PENDING',0)")
    def fail(*args, **kwargs):
        raise RuntimeError('offline')
    monkeypatch.setattr(worker.httpx, 'post', fail)
    worker.scan_library(settings)
    with state.db() as c:
        row = c.execute('SELECT * FROM scan_requests').fetchone()
        assert row['status'] == 'PENDING' and row['error']
        c.execute('UPDATE scan_requests SET updated=0')
    class Response:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(worker.httpx, 'post', lambda *a, **k: Response())
    worker.scan_library(settings)
    with state.db() as c:
        assert c.execute('SELECT status FROM scan_requests').fetchone()[0] == 'ACCEPTED'


@pytest.mark.parametrize('mixed', [False, True])
def test_multiple_parts_preserve_order_and_chapters(configured, monkeypatch, mixed):
    source = Path(configured.source_path) / 'Parts'
    audio(source / '01.m4a')
    audio(source / ('02.mp3' if mixed else '02.m4a'), 'libmp3lame' if mixed else 'aac')
    monkeypatch.setattr(metadata, 'search', lambda *a, **k: [])
    worker.discover(configured)
    worker.inspect_pending(configured)
    with state.db() as c:
        job = state.unpack(c.execute('SELECT * FROM jobs').fetchone())
    folder = Path(media.process(job, configured))
    result = media.probe(next(folder.glob('*.m4b')))
    assert len(result['chapters']) == 2
    assert 2.9 < result['duration'] < 3.3
    manifest = json.loads((folder / 'funnel.json').read_text())
    assert manifest['audio_mode'] == ('AAC encode' if mixed else 'copy')


def test_embedded_cover_bytes_preserved(configured, monkeypatch):
    src, job = import_book(configured, monkeypatch)
    image = src.parent / 'art.png'
    Image.new('RGB', (40, 40), 'navy').save(image)
    media.write_tags(src, {'title': 'Test Book', 'author': 'Test Author'}, image)
    body = job['body']
    body['files'][0].update(media.probe(src), size=src.stat().st_size, mtime=src.stat().st_mtime_ns)
    state.update_job(job['id'], 'REVIEW', body)
    job = state.job(job['id'])
    with TestClient(app) as client:
        response = client.get(f"/api/jobs/{job['id']}/embedded-cover")
        assert response.status_code == 200 and response.content == image.read_bytes()
    folder = Path(media.process(job, configured))
    assert (folder / 'cover.png').read_bytes() == image.read_bytes()


def test_window_and_ready_claim(configured, monkeypatch):
    src, job = import_book(configured, monkeypatch, 'libmp3lame')
    state.update_job(job['id'], 'READY')
    monkeypatch.setattr(worker, 'in_window', lambda s: False)
    assert worker.claim(configured) is None
    body = job['body']
    body['force_now'] = True
    state.update_job(job['id'], 'READY', body)
    assert worker.claim(configured)['id'] == job['id']
    assert worker.claim(configured) is None


def test_publication_recovery_freezes_destination(configured, monkeypatch):
    src, job = import_book(configured, monkeypatch)
    destination = media.process(job, configured)
    recovered = state.job(job['id'])
    changed = configured.model_copy(update={'folder_template': '{title}/{author}', 'file_template': '{author} - {title}'})
    assert media.process(recovered, changed) == destination
