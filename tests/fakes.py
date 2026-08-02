"""A deterministic `Embedder` with no weights, no network and no meaning.

The workhorse of the dense tests. It has the real dimension, the real two-method interface and a
real `EmbedderSpec`, so it exercises the schema, the `WHERE model_key`, the write guard, the tier
filter, the shape of `components`, the endpoint and every fingerprint assertion — all of it offline
and in milliseconds.

**It deliberately produces no semantics**, and that is a constraint rather than a shortcut. A test
reading "`torque do cabeçote` retrieves the cylinder head chunk" against a hash-seeded embedder
passes or fails by coincidence, and a coincidence that goes green is worse than no test: it would
have to be deleted the first time someone changed the corpus, and until then it would be cited as
evidence. Retrieval *quality* is measured in exactly one place, by the ADR-0004 gate, against
committed questions and a promoted baseline. This file measures plumbing.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Sequence

from garage.embedding import (
    EMBEDDING_DIMENSION,
    GRAPH_OPTIMIZATION,
    MAX_SEQ_LEN,
    PASSAGE_PREFIX,
    POOLING,
    QUERY_PREFIX,
    EmbedderSpec,
)


class FakeEmbedder:
    """sha256 of the text seeds a PRNG, the PRNG fills 384 floats, the vector is L2 normalised.

    Every constructor argument is a fingerprint field, so a test that wants "the same embedder with
    the prefixes swapped" or "a second, different embedder" constructs one here rather than
    monkeypatching a real one into a state it could never reach.
    """

    def __init__(
        self,
        *,
        model_id: str = "fake-embedder",
        model_key: str = "baseline",
        dimension: int = EMBEDDING_DIMENSION,
        query_prefix: str = QUERY_PREFIX,
        passage_prefix: str = PASSAGE_PREFIX,
        embed_version: int = 1,
    ) -> None:
        self.model_key = model_key
        self.dimension = dimension
        self.normalized = True
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self.spec = EmbedderSpec(
            model_id=model_id,
            # A fake's "weights" are its own identity: the digest of the parameters that decide what
            # it returns. Made up, but not constant — two fakes differing in `model_id` must not
            # share a fingerprint, or the mismatch tests would be testing nothing.
            weights_sha256=hashlib.sha256(model_id.encode()).hexdigest(),
            tokenizer_sha256=hashlib.sha256(b"fake-tokenizer").hexdigest(),
            dimension=dimension,
            max_seq_len=MAX_SEQ_LEN,
            pooling=POOLING,
            normalize=True,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
            graph_optimization=GRAPH_OPTIMIZATION,
            embed_version=embed_version,
        )
        self.fingerprint = self.spec.fingerprint

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(self._query_prefix + text)

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vector(self._passage_prefix + text) for text in texts)

    def _vector(self, text: str) -> tuple[float, ...]:
        # `random()` and not `gauss()`: the uniform generator is the documented Mersenne Twister
        # output and is stable across interpreters, while the normal deviate is an implementation
        # detail nobody promised. These vectors end up compared byte for byte against a database.
        rng = random.Random(int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big"))
        raw = [rng.random() - 0.5 for _ in range(self.dimension)]
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return tuple(value / norm for value in raw)
