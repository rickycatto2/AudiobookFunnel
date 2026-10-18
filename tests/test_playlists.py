import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import audiobookshelf, state
from app.main import app


@pytest.fixture
def abs_service(configured, monkeypatch):
    settings = configured.model_copy(update={'abs_url': 'http://abs.test', 'abs_token': 'secret-test-token', 'abs_library_id': 'lib'})
    with state.db() as c:
        c.execute('UPDATE settings SET body=?', (settings.model_dump_json(),))
        body = {'publication': {'library_path': '/library', 'relative': 'Author/Book', 'filename': 'Book.m4b'},
                'metadata': {'title': 'Same Title', 'author': 'Same Author'}}
        c.execute('INSERT INTO packages VALUES (?,?,?,?,?)', ('package', '/source/book', 'INSPECTED', None, time.time()))
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', ('job', 'package', 'COMPLETE', json.dumps(body), None, '/library/Author/Book', time.time()))
    client = TestClient(app)
    original = httpx.Client
    service = {'items': [{'id': 'book-id', 'libraryId': 'lib', 'path': '/different/Author/Book', 'relPath': 'Author/Book',
                          'media': {'metadata': {'title': 'Entirely different title'}}}],
               'playlist': {'id': 'tbr', 'name': 'TBR', 'libraryId': 'lib', 'items': []}, 'adds': 0, 'fail': False}

    def handler(request):
        assert request.headers['Authorization'] == 'Bearer secret-test-token'
        if service['fail']:
            raise httpx.ConnectError('secret-test-token', request=request)
        path = request.url.path
        if path == '/api/playlists':
            if request.method == 'POST':
                data = json.loads(request.content)
                assert data['libraryId'] == 'lib' and data['items'] == []
                return httpx.Response(200, json=dict(service['playlist'], name=data['name']))
            return httpx.Response(200, json=[service['playlist']])
        if path == '/api/libraries/lib/items':
            return httpx.Response(200, json={'results': service['items'], 'total': len(service['items'])})
        if path == '/api/playlists/tbr':
            return httpx.Response(200, json=service['playlist'])
        if path == '/api/playlists/tbr/item':
            assert json.loads(request.content) == {'libraryItemId': 'book-id'}
            service['adds'] += 1
            service['playlist']['items'].append({'libraryItemId': 'book-id'})
            return httpx.Response(200, json=service['playlist'])
        pytest.fail('Unexpected ABS call: ' + path)

    monkeypatch.setattr(audiobookshelf.httpx, 'Client', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    return client, service


def test_list_and_create(abs_service):
    client, _ = abs_service
    assert client.get('/api/playlists').json() == {'playlists': [{'id': 'tbr', 'name': 'TBR'}]}
    assert client.post('/api/playlists', json={'name': 'Next listens'}).json() == {'id': 'tbr', 'name': 'Next listens'}
    assert client.post('/api/playlists', json={'name': '   '}).status_code == 400


def test_add_exact_relative_path_and_idempotence(abs_service):
    client, service = abs_service
    assert client.get('/api/jobs/job/library-item').json() == {'libraryItemId': 'book-id'}
    for message in ('Added to TBR', 'Already in TBR'):
        response = client.post('/api/playlists/tbr/items', json={'job_id': 'job'})
        assert response.status_code == 200
        assert response.json()['message'] == message
    assert service['adds'] == 1
    assert len(service['playlist']['items']) == 1


@pytest.mark.parametrize('items', [[], [{'id': 'wrong', 'path': '/other/book', 'relPath': 'Other/Book',
                                      'media': {'metadata': {'title': 'Same Title', 'author': 'Same Author'}}}]])
def test_unindexed_and_title_author_never_fallback(abs_service, items):
    client, service = abs_service
    service['items'] = items
    response = client.post('/api/playlists/tbr/items', json={'job_id': 'job'})
    assert response.status_code == 409
    assert 'Not yet in Audiobookshelf' in response.json()['detail']
    assert service['adds'] == 0


def test_absolute_path_and_ambiguity(abs_service):
    client, service = abs_service
    service['items'] = [{'id': 'book-id', 'path': '/library/Author/Book'}]
    assert client.get('/api/jobs/job/library-item').status_code == 200
    service['items'].append({'id': 'other-id', 'relPath': 'Author/Book'})
    response = client.get('/api/jobs/job/library-item')
    assert response.status_code == 409 and 'ambiguous' in response.text


def test_plan_without_output_is_not_publication(abs_service):
    client, service = abs_service
    with state.db() as c:
        c.execute("UPDATE jobs SET output=NULL,status='PROCESSING'")
    assert client.post('/api/playlists/tbr/items', json={'job_id': 'job'}).status_code == 409
    assert service['adds'] == 0


@pytest.mark.parametrize('method,path,payload', [('GET', '/api/playlists', None), ('POST', '/api/playlists', {'name': 'TBR'}),
                                               ('POST', '/api/playlists/tbr/items', {'job_id': 'job'})])
def test_unreachable_is_safe(abs_service, method, path, payload):
    client, service = abs_service
    service['fail'] = True
    response = client.request(method, path, json=payload)
    assert response.status_code == 502
    assert 'Audiobookshelf is unavailable' in response.text
    assert 'secret-test-token' not in response.text


def test_missing_settings(configured):
    response = TestClient(app).get('/api/playlists')
    assert response.status_code == 400
    assert 'Configure Audiobookshelf' in response.text


def test_upstream_error_body_never_returned(abs_service, monkeypatch):
    client, _ = abs_service
    def rejected(*args, **kwargs):
        request = httpx.Request('GET', 'http://abs.test')
        raise httpx.HTTPStatusError('secret-test-token', request=request,
                                    response=httpx.Response(403, text='secret-test-token', request=request))
    monkeypatch.setattr(audiobookshelf, 'request', rejected)
    response = client.get('/api/playlists')
    assert response.status_code == 502
    assert 'secret-test-token' not in response.text


def test_lookup_checks_later_pages(abs_service, monkeypatch):
    client, _ = abs_service
    pages = []
    def paginated(client, method, path, **kwargs):
        page = kwargs['params']['page']
        pages.append(page)
        rows = [{'id': 'wrong', 'relPath': 'Other/Book'}] * 200 if page == 0 else [{'id': 'book-id', 'relPath': 'Author/Book'}]
        return {'results': rows, 'total': 201}
    monkeypatch.setattr(audiobookshelf, 'request', paginated)
    assert client.get('/api/jobs/job/library-item').json() == {'libraryItemId': 'book-id'}
    assert pages == [0, 1]


def test_playlist_ui_contract():
    client = TestClient(app)
    html = client.get('/static/index.html').text
    js = client.get('/static/app.js').text
    assert 'Create new playlist' in html and 'New playlist name' in html
    assert 'Retry lookup / refresh playlists' in html
    assert 'published(j) ? `<button data-playlist=' in js
    assert 'return Boolean(job.output && job.body.publication)' in js
    assert 'esc(p.id)' in js and 'esc(p.name)' in js
    assert 'value || !resolved' in js
