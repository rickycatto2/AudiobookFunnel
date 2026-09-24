from pathlib import Path

import httpx

from app import metadata, state, worker


def test_google_books_mapping_and_api_key(monkeypatch):
    def handler(request):
        assert request.url.params['key'] == 'test-key'
        return httpx.Response(200, json={'items': [{'volumeInfo': {'title': 'Little Brother', 'authors': ['Cory Doctorow'], 'publishedDate': '2008-04-29', 'description': '<b>Summary</b>', 'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': '9780765319852'}]}}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(metadata.httpx, 'Client', lambda **kwargs: client)
    book = metadata.search('Google Books', 'Little Brother', 'Cory Doctorow', google_key='test-key')[0]
    assert book['year'] == '2008' and book['description'] == 'Summary'
    assert book['isbn'] == '9780765319852' and book['provider'] == 'Google Books'


def test_qbit_submission_is_deduplicated_and_non_destructive(configured, monkeypatch):
    path = Path(configured.torrent_path) / 'test.torrent'
    path.write_bytes(b'test fixture')
    settings = configured.model_copy(update={'qbit_enabled': True})
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/add'):
            assert settings.qbit_save_path.encode() in request.content
            assert b'autoTMM' in request.content
        return httpx.Response(200, text='Ok.')
    client = httpx.Client(base_url=settings.qbit_url, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(worker.httpx, 'Client', lambda **kwargs: client)
    worker.submit_torrents(settings)
    worker.submit_torrents(settings)
    assert calls == ['/api/v2/auth/login', '/api/v2/torrents/add']
    assert path.read_bytes() == b'test fixture'
    with state.db() as c:
        assert c.execute('SELECT COUNT(*) FROM torrents').fetchone()[0] == 1
