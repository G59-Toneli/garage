"""What chunking must never do.

The failure this module exists to prevent is a torque figure separated from the fastener it applies
to, so most of these tests are about what stays *together*, not about how many chunks come out.
"""

from garage.chunking import chunk_document
from garage.ingest import chunk_corpus
from garage.corpus import FIXTURE_CORPUS

SPEC_TABLE = """# Manual

## Section 3.2 — Cylinder head, tightening specifications

| Fastener                    | Thread | Torque (N·m) |
| --------------------------- | ------ | ------------ |
| Cylinder head bolt, stage 1 | M11    | 41 |
| Camshaft bearing cap nut    | M8     | 23 |
"""

PROCEDURE = """## Section 4.1 — Procedure: removing the camshaft

1. Disconnect the battery negative lead.
2. Remove the camshaft cover and set the crankshaft to top dead centre.
3. On reassembly, tighten the bearing cap nuts to 23 N·m.
"""

PROSE = """## O que eu faria de novo

Refiz a suspensão antes de mexer no motor. Foi a ordem certa.

Comprei escape de catálogo achando que era plug and play.
"""


def _texts(markdown: str, kind: str | None = None) -> list[str]:
    chunks = chunk_document(markdown, doc_id="d", tier="A")
    return [chunk.text for chunk in chunks if kind is None or chunk.kind == kind]


def test_a_specification_row_is_never_cut_in_half():
    texts = _texts(SPEC_TABLE, "spec")

    assert len(texts) == 2
    # The fastener, its thread and its torque are in one chunk or the citation is a lie.
    assert "Fastener: Cylinder head bolt, stage 1" in texts[0]
    assert "Thread: M11" in texts[0]
    assert "Torque (N·m): 41" in texts[0]
    assert "Torque (N·m): 23" in texts[1]


def test_a_specification_chunk_carries_its_column_headings_and_section():
    text = _texts(SPEC_TABLE, "spec")[0]

    # A chunk reading `41` is unfindable and unreadable; retrieval scores this text on its own.
    assert text.startswith("Section 3.2 — Cylinder head, tightening specifications — ")
    assert "Torque (N·m)" in text


def test_a_table_separator_row_never_becomes_a_chunk():
    assert not any(set(text) <= set("|- :—") for text in _texts(SPEC_TABLE, "spec"))


def test_a_procedure_is_split_one_step_per_chunk():
    texts = _texts(PROCEDURE, "procedure")

    assert len(texts) == 3
    assert "step 1: Disconnect the battery negative lead." in texts[0]
    assert "23 N·m" in texts[2]


def test_prose_carries_the_previous_paragraph_as_overlap():
    texts = _texts(PROSE, "prose")

    assert len(texts) == 2
    # The second paragraph never names the subject of the first; the overlap is what keeps it.
    assert texts[1].startswith("Foi a ordem certa.")
    assert "escape de catálogo" in texts[1]


def test_overlap_does_not_cross_a_section_boundary():
    texts = _texts("## One\n\nFirst thing.\n\n## Two\n\nSecond thing.\n", "prose")

    assert texts == ["First thing.", "Second thing."]


def test_headings_do_not_become_chunks_of_their_own():
    assert all("Section 3.2" != text for text in _texts(SPEC_TABLE))


def test_every_chunk_carries_tier_section_and_kind():
    chunks = chunk_document(SPEC_TABLE + PROCEDURE, doc_id="svc", tier="A")

    assert chunks
    assert all(chunk.tier == "A" for chunk in chunks)
    assert all(chunk.section for chunk in chunks)
    assert {chunk.kind for chunk in chunks} == {"spec", "procedure"}


def test_page_markers_are_carried_onto_the_chunks_that_follow_them():
    chunks = chunk_document(
        "<!-- page: 12 -->\n## Torque\n\nA paragraph.\n\n<!-- page: 13 -->\n\nAnother paragraph.\n",
        doc_id="svc",
        tier="A",
    )

    assert [chunk.page for chunk in chunks] == [12, 13]


def test_an_unpaged_document_reports_no_page_rather_than_inventing_one():
    assert all(chunk.page is None for chunk in chunk_document(PROSE, doc_id="b", tier="B"))


def test_chunk_ids_are_deterministic_and_scoped_to_their_document():
    first = chunk_document(SPEC_TABLE, doc_id="svc", tier="A")
    again = chunk_document(SPEC_TABLE, doc_id="svc", tier="A")
    other = chunk_document(SPEC_TABLE, doc_id="owner", tier="A")

    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in again]
    assert first[0].chunk_id == "svc#0000"
    assert not {chunk.chunk_id for chunk in first} & {chunk.chunk_id for chunk in other}


def test_jargon_terms_are_detected_on_the_chunks_that_use_them():
    chunks = chunk_document(
        "## Motor\n\nFiz o **swap 250-S** e trabalhei o cabecote.\n", doc_id="f", tier="B"
    )

    assert chunks[0].jargon_terms == ("swap 250-S", "cabeçote")


def test_a_table_split_across_a_page_break_keeps_every_row_and_its_headings():
    chunks = chunk_document(
        "## Torque\n\n"
        "| Fastener | Torque (N·m) |\n| --- | --- |\n| Flywheel bolt | 63 |\n"
        "\n<!-- page: 42 -->\n"
        "| Sump bolt | 12 |\n",
        doc_id="svc",
        tier="A",
    )

    # The row after the break has no heading row of its own; consuming it as headings would delete
    # a specification outright — the failure structure-aware chunking exists to prevent.
    assert [chunk.page for chunk in chunks] == [None, 42]
    assert "Fastener: Sump bolt" in chunks[1].text
    assert "Torque (N·m): 12" in chunks[1].text


def test_overlap_does_not_put_the_previous_paragraph_s_jargon_on_this_chunk():
    chunks = chunk_document(
        "## Motor\n\nFiz o **swap 250-S** em 2019.\n\nDepois vendi o carro.\n", doc_id="b", tier="B"
    )

    # The overlap is context for retrieval, not a claim about what this chunk discusses. A term the
    # chunk does not talk about is a false positive on every query that filters by it.
    assert chunks[1].text.startswith("Fiz o **swap 250-S** em 2019.")
    assert chunks[1].jargon_terms == ()


def test_the_fixture_corpus_chunks_into_all_three_kinds_with_tiers_preserved():
    chunks = chunk_corpus(FIXTURE_CORPUS)

    assert {chunk.kind for chunk in chunks} == {"spec", "procedure", "prose"}
    assert {chunk.tier for chunk in chunks} == {"A", "B"}
    # The torque table of the service manual survives as one chunk per specification.
    torques = [chunk for chunk in chunks if "Torque (N·m): 77" in chunk.text]
    assert len(torques) == 1
    assert torques[0].kind == "spec"
    assert "Cylinder head bolt, stage 2" in torques[0].text
