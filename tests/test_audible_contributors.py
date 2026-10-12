import copy
import json
from pathlib import Path

import httpx
import pytest

from app import metadata


@pytest.fixture
def catalog():
    # Reduced public api.audible.com response, fetched 2026-10-11; no credentials.
    return json.loads((Path(__file__).parent / 'fixtures' / 'audible_sixth_faction.json').read_text(encoding='utf-8'))


def test_translation_filters_only_labeled_authors(catalog):
    original = copy.deepcopy(catalog)
    candidate = metadata.audible_product(catalog['products'][1])
    assert candidate['author'] == 'Veronica Roth'
    assert candidate['narrator'] == 'Janin Stenzel; Jacob Weigert; Nils Nelleßen'
    assert candidate['language'] == 'german'
    assert candidate['duration'] == 59100
    assert catalog == original  # Provider payload is not modified.


def test_multiple_authors_and_ambiguous_names_are_retained(catalog):
    product = catalog['products'][0]
    product['authors'] = [
        {'name': 'Veronica Roth'}, {'name': 'Actual Coauthor'},
        {'name': 'Teresa Englert'},  # Same person, but no role evidence: keep.
        {'name': 'A Translator'}, {'name': 'Writer - editor'},
        {'name': 'Translator - A Novel'},
    ]
    assert metadata.audible_product(product)['author'] == (
        'Veronica Roth; Actual Coauthor; Teresa Englert; A Translator; '
        'Writer - editor; Translator - A Novel')
    product.pop('authors')
    assert metadata.audible_product(product)['author'] == ''


@pytest.mark.parametrize('label', [' - translator', ' – Translator', ' — TRANSLATOR  '])
def test_explicit_terminal_role_labels_only(catalog, label):
    product = catalog['products'][0]
    product['authors'] = [{'name': 'Actual Coauthor'}, {'name': 'Contributor' + label}]
    product['narrators'] = [{'name': 'Narrator' + label}]
    candidate = metadata.audible_product(product)
    assert candidate['author'] == 'Actual Coauthor'
    assert candidate['narrator'] == 'Narrator' + label  # Never filter narrator entries.
    product['authors'] = [{'name': 'Contributor' + label}]
    assert metadata.audible_product(product)['author'] == ''  # No invented fallback author.


@pytest.mark.parametrize('direct', [False, True])
def test_search_and_direct_asin_share_contributor_mapping(catalog, monkeypatch, direct):
    real_client = httpx.Client
    calls = []

    def respond(request):
        calls.append(request)
        assert 'contributors' in request.url.params['response_groups']
        if direct:
            assert request.url.path.endswith('/B0HCW29JJZ')
            payload = {'product': catalog['products'][1]}
        else:
            assert request.url.params['title'] == 'The Sixth Faction'
            assert request.url.params['author'] == 'Veronica Roth'
            payload = catalog
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(metadata.httpx, 'Client', lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    candidates = metadata.search('Audible', 'The Sixth Faction', 'Veronica Roth',
                                 asin='B0HCW29JJZ' if direct else '')
    assert len(calls) == 1
    assert all(c['author'] == 'Veronica Roth' for c in candidates)
    assert candidates[-1]['narrator'] == 'Janin Stenzel; Jacob Weigert; Nils Nelleßen'
    if not direct:
        assert candidates[0]['asin'] == 'B0GFB7WK22'
        assert candidates[0]['narrator'] == 'Emma Love; Charlie Thurston; Max Meyers'


def test_scores_and_recommendations_still_require_correct_edition(catalog, configured):
    candidates = [metadata.audible_product(p, 'com') for p in catalog['products']]
    settings = configured.model_copy(update={'auto_approve': True})
    english, translated = candidates
    source = {k: english[k] for k in ('title', 'author', 'narrator', 'language', 'duration')}
    ranked = metadata.ranked(source, candidates)
    assert ranked[0]['asin'] == 'B0GFB7WK22'
    assert ranked[0]['confidence']['total'] == 95
    assert metadata.automation_decision(source, ranked, settings, True)['eligible']
    assert {'narrator', 'language'} <= set(translated['confidence']['conflicts'])
    assert not metadata.automation_decision(source, [translated], settings, True)['eligible']

    # Correct translated audio gets full author evidence, without weakening rules.
    source = {k: translated[k] for k in ('title', 'author', 'narrator', 'language', 'duration')}
    scoring = metadata.score(source, translated)
    assert scoring['strong_match'] and scoring['total'] == 95
    assert next(s['points'] for s in scoring['signals'] if s['field'] == 'author') == 25
    assert metadata.may_automate(source, [translated], settings, True)
    legacy = dict(translated, author='; '.join(p['name'] for p in catalog['products'][1]['authors']))
    assert not metadata.score(source, legacy)['strong_match']
    assert not metadata.may_automate(source, [legacy], settings, True)
    assert not metadata.may_automate(source, [translated, dict(translated, asin='B999999999')], settings, True)


def test_real_coauthors_remain_part_of_score(catalog, configured):
    product = catalog['products'][0]
    product['authors'].append({'name': 'Actual Coauthor'})
    candidate = metadata.audible_product(product, 'com')
    source = dict(title=candidate['title'], author='Veronica Roth; Actual Coauthor', duration=candidate['duration'], language=candidate['language'])
    assert metadata.score(source, candidate)['strong_match']
    assert metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)
    source['author'] = 'Veronica Roth'
    assert not metadata.score(source, candidate)['strong_match']
