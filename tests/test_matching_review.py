import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import metadata, state, worker
from app.main import app


def pair(title='Five Found Dead', duration=28218, narrator='Katherine Littrell; Eden Gabay'):
    source = dict(title=title, author='Sulari Gentill', duration=duration, narrator=narrator)
    candidate = dict(source, duration=28200, provider='Audible', asin='B012345678')
    return source, candidate


@pytest.mark.parametrize('duration,provider_duration,narrator', [(46155, 46140, ''), (28218, 28200, 'Katherine Littrell; Eden Gabay')])
def test_screenshot_matches_pass(configured, duration, provider_duration, narrator):
    source, candidate = pair(duration=duration, narrator=narrator)
    candidate['duration'] = provider_duration
    candidates = metadata.ranked(source, [candidate])
    settings = configured.model_copy(update={'auto_approve': True})
    assert candidates[0]['confidence']['total'] == 95
    assert metadata.may_automate(source, candidates, settings, True)
    assert not metadata.may_automate(source, candidates, settings, False)
    assert not metadata.may_automate(source, candidates, configured, True)


@pytest.mark.parametrize('field,value', [('asin', 'B999999999'), ('narrator', 'Different Narrator'), ('series', 'Different Series'), ('series_number', '2'), ('language', 'german')])
def test_contradictions_block_even_at_low_threshold(configured, field, value):
    source, candidate = pair()
    source.update(asin='B012345678', series='Series', series_number='1', language='english')
    candidate.update(source, provider='Audible')
    candidate[field] = value
    settings = configured.model_copy(update={'auto_approve': True, 'confidence_threshold': 1})
    assert not metadata.may_automate(source, [candidate], settings, True)


def test_competing_editions_and_duplicate_asins(configured):
    source, candidate = pair()
    settings = configured.model_copy(update={'auto_approve': True})
    assert not metadata.may_automate(source, [candidate, dict(candidate, asin='B987654321')], settings, True)
    assert metadata.may_automate(source, [candidate, candidate.copy()], settings, True)
    assert not metadata.may_automate(source, [dict(candidate, provider='Google Books')], settings, True)


@pytest.mark.parametrize('change', [{'duration': 29000}, {'duration': 0}, {'author': 'Another Writer'}, {'title': 'Five Found Alive'}])
def test_weak_core_does_not_get_bonus(configured, change):
    source, candidate = pair(narrator='')
    candidate.update(change)
    assert not metadata.score(source, candidate)['strong_match']
    assert not metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)


def seed(settings, status='ERROR'):
    source, candidate = pair()
    path = Path(settings.source_path) / 'book.m4b'
    path.write_bytes(b'source stays untouched')
    package_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = dict(embedded=source, metadata=source.copy(), candidates=[candidate], grouping_confirmed=True,
                files=[{'path': str(path)}], provenance={}, covers=[], cover_choice='embedded')
    with state.db() as c:
        c.execute('INSERT INTO packages VALUES (?,?,?,?,?)', (package_id, str(path), 'INSPECTED', None, time.time()))
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (job_id, package_id, status, json.dumps(body), 'Missing source', None, time.time()))
    return package_id, job_id, path


def test_dismiss_restore_preserves_error_files_and_dedup(configured):
    package, job, path = seed(configured)
    with TestClient(app) as client:
        assert client.post(f'/api/jobs/{job}/dismiss', json={}).status_code == 200
        assert state.job(job)['status'] == 'DISMISSED'
        assert state.job(job)['error'] == 'Missing source'
        assert path.read_bytes() == b'source stays untouched'
        worker.discover(configured)
        with state.db() as c:
            assert c.execute('SELECT count(*) FROM packages').fetchone()[0] == 1
        assert worker.claim(configured) is None
        assert client.post(f'/api/jobs/{job}/restore', json={}).status_code == 200
        assert state.job(job)['status'] == 'ERROR'
        state.update_job(job, 'PROCESSING')
        assert client.post(f'/api/jobs/{job}/dismiss', json={}).status_code == 409


def test_old_review_is_rescored_and_explicitly_queued(configured):
    _, job, path = seed(configured, 'REVIEW')
    with TestClient(app) as client:
        client.put('/api/settings', json={'auto_approve': True})
        result = client.get(f'/api/jobs/{job}').json()
        assert result['automation']['eligible']
        assert result['body']['candidates'][0]['confidence']['total'] == 95
        assert state.job(job)['status'] == 'REVIEW'  # merely viewing never approves
        assert client.post(f'/api/jobs/{job}/auto-match', json={}).status_code == 200
        assert state.job(job)['status'] == 'READY'
        assert state.job(job)['body']['metadata']['asin'] == 'B012345678'
        assert not state.job(job)['body']['force_now']


def test_package_error_dismiss_restore(configured):
    package, _, _ = seed(configured)
    with state.db() as c:
        c.execute("UPDATE packages SET status='ERROR',error='File missing' WHERE id=?", (package,))
    with TestClient(app) as client:
        assert client.post(f'/api/packages/{package}/dismiss', json={}).status_code == 200
        assert client.post(f'/api/packages/{package}/retry', json={}).status_code == 200
        with state.db() as c:
            assert c.execute('SELECT status FROM packages WHERE id=?', (package,)).fetchone()[0] == 'DISMISSED'
        assert client.post(f'/api/packages/{package}/restore', json={}).status_code == 200
