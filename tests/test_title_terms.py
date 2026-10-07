import httpx
import pytest
from fastapi.testclient import TestClient

from app import metadata, state
from app.main import app
from test_matching_review import seed


@pytest.mark.parametrize('title', [
    'The Book (Unabridged)', 'The Book [Full Cast]', 'The Book - Full-Cast',
    'The Book: Dramatized Adaptation', 'The Book Unabridged',
    'Full Cast - The Book', 'The Book (Unabridged, Full Cast)',
    'The Book [Full Cast] (Unabridged)',
])
def test_clean_edition_labels(title):
    assert metadata.clean_title(title) == 'The Book'


@pytest.mark.parametrize('title', ['The Unabridged History', 'Full Cast', 'Unabridged', 'The Book (A Novel)', 'The Book: Full Cast of Characters'])
def test_preserve_real_title_words(title):
    assert metadata.clean_title(title) == title


def test_custom_terms_are_literal_and_can_be_disabled():
    assert metadata.clean_title('The Book (Special.* Edition)', ['special.* edition']) == 'The Book'
    assert metadata.clean_title('The Book (SpecialXX Edition)', ['special.* edition']) == 'The Book (SpecialXX Edition)'
    assert metadata.clean_title('The Book (Unabridged)', []) == 'The Book (Unabridged)'


@pytest.mark.parametrize('provider', ['Audible', 'Google Books', 'Open Library'])
def test_provider_requests_clean_title_without_changing_author(monkeypatch, provider):
    def handler(request):
        query = request.url.params.get('title') or request.url.params['q'].split(' inauthor:')[0]
        assert 'Unabridged' not in query and 'Full Cast' not in query
        assert 'The Book' in query
        if provider != 'Google Books':
            assert request.url.params['author'] == 'Full Cast Writer'
        return httpx.Response(200, json={})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(metadata.httpx, 'Client', lambda **kwargs: client)
    assert metadata.search(provider, 'The Book (Unabridged) [Full Cast]', 'Full Cast Writer') == []


def test_matching_and_filename_use_same_cleaning(configured):
    source = metadata.with_files({'title': 'The Book (Unabridged)', 'author': 'Writer', 'duration': 3600}, [{'relative': 'The Book - Writer (Full Cast).m4b'}])
    candidate = {'title': 'The Book', 'author': 'Writer', 'duration': 3600, 'provider': 'Audible'}
    assert metadata.search_terms(source) == ('The Book', 'Writer')
    assert metadata.score(source, candidate)['strong_match']
    assert metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)
    assert source['title'] == 'The Book (Unabridged)'


def test_conflicting_editions_still_block(configured):
    source = {'title': 'The Book (Abridged)', 'author': 'Writer', 'duration': 3600}
    candidate = dict(source, title='The Book (Unabridged)', provider='Audible')
    assert 'abridgement' in metadata.score(source, candidate)['conflicts']
    assert not metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)


def test_settings_persist_and_existing_reviews_rescore(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    job = state.job(job_id)
    job['body']['embedded']['title'] += ' (Custom Edition)'
    state.update_job(job_id, 'REVIEW', job['body'])
    with TestClient(app) as client:
        assert client.put('/api/settings', json={'ignored_title_terms': [' Custom Edition ', 'custom edition']}).status_code == 200
        assert state.settings().ignored_title_terms == ['custom edition']
        result = client.get(f'/api/jobs/{job_id}').json()
        assert result['body']['candidates'][0]['confidence']['strong_match']
        assert 'ignored configured title labels' in result['body']['candidates'][0]['confidence']['signals'][0]['reason']
        assert state.job(job_id)['status'] == 'REVIEW'
        assert client.put('/api/settings', json={'ignored_title_terms': []}).status_code == 200
        assert not client.get(f'/api/jobs/{job_id}').json()['body']['candidates'][0]['confidence']['strong_match']
        assert client.put('/api/settings', json={'ignored_title_terms': ['x' * 81]}).status_code == 400
