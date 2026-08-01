"""The Jargon vocabulary: workshop terms and what formal text calls the same thing.

Jargon is the gap between how a person asks and how a manual writes (CONTEXT.md). Two things live
here: the curated vocabulary itself (`corpus/jargon.yaml`), and detection — finding which terms a
piece of text actually uses, so a chunk can be filtered and expanded on later.

Detection is deliberately conservative. It matches terms, not concepts: `cabeçote` matches
`Cabeçote` and `cabecote`, but nothing here guesses that `cabeça` was meant. A false positive
pollutes every chunk that carries it; a false negative costs one recall opportunity a query
expansion can still recover.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

JARGON_VOCABULARY = Path(__file__).resolve().parents[2] / "corpus" / "jargon.yaml"


class JargonError(Exception):
    """The vocabulary file is missing or malformed."""


class JargonTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    notes: str = ""


@dataclass(frozen=True)
class _Matcher:
    """A vocabulary compiled once into the regex that finds it."""

    terms: tuple[JargonTerm, ...]
    pattern: re.Pattern[str]
    by_fold: dict[str, str]


def _fold(text: str) -> str:
    """Casefold and strip accents, so `Cabeçote` and `cabecote` are the same term.

    Brazilian workshop writing drops accents constantly — forum posts especially. Matching on the
    accented form alone would find the manual and miss the thread that needed the help.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def load_vocabulary(path: Path | None = None) -> tuple[JargonTerm, ...]:
    """Read the curated vocabulary. Raises `JargonError` rather than returning a partial list."""
    path = Path(path) if path is not None else JARGON_VOCABULARY
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise JargonError(f"no jargon vocabulary at {path}") from missing
    except yaml.YAMLError as broken:
        raise JargonError(f"{path} is not valid YAML: {broken}") from broken

    if not isinstance(raw, list) or not raw:
        raise JargonError(f"{path} must be a non-empty list of terms")

    try:
        terms = tuple(JargonTerm.model_validate(entry) for entry in raw)
    except ValidationError as invalid:
        raise JargonError(f"{path} does not satisfy the jargon schema:\n{invalid}") from invalid

    seen = {term.term for term in terms}
    if len(seen) != len(terms):
        raise JargonError(f"{path} lists the same term twice")
    return terms


def _compile(terms: tuple[JargonTerm, ...]) -> _Matcher:
    # Longest first: `swap 250-S` must win over any shorter term nested inside it.
    ordered = sorted(terms, key=lambda term: len(term.term), reverse=True)
    alternatives = "|".join(re.escape(_fold(term.term)) for term in ordered)
    # `\b` on both ends keeps `mesa` out of `mesada`. Terms containing spaces or hyphens still work:
    # the boundary is only asserted at the outer edges of the alternation.
    pattern = re.compile(rf"\b(?:{alternatives})\b")
    return _Matcher(
        terms=terms,
        pattern=pattern,
        by_fold={_fold(term.term): term.term for term in terms},
    )


@lru_cache(maxsize=1)
def _default_matcher() -> _Matcher:
    return _compile(load_vocabulary())


def detect(text: str, terms: tuple[JargonTerm, ...] | None = None) -> tuple[str, ...]:
    """Which vocabulary terms this text uses, in vocabulary order, without duplicates.

    Vocabulary order rather than order of appearance: the result is stored on a chunk, and a chunk's
    metadata should not change because a sentence was rearranged.
    """
    matcher = _compile(terms) if terms is not None else _default_matcher()
    found = {matcher.by_fold[match] for match in matcher.pattern.findall(_fold(text))}
    return tuple(term.term for term in matcher.terms if term.term in found)
