"""Manual URL marketplaces do not change the automatic matching region."""
import httpx
import pytest
from fastapi.testclient import TestClient
from test_matching_review import seed

from app import metadata, state
from app.main import app
from app.selection import apply_candidate


@pytest.mark.parametrize('region', sorted(metadata.AUDIBLE_REGIONS))
@pytest.mark.parametrize('prefix', ['', 'www.'])
def test_url_marketplace_parser(region, prefix):
    url = f'https://{prefix}audible.{region}/pd/Six-Strikes-Audiobook/B0G2FHRFH1?overrideBaseCountry=true'
    assert metadata.audible_lookup(url, 'com') == ('B0G2FHRFH1', region, True)
    assert metadata.asin_from(url) == 'B0G2FHRFH1'


@pytest.mark.parametrize('url', [
    'https://audible.xyz/pd/B0G2FHRFH1',
    'https://audible.in.evil.example/pd/B0G2FHRFH1',
    'https://evil.audible.in/pd/B0G2FHRFH1',
    'https://audible.com@evil.example/pd/B0G2FHRFH1',
    'https://evil.example@audible.in/pd/B0G2FHRFH1',
    'https://audible.in:443/pd/B0G2FHRFH1',
    'https://audible.in/?asin=B0G2FHRFH1',
    'https://audible.in/pd/not-an-asin?asin=B0G2FHRFH1',
    'https://audible.in/redirect/B0G2FHRFH1',
    'https://audible.in/pd/title/B0G2FHRFH1/extra',
    'https://audible.in/pd/title%2Fextra/B0G2FHRFH1',
    'https://audible.in\\@evil.example/pd/B0G2FHRFH1',
    'https://audible.\nin/pd/B0G2FHRFH1',
    'https://audible.in/pd//B0G2FHRFH1',
    'https://audible.in/pd/title/../B0G2FHRFH1',
])
def test_unsupported_or_malformed_url_rejected(url):
    with pytest.raises(ValueError):
        metadata.audible_lookup(url)


def test_raw_identifier_keeps_configured_region():
    assert metadata.audible_lookup(' b0fm6gst96 ', 'com') == ('B0FM6GST96', 'com', False)
    assert metadata.audible_lookup('B0FM6GST96', 'in') == ('B0FM6GST96', 'in', False)


def mock_catalog(monkeypatch, respond):
    real_client = httpx.Client
    monkeypatch.setattr(metadata.httpx, 'Client', lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))


def test_manual_search_requests_actual_marketplace_and_preserves_provenance(configured, monkeypatch):
    _, job_id, _ = seed(configured, 'REVIEW')
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == 'api.audible.com':
            return httpx.Response(200, json={'product': {'asin': 'B0FM6GST96', 'title': ''}})
        return httpx.Response(200, json={'product': {
            'asin': request.url.path.rsplit('/', 1)[-1], 'title': 'Six Strikes',
            'authors': [{'name': 'Antonia Grave'}],
        }})

    mock_catalog(monkeypatch, respond)
    with TestClient(app) as client:
        for value, region in [('https://www.audible.in/pd/Six-Strikes-Audiobook/B0G2FHRFH1?ipRedirectOverride=true', 'in')]:
            result = client.post(f'/api/jobs/{job_id}/search', json={'asin': value})
            assert result.status_code == 200, result.text
            candidate = result.json()['body']['candidates'][0]
            assert calls[-1].url.host == f'api.audible.{region}'
            assert candidate['region'] == region
            assert calls[-1].url.path.endswith('/' + metadata.asin_from(value))
            assert client.post(f'/api/jobs/{job_id}/select', json={'index': 0}).status_code == 200
            assert state.job(job_id)['body']['selected_candidate']['region'] == region
            assert candidate['lookup_region'] == 'in'
            assert candidate['region_source'] == 'audible_url'
        result = client.post(f'/api/jobs/{job_id}/search', json={'asin': 'B0FM6GST96'})
        assert result.status_code == 400
        assert 'B0FM6GST96' in result.json()['detail'] and 'region COM' in result.json()['detail']
        assert calls[-1].url.host == 'api.audible.com'
        assert calls[-1].url.path.endswith('/B0FM6GST96')
    assert configured.audible_region == state.settings().audible_region == 'com'


@pytest.mark.parametrize('status,payload', [
    (404, {}), (200, {}), (200, {'product': None}), (200, {'product': {}}),
    (200, {'product': {'asin': 'B0FM6GST96', 'title': '  '}}),
    (200, {'product': {'title': '<br>'}}),
])
def test_direct_lookup_has_actionable_error_not_blank_candidate(configured, monkeypatch, status, payload):
    _, job_id, _ = seed(configured, 'REVIEW')
    before = state.job(job_id)['body']
    mock_catalog(monkeypatch, lambda request: httpx.Response(status, json=payload))
    with TestClient(app) as client:
        result = client.post(f'/api/jobs/{job_id}/search', json={'asin': 'B0FM6GST96'})
    assert result.status_code == 400
    detail = result.json()['detail']
    assert 'B0FM6GST96' in detail and 'region COM' in detail and 'Audible URL' in detail
    assert 'B0G2FHRFH1' not in detail and 'Six Strikes' not in detail
    assert state.job(job_id)['body'] == before


def test_audible_api_redirect_is_not_followed(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'https://api.audible.com/1.0/catalog/products/B0FM6GST96'})

    mock_catalog(monkeypatch, respond)
    with pytest.raises(httpx.HTTPStatusError):
        metadata.search('Audible', '', asin='https://audible.in/pd/B0G2FHRFH1')
    assert len(calls) == 1 and calls[0].url.host == 'api.audible.in'


def test_explicit_manual_region_warning_does_not_relax_automation(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    body = state.job(job_id)['body']
    candidate = dict(body['candidates'][0], region='in', region_source='audible_url', lookup_region='in')
    apply_candidate(body, candidate)
    assert metadata.approval_warnings(body, configured) == []
    settings = configured.model_copy(update={'auto_approve': True})
    decision = metadata.automation_decision(body['embedded'], [candidate], settings, True)
    assert not decision['eligible']
    assert any('configured Audible region' in reason for reason in decision['reasons'])
    for change in [{'lookup_region': 'de'}, {'region_source': ''}, {'region': None}]:
        body['selected_candidate'] = dict(candidate, **change)
        assert any('region evidence' in w for w in metadata.approval_warnings(body, configured))
    body['selected_candidate'] = dict(candidate)
    body['metadata']['author'] = 'Edited author'
    assert any('author' in w for w in metadata.approval_warnings(body, configured))


def test_explicit_region_warnings_through_get_and_save(configured):
    _, job_id, _ = seed(configured, 'REVIEW')
    body = state.job(job_id)['body']
    candidate = dict(body['candidates'][0], region='in', region_source='audible_url', lookup_region='in')
    apply_candidate(body, candidate)
    body['candidates'] = [candidate]
    state.update_job(job_id, 'REVIEW', body)
    with TestClient(app) as client:
        assert client.get(f'/api/jobs/{job_id}').json()['approval_warnings'] == []
        result = client.put(f'/api/jobs/{job_id}', json={'metadata': {}, 'grouping_confirmed': True})
        assert result.status_code == 200 and result.json()['approval_warnings'] == []
        client.put('/api/settings', json={'auto_approve': True})
        assert client.post(f'/api/jobs/{job_id}/auto-match', json={}).status_code == 409
