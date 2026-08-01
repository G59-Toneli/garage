"""Structure-aware chunking: turning a document into retrievable units without breaking them.

A chunker that splits on a character budget will eventually cut a torque figure away from the
fastener it applies to. The answer is then wrong in the worst possible way — confidently, with a
citation. Everything in this module exists to make that cut impossible:

- a **specification table** is split one row per chunk, and every chunk carries its column headings,
  so the number never travels without the thing it describes;
- a **procedure** is split one step per chunk, because a step is the smallest unit that is still an
  instruction;
- **prose** is split per paragraph, with the previous paragraph's last sentence carried in as
  overlap, because a paragraph's subject is often named only in the one before it.

Chunking is deterministic: the same bytes produce the same `chunk_id`s in the same order. That is
what lets an evaluation set point at a `chunk_id` and still mean something after a rebuild
(ADR-0002). `INGEST_VERSION` is the escape hatch — change the rules here and it must go up, because
a stored chunk built by the old rules is not the chunk this code would produce.

The input format is Markdown. Real Tier A material arrives as PDF; the extraction step that turns a
PDF into this Markdown-with-page-markers shape lands with the real corpus, and everything below is
written against that intermediate form rather than against any particular file format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from garage.jargon import JargonTerm, detect

# Goes up whenever these rules change — including an edit to `corpus/jargon.yaml`, whose detected
# terms are stored on the chunk (ADR-0007). Stored alongside the corpus hash so a database can be
# recognised as built by rules this code no longer implements.
INGEST_VERSION = 1

ChunkKind = Literal["spec", "procedure", "prose"]

# `<!-- page: 12 -->` — the page the following content came from. Markdown has no pages; a document
# extracted from a scanned manual does, and citations are worth much less without them. Absent
# marker means an unpaged document, and `page` stays None rather than being invented.
_PAGE_MARKER = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_ORDERED_STEP = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_THEMATIC_BREAK = re.compile(r"^([-*_])\1{2,}$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit, carrying everything a citation and a tier filter will need."""

    chunk_id: str
    doc_id: str
    ordinal: int
    tier: str
    page: int | None
    section: str | None
    kind: ChunkKind
    text: str
    jargon_terms: tuple[str, ...]


@dataclass(frozen=True)
class _Block:
    """A run of lines that belongs together, tagged with where in the document it was found."""

    kind: ChunkKind
    lines: tuple[str, ...]
    section: str | None
    page: int | None


def _split_row(line: str) -> tuple[str, ...]:
    """Cells of a Markdown table row, outer pipes discarded."""
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _blocks(markdown: str) -> list[_Block]:
    """Group lines into typed blocks, tracking the current section and page as we go.

    One pass, because block type is decided by how a line starts and blocks never nest in the
    material this handles. Anything unrecognised falls through to prose, so a new formatting habit
    in a source degrades to a paragraph rather than disappearing.
    """
    blocks: list[_Block] = []
    section: str | None = None
    page: int | None = None
    pending: list[str] = []
    pending_kind: ChunkKind = "prose"

    def flush() -> None:
        nonlocal pending
        if pending:
            blocks.append(_Block(pending_kind, tuple(pending), section, page))
            pending = []

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        marker = _PAGE_MARKER.match(stripped)
        if marker:
            flush()
            page = int(marker.group(1))
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            # The heading itself is not a chunk: on its own it retrieves nothing and answers
            # nothing. It becomes the `section` every chunk under it carries.
            section = heading.group(2)
            continue

        if not stripped or _THEMATIC_BREAK.match(stripped):
            flush()
            continue

        kind: ChunkKind
        if stripped.startswith("|"):
            kind = "spec"
        elif _ORDERED_STEP.match(stripped):
            kind = "procedure"
        elif pending_kind == "procedure" and pending and line.startswith((" ", "\t")):
            # A wrapped continuation of the step above, not a new paragraph.
            kind = "procedure"
        else:
            kind = "prose"

        if kind != pending_kind:
            flush()
            pending_kind = kind
        pending.append(line)

    flush()
    return blocks


def _spec_texts(block: _Block, carried: tuple[str, ...]) -> tuple[list[str], tuple[str, ...]]:
    """One text per data row: `Section — Heading: cell; Heading: cell`, plus the headings used.

    The heading row is repeated into every chunk rather than stored once, because retrieval scores a
    chunk on its own text. A row reading `41` is not findable; `Torque (N·m): 41` is.

    A table only owns a heading row if a `|---|` separator says so. Long tables in real manuals run
    across a page break, and the rows after the break carry no headings of their own — treating the
    first of them as headings would silently eat a specification, which is the one thing this module
    exists to prevent. So `carried` holds the previous table's headings for exactly that case.
    """
    lines = [line.strip() for line in block.lines]
    has_own_headers = len(lines) > 1 and bool(_TABLE_SEPARATOR.match(lines[1]))
    rows = [line for line in lines if not _TABLE_SEPARATOR.match(line)]
    if not rows:
        return [], carried

    headers = _split_row(rows[0]) if has_own_headers else carried
    texts: list[str] = []
    for row in rows[1:] if has_own_headers else rows:
        cells = _split_row(row)
        if not any(cells):
            continue
        pairs = [
            f"{headers[index]}: {cell}" if index < len(headers) and headers[index] else cell
            for index, cell in enumerate(cells)
        ]
        body = "; ".join(pair for pair in pairs if pair)
        texts.append(f"{block.section} — {body}" if block.section else body)
    return texts, headers


def _procedure_texts(block: _Block) -> list[str]:
    """One text per numbered step, wrapped continuation lines folded back in."""
    steps: list[list[str]] = []
    for line in block.lines:
        step = _ORDERED_STEP.match(line.strip())
        if step:
            steps.append([f"step {step.group(1)}: {step.group(2)}"])
        elif steps:
            steps[-1].append(line.strip())

    texts = [" ".join(part for part in step if part) for step in steps]
    if block.section:
        texts = [f"{block.section} — {text}" for text in texts]
    return texts


def _paragraph(block: _Block) -> str:
    """The block's own text, wrapped lines rejoined. Blank lines already ended it, so it is one."""
    return " ".join(line.strip() for line in block.lines).strip()


def _last_sentence(text: str) -> str:
    """The overlap carried into the next paragraph.

    One sentence rather than a character window: a sentence is the smallest unit that still names
    its subject, which is the whole reason the overlap exists — the paragraph explaining what to do
    about the tensioner rarely repeats the word `tensionador`.
    """
    sentences = [sentence for sentence in _SENTENCE_END.split(text) if sentence.strip()]
    return sentences[-1] if sentences else text


def chunk_document(
    markdown: str,
    *,
    doc_id: str,
    tier: str,
    vocabulary: tuple[JargonTerm, ...] | None = None,
) -> tuple[Chunk, ...]:
    """Split one document into chunks, in document order.

    `chunk_id` is `<doc_id>#<ordinal>`: derived from position, not from content, so a chunk keeps
    its identity when a typo is fixed three paragraphs above it, and so two documents can never
    collide. Rebuild determinism is what makes that safe — see `INGEST_VERSION`.

    `vocabulary` is passed in rather than reached for, so a chunk is a function of its inputs alone.
    Jargon is curated separately from any Corpus (`corpus/jargon.yaml`) and the terms it detects are
    stored on the chunk, so which vocabulary was used is part of what produced this output.
    """
    chunks: list[Chunk] = []
    prose_run: list[_Block] = []
    headers: tuple[str, ...] = ()
    section: str | None = None

    def emit(kind: ChunkKind, texts: list[str], source: _Block, *, own: list[str] | None = None) -> None:
        # `own` is the chunk's own words where `text` also carries overlap. Jargon is detected on the
        # former: a term the chunk does not actually discuss is a retrieval false positive, and a
        # false positive pollutes every chunk that carries it (see `jargon.detect`).
        for text, subject in zip(texts, own if own is not None else texts):
            ordinal = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#{ordinal:04d}",
                    doc_id=doc_id,
                    ordinal=ordinal,
                    tier=tier,
                    page=source.page,
                    section=source.section,
                    kind=kind,
                    text=text,
                    jargon_terms=detect(subject, vocabulary),
                )
            )

    def flush_prose() -> None:
        nonlocal prose_run
        carried: str | None = None
        for block in prose_run:
            paragraph = _paragraph(block)
            if not paragraph:
                continue
            emit(
                "prose",
                [f"{carried} {paragraph}" if carried else paragraph],
                block,
                own=[paragraph],
            )
            carried = _last_sentence(paragraph)
        prose_run = []

    for block in _blocks(markdown):
        if block.kind == "prose":
            # Overlap runs across consecutive paragraphs but stops at a heading or a page break:
            # text on the far side of a section boundary is not context, it is a different subject.
            if prose_run and (
                prose_run[-1].section != block.section or prose_run[-1].page != block.page
            ):
                flush_prose()
            prose_run.append(block)
            continue

        flush_prose()
        if block.kind == "spec":
            # Headings carry into a continuation table only within the same section: a page break
            # splits one table, a new heading starts a different one.
            texts, headers = _spec_texts(block, headers if block.section == section else ())
            section = block.section
        else:
            texts = _procedure_texts(block)
        emit(block.kind, texts, block)

    flush_prose()
    return tuple(chunks)
