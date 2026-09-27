import pytest
from fastapi.testclient import TestClient

from app import metadata, state
from app.main import app
from test_matching_review import seed


def example(filename="Devil's Slide - Stacy Lynn Miller.mp3", title='Speakeasy Series'):
    source = metadata.with_files(dict(title=title, author='Stacy Lynn Miller', duration=38217), [{'relative': filename}])
    candidate = dict(title="Devil's Slide", author='Stacy Lynn Miller', duration=38160, provider='Audible')
    return source, candidate


@pytest.mark.parametrize('filename', ["Devil's Slide - Stacy Lynn Miller.mp3", "Stacy Lynn Miller - Devil’s Slide.m4b", "folder/Devil's Slide – Stacy Lynn Miller.mp3"])
def test_filename_rescues_series_album(configured, filename):
    source, candidate = example(filename)
    score = metadata.score(source, candidate)
    assert score['total'] == 95 and not score['conflict']
    assert 'filename match' in score['signals'][0]['reason']
    assert 'Speakeasy Series' in score['signals'][0]['reason']
    assert metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)
    assert source['title'] == 'Speakeasy Series'
    assert metadata.search_terms(source)[0] in ("Devil's Slide", 'Devil’s Slide')


def test_real_embedded_title_conflict_requires_review(configured):
    source, candidate = example(title='Another Actual Book')
    assert metadata.score(source, candidate)['conflict']
    assert not metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)


def test_filename_author_conflict_and_no_substring_bonus():
    source, candidate = example()
    source['author'] = 'Another Author'
    assert metadata.score(source, candidate)['conflict']
    source, candidate = example("Devil's Slide - Stacy Lynn Miller - sample.mp3")
    assert not metadata.score(source, candidate)['strong_match']


def test_full_filename_fallback_is_not_a_conflicting_book_title():
    source, candidate = example(title="Devil's Slide - Stacy Lynn Miller")
    assert metadata.score(source, candidate)['strong_match']
    assert metadata.search_terms(source)[0] == "Devil's Slide"


def test_multifile_tracks_do_not_supply_book_identity():
    source, candidate = example()
    source = metadata.with_files(source, [{'relative': "Devil's Slide - Stacy Lynn Miller.mp3"}, {'relative': 'Other Book.mp3'}])
    assert not metadata.score(source, candidate)['strong_match']


def test_existing_review_and_selection_use_same_filename_ranking(configured):
    _, job_id, path = seed(configured, 'REVIEW')
    job = state.job(job_id)
    source, candidate = example()
    source.pop('_filenames')
    body = job['body']
    body.update(embedded=source, metadata=source.copy(), candidates=[dict(candidate, title='Speakeasy Series'), candidate])
    body['files'][0]['relative'] = "Devil's Slide - Stacy Lynn Miller.mp3"
    state.update_job(job_id, 'REVIEW', body)
    with TestClient(app) as client:
        data = client.get(f'/api/jobs/{job_id}').json()
        assert data['body']['candidates'][0]['title'] == "Devil's Slide"
        assert data['body']['candidates'][0]['confidence']['total'] == 95
        assert client.post(f'/api/jobs/{job_id}/select', json={'index': 0}).status_code == 200
        assert state.job(job_id)['body']['metadata']['title'] == "Devil's Slide"
