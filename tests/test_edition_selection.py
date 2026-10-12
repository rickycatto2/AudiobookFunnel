from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import state
from app.main import app
from app.selection import apply_candidate
from test_matching_review import seed


@pytest.mark.parametrize('missing', ['isbn', 'asin', 'language'])
@pytest.mark.parametrize('empty', [None, ''])
def test_switch_clears_missing_edition_field(configured, missing, empty):
    _, job_id, _ = seed(configured, 'REVIEW')
    body = state.job(job_id)['body']
    body['candidates'][0].update(isbn='9781111111111', asin='B111111111', language='english')
    state.update_job(job_id, 'REVIEW', body)
    with TestClient(app) as client:
        assert client.post(f'/api/jobs/{job_id}/select', json={'index': 0}).status_code == 200
        body = state.job(job_id)['body']
        body['candidates'][0].update(isbn='9782222222222', asin='B222222222', language='french')
        if empty is None:
            body['candidates'][0].pop(missing)
        else:
            body['candidates'][0][missing] = empty
        state.update_job(job_id, 'REVIEW', body)
        result = client.post(f'/api/jobs/{job_id}/select', json={'index': 0})
        assert result.status_code == 200
        assert result.json()['body']['metadata'][missing] == ''
        assert result.json()['body']['provenance'][missing] == 'Audible (not supplied)'


@pytest.mark.parametrize('action', ['select', 'auto-match'])
@pytest.mark.parametrize('confirmed', [False, True])
def test_manual_identifier_requires_confirmation_for_this_match(configured, action, confirmed):
    _, job_id, _ = seed(configured, 'REVIEW')
    with TestClient(app) as client:
        client.put('/api/settings', json={'auto_approve': True})
        assert client.put(f'/api/jobs/{job_id}', json={
            'metadata': {'isbn': '9783333333333', 'language': 'french', 'asin': 'B333333333'},
            'grouping_confirmed': True,
        }).status_code == 200
        payload = {'index': 0} if action == 'select' else {}
        if confirmed:
            payload['keep_manual_fields'] = ['isbn', 'asin', 'language']
        result = client.post(f'/api/jobs/{job_id}/{action}', json=payload)
        assert result.status_code == 200, result.text
        body = state.job(job_id)['body']
        assert body['metadata']['isbn'] == ('9783333333333' if confirmed else '')
        assert body['metadata']['language'] == ('french' if confirmed else 'english')
        assert body['metadata']['asin'] == ('B333333333' if confirmed else 'B012345678')
        assert body['provenance']['isbn'] == ('Manual' if confirmed else 'Audible (not supplied)')
        if confirmed and action == 'select':
            # A previous confirmation is not reusable for a later selection.
            assert client.post(f'/api/jobs/{job_id}/select', json={'index': 0}).status_code == 200
            assert state.job(job_id)['body']['metadata']['isbn'] == ''


def test_old_review_remains_unchanged_until_explicit_rematch(configured):
    _, job_id, path = seed(configured, 'REVIEW')
    body = state.job(job_id)['body']
    body['metadata'].update(isbn='stale-isbn', asin='stale-asin', language='stale-language')
    body['provenance'].update(isbn='Google Books', asin='Manual', language='Embedded / filename')
    state.update_job(job_id, 'REVIEW', body)
    before = deepcopy(state.job(job_id)['body'])
    state.init()
    with TestClient(app) as client:
        client.get('/api/jobs')
        client.get(f'/api/jobs/{job_id}')
        assert state.job(job_id)['body'] == before
        client.put('/api/settings', json={'auto_approve': True})
        assert client.post(f'/api/jobs/{job_id}/auto-match', json={}).status_code == 200
    after = state.job(job_id)['body']
    assert after['metadata']['isbn'] == ''
    assert after['metadata']['language'] == 'english'
    assert after['metadata']['asin'] == 'B012345678'
    assert after['embedded'] == before['embedded']
    assert path.read_bytes() == b'source stays untouched'


def test_cannot_keep_unreviewed_provider_field(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    before = state.job(job_id)['body']
    with TestClient(app) as client:
        result = client.post(f'/api/jobs/{job_id}/select', json={'index': 0, 'keep_manual_fields': ['asin']})
        assert result.status_code == 400
    assert state.job(job_id)['body'] == before


def test_manual_edit_after_selection_is_deliberate(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    with TestClient(app) as client:
        assert client.post(f'/api/jobs/{job_id}/select', json={'index': 0}).status_code == 200
        result = client.put(f'/api/jobs/{job_id}', json={
            'metadata': {'isbn': '9784444444444', 'asin': '', 'language': 'eng'},
            'grouping_confirmed': True,
        })
        assert result.status_code == 200
        body = state.job(job_id)['body']
        assert body['metadata']['isbn'] == '9784444444444'
        assert body['metadata']['asin'] == ''
        assert body['metadata']['language'] == 'eng'
        assert all(body['provenance'][field] == 'Manual' for field in ('isbn', 'asin', 'language'))


@pytest.mark.parametrize('automatic', [False, True])
def test_contributor_strings_are_not_reinterpreted(automatic):
    authors = 'Author One, Author Two'
    candidate = {'provider': 'Audible', 'author': authors, 'narrator': 'Narrator One, Narrator Two',
                 'contributors': [{'name': 'Translator Name', 'role': 'translator'}]}
    body = {'metadata': {'author': 'Old Author', 'isbn': 'old'}, 'provenance': {}}
    apply_candidate(body, candidate, automatic=automatic)
    assert body['metadata']['author'] == authors
    assert body['metadata']['narrator'] == candidate['narrator']
    assert 'Translator Name' not in body['metadata']['author']
    assert candidate['contributors'][0]['role'] == 'translator'
