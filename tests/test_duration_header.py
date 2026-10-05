import json

import pytest

from app import media, state, worker
from test_pipeline import import_book


def test_capped_header_uses_verified_packet_timing(monkeypatch):
    info = {'format': {'duration': '48695.774331'}, 'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'duration_ts': 2 ** 31, 'nb_frames': '2', 'time_base': '1/44100'}]}
    monkeypatch.setattr(media, 'run', lambda args, **kwargs: '0,1000000000\n1000000000,1272613376\n' if '-show_packets' in args else json.dumps(info))
    result = media.probe('fixture.m4b')
    assert result['duration'] == pytest.approx(51533.183129)
    assert '2^31' in result['duration_note']


def test_capped_header_does_not_mask_missing_packets(monkeypatch):
    info = {'format': {'duration': '48695.774331'}, 'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'duration_ts': 2 ** 31, 'nb_frames': '3', 'time_base': '1/44100'}]}
    monkeypatch.setattr(media, 'run', lambda args, **kwargs: '0,1000000000\n1000000000,1272613376\n' if '-show_packets' in args else json.dumps(info))
    with pytest.raises(ValueError, match='packet count'):
        media.probe('fixture.m4b')


@pytest.mark.parametrize('packets', ['0,1000\n2000,1000\n2000,1000\n', '0,1000\n2000,1000\n', '0,1000\n500,1000\n'])
def test_gaps_and_overlaps_cannot_cancel(monkeypatch, packets):
    info = {'format': {'duration': '48695.774331'}, 'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'duration_ts': 2 ** 31, 'time_base': '1/44100'}]}
    monkeypatch.setattr(media, 'run', lambda args, **kwargs: packets if '-show_packets' in args else json.dumps(info))
    with pytest.raises(ValueError, match='discontinuous'):
        media.probe('fixture.m4b')


def test_normal_aac_does_not_scan_packets(monkeypatch):
    info = {'format': {'duration': '10'}, 'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'duration_ts': 441000, 'time_base': '1/44100'}]}
    def run(args, **kwargs):
        assert '-show_packets' not in args
        return json.dumps(info)
    monkeypatch.setattr(media, 'run', run)
    assert media.probe('fixture.m4b')['duration'] == 10


@pytest.mark.parametrize('packets', ['nan,1000\n', '0,0\n', '0,-1000\n'])
def test_invalid_packet_timings_are_rejected(monkeypatch, packets):
    info = {'format': {'duration': '48695.774331'}, 'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'duration_ts': 2 ** 31, 'time_base': '1/44100'}]}
    monkeypatch.setattr(media, 'run', lambda args, **kwargs: packets if '-show_packets' in args else json.dumps(info))
    with pytest.raises(ValueError):
        media.probe('fixture.m4b')


def test_old_job_can_recover_verified_duration_without_encoding(configured, monkeypatch):
    src, job = import_book(configured, monkeypatch)
    original = media.digest(src)
    true_duration = job['body']['files'][0]['duration']
    job['body']['files'][0]['duration'] = .1
    job['body']['grouping_confirmed'] = True
    probe = media.probe
    def corrected(path):
        result = probe(path)
        if path.name.startswith('input-'):
            result['duration_note'] = 'Verified fixture duration correction'
        return result
    monkeypatch.setattr(media, 'probe', corrected)
    state.update_job(job['id'], 'READY', job['body'])
    worker.finalize_one(configured)
    saved = state.job(job['id'])
    assert saved['status'] == 'COMPLETE', saved['error']
    assert saved['body']['files'][0]['duration'] == true_duration
    assert saved['body']['files'][0]['original_duration'] == .1
    assert saved['body']['metadata']['duration'] == true_duration
    assert media.digest(src) == original
