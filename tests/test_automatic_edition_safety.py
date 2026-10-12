"""Future imports must not confuse a translation with the source edition."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from app import media, metadata, state, worker
from app.main import app
from app.selection import apply_candidate
from test_matching_review import seed
from test_pipeline import audio


def editions():
    catalog = json.loads((Path(__file__).parent / 'fixtures' / 'audible_sixth_faction.json').read_text('utf-8'))
    return [metadata.audible_product(p, 'com') for p in catalog['products']]


def test_translation_cannot_win_even_with_low_threshold(configured):
    english, german = editions()
    source = dict(title=english['title'], author='Veronica Roth', duration=english['duration'] + 18, language='eng')
    settings = configured.model_copy(update={'auto_approve': True, 'confidence_threshold': 1})
    ranked = metadata.ranked(source, [german, english])
    assert ranked[0]['asin'] == 'B0GFB7WK22'
    assert metadata.may_automate(source, ranked, settings, True)
    assert {'language', 'duration'} <= set(german['confidence']['conflicts'])
    assert not metadata.may_automate(source, [german], settings, True)


def test_known_identifier_eliminates_demonstrably_wrong_rival(configured):
    english, _ = editions()
    source = dict(english)
    settings = configured.model_copy(update={'auto_approve': True})
    assert metadata.may_automate(source, [dict(english, asin='B999999999'), english], settings, True)
    source['isbn'] = '9781234567890'
    assert not metadata.may_automate(source, [dict(english, isbn='')], settings, True)


@pytest.mark.parametrize('change', [{'language': ''}, {'language': 'und'}])
def test_filename_is_not_source_language_evidence(configured, change):
    english, german = editions()
    source = dict(title=english['title'], author='Veronica Roth', duration=english['duration'], **change)
    evidence = metadata.with_files(source, [{'relative': 'The Sixth Faction - Veronica Roth English.m4b'}])
    settings = configured.model_copy(update={'auto_approve': True, 'confidence_threshold': 1})
    assert not metadata.score(evidence, english)['strong_match']
    decision = metadata.automation_decision(evidence, [english, german], settings, True)
    assert not decision['eligible']
    assert any('language evidence' in reason for reason in decision['reasons'])
    assert source['language'] == change['language']


@pytest.mark.parametrize('region', [None, 'de'])
def test_only_verified_configured_region_can_automate(configured, region):
    english, _ = editions()
    source = dict(english)
    candidate = dict(english, region=region)
    assert not metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)


def test_source_file_disagreements_block(configured):
    english, _ = editions()
    evidence = metadata.with_files(english, [{'tags': {'language': 'eng'}}, {'tags': {'language': 'deu'}}])
    assert not metadata.may_automate(evidence, [english], configured.model_copy(update={'auto_approve': True}), True)


@pytest.mark.parametrize('known_language', [True, False])
def test_new_import_selects_only_proven_english_edition(configured, monkeypatch, known_language):
    english, german = editions()
    path = Path(configured.source_path) / 'The Sixth Faction - Veronica Roth English.m4b'
    path.write_bytes(b'test fixture; probe mocked, no real book')
    tags = {'album': english['title'], 'album_artist': english['author']}
    if known_language:
        tags['language'] = 'eng'
    monkeypatch.setattr(media, 'probe', lambda p: dict(duration=english['duration'] + 18, codec='aac',
                       sample_rate='44100', channels=2, profile='LC', tags=tags, chapters=[], cover=False))
    monkeypatch.setattr(metadata, 'search', lambda *a, **k: [german, english])
    settings = configured.model_copy(update={'auto_approve': True})
    worker.discover(settings)
    worker.inspect_pending(settings)
    with state.db() as c:
        job = state.unpack(c.execute('SELECT * FROM jobs').fetchone())
    assert job['status'] == ('READY' if known_language else 'REVIEW')
    assert job['body']['embedded']['language'] == ('eng' if known_language else '')
    if known_language:
        assert job['body']['metadata']['asin'] == 'B0GFB7WK22'
        assert job['body']['metadata']['author'] == 'Veronica Roth'
        assert job['body']['automatic_approval']
    else:
        assert job['body']['metadata']['asin'] == ''
        assert 'selected_candidate' not in job['body']


def test_language_comes_from_explicit_audio_stream_tag(tmp_path):
    path = tmp_path / 'English in filename is not evidence.m4b'
    audio(path)
    media.run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-c', 'copy',
               '-metadata:s:a:0', 'language=eng', str(tmp_path / 'tagged.m4b')])
    assert media.embedded([media.probe(tmp_path / 'tagged.m4b')], 'fallback')['language'] == 'eng'


def test_offline_discovery_is_review_not_package_error(configured, monkeypatch):
    path = Path(configured.source_path) / 'The Sixth Faction - Veronica Roth English.m4b'
    audio(path, title='The Sixth Faction', author='Veronica Roth')
    original = media.digest(path)
    def offline(*args, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(metadata, 'search', offline)
    settings = configured.model_copy(update={'auto_approve': True})
    worker.discover(settings)
    worker.inspect_pending(settings)
    with state.db() as c:
        job = state.unpack(c.execute('SELECT * FROM jobs').fetchone())
        assert c.execute('SELECT status FROM packages').fetchone()[0] == 'INSPECTED'
    assert job['status'] == 'REVIEW'
    assert job['body']['candidates'] == []
    assert job['body']['embedded']['language'] == ''
    assert 'ConnectError' in job['body']['lookup_error']
    assert worker.claim(settings) is None
    assert media.digest(path) == original


def test_automatic_publish_rechecks_evidence_but_manual_override_survives(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    body = state.job(job_id)['body']
    apply_candidate(body, body['candidates'][0], automatic=True)
    body['embedded']['language'] = 'german'
    body['force_now'] = True
    state.update_job(job_id, 'READY', body)
    settings = configured.model_copy(update={'auto_approve': True})
    assert worker.finalize_one(settings)
    assert state.job(job_id)['status'] == 'REVIEW'
    assert list(Path(settings.library_path).iterdir()) == []
    with TestClient(app) as client:
        response = client.get(f'/api/jobs/{job_id}').json()
        assert any('language' in warning for warning in response['approval_warnings'])
        assert client.post(f'/api/jobs/{job_id}/approve', json={}).status_code == 200
    assert not state.job(job_id)['body']['automatic_approval']


def test_save_returns_approval_warnings_for_publish_confirmation(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    with TestClient(app) as client:
        saved = client.put(f'/api/jobs/{job_id}', json={'metadata': {}, 'grouping_confirmed': True})
    assert saved.status_code == 200, saved.text
    warnings = saved.json()['approval_warnings']
    assert warnings and any('provenance' in warning for warning in warnings)


def test_actual_english_edition_writer_and_identifier_atoms(tmp_path):
    english, _ = editions()
    output = tmp_path / 'book.m4b'
    audio(output)
    media.write_tags(output, english, None)
    tags = MP4(output)
    assert tags['aART'] == tags['\xa9ART'] == ['Veronica Roth']
    assert tags['\xa9wrt'] == ['Emma Love; Charlie Thurston; Max Meyers']
    assert bytes(tags['----:com.apple.iTunes:ASIN'][0]) == b'B0GFB7WK22'
    assert bytes(tags['----:com.apple.iTunes:LANGUAGE'][0]) == b'english'
    source = media.embedded([media.probe(output)], 'fallback')
    assert source['author'] == 'Veronica Roth'
    assert source['narrator'] == english['narrator']
    assert source['asin'] == english['asin']
    assert source['language'] == english['language']
