"""Spans: the trace is the product (design §12).

Every query emits a tree — `query` at the root, one child per pipeline stage — carrying what each
stage did and how long it took. It is not diagnostic output kept for a bad day: the demo renders it
beside the answer, and a run record stores it, so a claim the system makes about its own latency or
its own retrieval is inspectable rather than asserted (Glass Box, CONTEXT.md).

Written by hand rather than against the OpenTelemetry SDK, and the reason is proportion: the whole
requirement is a tree of named, timed, attributed spans on a machine running no collector (design
§14). What the SDK would buy is exporters, and an exporter can be written against `Span.to_dict()`
the day a Jaeger is actually running. What it would cost is a dependency and a global provider in
every test.

Compatible where compatibility is load-bearing: identifiers are OTel-shaped (16 hex characters for a
span, 32 for a trace), times are Unix nanoseconds, and attribute keys are dotted namespaces. The
nesting is the one difference — OTLP is a flat list of spans joined by `parent_span_id`, and this
tree flattens to exactly that.
"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# Attribute values are kept to what OTLP can carry natively, so flattening never has to invent an
# encoding for a value that was a Python object all along.
AttributeValue = str | bool | int | float | None


def _trace_id() -> str:
    return secrets.token_hex(16)


def _span_id() -> str:
    return secrets.token_hex(8)


@dataclass
class Span:
    """One stage: what it was, when it ran, what it decided."""

    name: str
    span_id: str
    parent_span_id: str | None
    # Two clocks on purpose. The wall clock says *when* this happened and is what an exporter needs;
    # it is also allowed to jump backwards. `perf_counter_ns` is monotonic and is the only thing
    # duration is ever measured with, so an NTP correction mid-query cannot produce a negative one.
    start_unix_nano: int
    _started: int
    attributes: dict[str, AttributeValue] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)
    end_unix_nano: int | None = None
    duration_ns: int | None = None

    def set(self, **attributes: AttributeValue) -> None:
        """Record what this stage decided. Called during the span, not guessed afterwards."""
        self.attributes.update(attributes)

    @property
    def duration_ms(self) -> float | None:
        return None if self.duration_ns is None else self.duration_ns / 1_000_000

    def to_dict(self, trace_id: str) -> dict[str, Any]:
        return {
            "traceId": trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "startTimeUnixNano": str(self.start_unix_nano),
            # Strings, as OTLP carries them: a nanosecond timestamp overflows the integer a JSON
            # parser in a browser will hand back.
            "endTimeUnixNano": None if self.end_unix_nano is None else str(self.end_unix_nano),
            "durationMs": self.duration_ms,
            "attributes": dict(self.attributes),
            "children": [child.to_dict(trace_id) for child in self.children],
        }


class Tracer:
    """Collects one query's spans. One tracer per request; never shared across them."""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or _trace_id()
        self.root: Span | None = None
        self._stack: list[Span] = []

    @contextmanager
    def span(self, name: str, **attributes: AttributeValue) -> Iterator[Span]:
        """Open a span under whichever one is currently open."""
        parent = self._stack[-1] if self._stack else None
        current = Span(
            name=name,
            span_id=_span_id(),
            parent_span_id=parent.span_id if parent else None,
            start_unix_nano=time.time_ns(),
            _started=time.perf_counter_ns(),
            attributes=dict(attributes),
        )
        if parent is None:
            self.root = current
        else:
            parent.children.append(current)
        self._stack.append(current)
        try:
            yield current
        except Exception as failure:
            # A stage that raised still gets a duration and still appears in the tree. A trace that
            # goes silent exactly when something went wrong is worth very little.
            current.set(
                error=True,
                **{"exception.type": type(failure).__name__, "exception.message": str(failure)},
            )
            raise
        finally:
            current.duration_ns = time.perf_counter_ns() - current._started
            current.end_unix_nano = time.time_ns()
            self._stack.pop()

    def tree(self) -> dict[str, Any] | None:
        """The finished tree, or None if nothing was ever traced."""
        return None if self.root is None else self.root.to_dict(self.trace_id)
