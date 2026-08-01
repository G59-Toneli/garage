"""Detection has to survive how people actually write.

Forum Portuguese drops accents and inflects freely. Matching only the form the vocabulary happens to
spell would find the manual and miss the thread that needed the help.
"""

import pytest

from garage.jargon import JargonError, JargonTerm, detect, load_vocabulary

VOCABULARY = (
    JargonTerm(term="swap 250-S", canonical="engine transplant", notes=""),
    JargonTerm(term="cabeçote", canonical="cylinder head", notes=""),
    JargonTerm(term="mesa", canonical="engine mounting cradle", notes=""),
)


def test_detection_ignores_case_and_missing_accents():
    assert detect("Trabalhei o CABECOTE", VOCABULARY) == ("cabeçote",)


def test_a_multi_word_term_is_detected_inside_markup():
    assert detect("Fiz o **swap 250-S** ano passado.", VOCABULARY) == ("swap 250-S",)


def test_a_term_is_not_detected_inside_a_longer_word():
    assert detect("Recebi a mesada e comprei um cabeçotex", VOCABULARY) == ()


def test_terms_come_back_in_vocabulary_order_without_duplicates():
    text = "mesa nova, cabeçote novo, e outra mesa"

    assert detect(text, VOCABULARY) == ("cabeçote", "mesa")


def test_text_with_no_jargon_reports_none():
    assert detect("Cold inflation pressure is 28 psi.", VOCABULARY) == ()


def test_the_curated_vocabulary_loads_and_covers_the_fixture_jargon():
    terms = {term.term for term in load_vocabulary()}

    # The two terms CONTEXT.md names as the reason Jargon exists at all.
    assert {"swap 250-S", "projetinho de rua"} <= terms
    assert all(term.canonical for term in load_vocabulary())


def test_a_missing_vocabulary_is_an_error_not_an_empty_list(tmp_path):
    with pytest.raises(JargonError):
        load_vocabulary(tmp_path / "absent.yaml")
