import pytest

from app import metadata


@pytest.mark.parametrize('candidate_author', [
    'A Very Long Established Author Name',
    'A Very Long Established Author Name; Ann',
])
def test_fuzzy_author_disagreement_never_auto_approves(configured, candidate_author):
    source = dict(title='Book', author='A Very Long Established Author Name; Ana', narrator='Reader',
                  series='Series', series_number='1', duration=36000, asin='B012345678')
    candidate = dict(source, author=candidate_author, provider='Audible')
    settings = configured.model_copy(update={'auto_approve': True, 'confidence_threshold': 1})
    assert 'author' in metadata.score(source, candidate)['conflicts']
    assert not metadata.may_automate(source, [candidate], settings, True)


def test_explicit_translator_mapping_does_not_erase_ambiguous_roles(configured):
    product = dict(title='Book', authors=[{'name': 'Author One'}, {'name': 'Author Two'},
                   {'name': 'Translator Person - translator'}, {'name': 'Other Person - adaptation'}],
                   narrators=[{'name': 'Narrator One'}, {'name': 'Narrator Two'}], runtime_length_min=600)
    candidate = metadata.audible_product(product)
    assert candidate['author'] == 'Author One; Author Two; Other Person - adaptation'
    assert candidate['narrator'] == 'Narrator One; Narrator Two'


def test_filename_cannot_hide_a_known_extra_author(configured):
    author = 'A Very Long Established Author Name'
    source = dict(title='Book', author=author + '; Ana', narrator='Reader', duration=36000,
                  _filenames=[f'Book - {author}.m4b'])
    candidate = dict(title='Book', author=author, narrator='Reader', duration=36000, provider='Audible')
    assert 'filename vs embedded author' in metadata.score(source, candidate)['conflicts']
    assert not metadata.may_automate(source, [candidate], configured.model_copy(update={'auto_approve': True}), True)
