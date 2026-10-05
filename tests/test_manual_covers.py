import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from mutagen.mp4 import MP4

from app import covers, media, state, worker
from app.main import app
from test_pipeline import import_book


def picture():
    buf = io.BytesIO()
    Image.new('RGBA', (60, 90), 'blue').save(buf, format='PNG')
    return buf.getvalue()


@pytest.mark.parametrize('kind', ['upload', 'url'])
def test_manual_cover_survives_publication(configured, monkeypatch, kind):
    src, job = import_book(configured, monkeypatch)
    original = media.digest(src)
    monkeypatch.setattr(covers, 'fetch', lambda url: picture())
    payload = {'url': 'https://publisher.example/cover.png'} if kind == 'url' else {'image': base64.b64encode(picture()).decode()}
    with TestClient(app) as client:
        result = client.post(f"/api/jobs/{job['id']}/manual-cover", json=payload)
        assert result.status_code == 200, result.text
        saved = state.job(job['id'])
        assert saved['body']['cover_choice'] == 'manual'
        assert saved['body']['metadata'] == job['body']['metadata']
        preview = client.get(f"/api/jobs/{job['id']}/manual-cover")
        assert preview.status_code == 200 and preview.content.startswith(b'\xff\xd8')
        body = saved['body']; body['grouping_confirmed'] = True
        state.update_job(job['id'], 'READY', body)
        worker.finalize_one(configured)
        done = state.job(job['id'])
        assert done['status'] == 'COMPLETE', done['error']
        output = next(Path(done['output']).glob('*.m4b'))
        assert bytes(MP4(output)['covr'][0]) == preview.content
        assert (Path(done['output']) / 'cover.jpg').read_bytes() == preview.content
        assert media.digest(src) == original
        assert client.post(f"/api/jobs/{job['id']}/manual-cover", json=payload).status_code == 409


def test_invalid_upload_preserves_previous_choice(configured, monkeypatch):
    _, job = import_book(configured, monkeypatch)
    with TestClient(app) as client:
        for payload in ({'image': 'not base64'}, {'image': base64.b64encode(b'<html>not image</html>').decode()}, [], {}):
            assert client.post(f"/api/jobs/{job['id']}/manual-cover", json=payload).status_code == 400
        assert state.job(job['id'])['body']['cover_choice'] == job['body']['cover_choice']


@pytest.mark.parametrize('url', ['http://example.com/a', 'file:///etc/passwd', 'https://localhost/a', 'https://127.0.0.1/a', 'https://user:pass@example.com/a'])
def test_private_urls_rejected(monkeypatch, url):
    monkeypatch.setattr(covers.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError):
        covers.public_target(url)


def test_provider_redirect_checked_and_followed(monkeypatch):
    checked = []
    def target(url):
        checked.append(url)
        return covers.urlsplit(url), '8.8.8.8'
    monkeypatch.setattr(covers, 'public_target', target)
    class Response:
        status = 302
        def getheader(self, name):
            return 'https://archive.org/cover.jpg'
    class Final:
        status = 200
        def read(self, limit):
            return picture()
    class Connection:
        def __init__(self, host, **kwargs): self.host = host
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response() if self.host == 'covers.openlibrary.org' else Final()
        def close(self): pass
    monkeypatch.setattr(covers.http.client, 'HTTPSConnection', Connection)
    assert covers.fetch('https://covers.openlibrary.org/b/id/12042053-L.jpg') == picture()
    assert checked == ['https://covers.openlibrary.org/b/id/12042053-L.jpg', 'https://archive.org/cover.jpg']


def test_redirect_to_private_address_blocked(monkeypatch):
    monkeypatch.setattr(covers.socket, 'getaddrinfo', lambda host, *a, **k: [(2, 1, 6, '', ('127.0.0.1' if host == 'localhost' else '8.8.8.8', 443))])
    class Connection:
        status = 302
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return self
        def getheader(self, name): return 'https://localhost/private'
        def close(self): pass
    monkeypatch.setattr(covers.http.client, 'HTTPSConnection', Connection)
    with pytest.raises(ValueError, match='public internet'):
        covers.fetch('https://example.com/image')
