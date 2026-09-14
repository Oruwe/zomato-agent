"""Microsecond-resolution latency instrumentation for the deterministic control plane.

Design notes
------------
The agent is split into two planes with very different latency profiles:

* **Control plane** (guardrails, policy, wallet, memory, state machine) -- pure local
  CPU work. Target: single-digit to low-hundreds of *microseconds*. Measured here and
  budget-asserted in CI so regressions fail the build.
* **Data plane** (Gemini inference, MCP network calls) -- inherently 10^5-10^6 us.
  Optimised separately via caching, connection reuse and parallel fan-out.

Everything in this module is built to keep measurement overhead far below the thing
being measured: ``time.perf_counter_ns`` reads, preallocated ring buffers, ``__slots__``
and no allocation on the hot path.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any, TypeVar

__all__ = [
    "LatencyRegistry",
    "REGISTRY",
    "timed",
    "measure",
    "now_ns",
    "LatencyBudgetExceeded",
]

F = TypeVar("F", bound=Callable[..., Any])

# Bind the clock read to a local name once; this is the single hottest call in the
# process and attribute lookup on every invocation is measurable at this scale.
now_ns = time.perf_counter_ns

_RING_CAPACITY = 4096


class LatencyBudgetExceeded(AssertionError):
    """Raised when a measured operation blows its declared microsecond budget."""


class _Ring:
    """Fixed-capacity ring buffer of ns samples. Appends are O(1) and allocation-free."""

    __slots__ = ("_buf", "_idx", "_count", "_total_ns", "_max_ns")

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        self._buf: list[int] = [0] * capacity
        self._idx = 0
        self._count = 0
        self._total_ns = 0
        self._max_ns = 0

    def add(self, sample_ns: int) -> None:
        buf = self._buf
        cap = len(buf)
        buf[self._idx] = sample_ns
        self._idx = (self._idx + 1) % cap
        if self._count < cap:
            self._count += 1
        self._total_ns += sample_ns
        if sample_ns > self._max_ns:
            self._max_ns = sample_ns

    def samples(self) -> list[int]:
        if self._count < len(self._buf):
            return self._buf[: self._count]
        return list(self._buf)

    @property
    def count(self) -> int:
        return self._count

    @property
    def total_ns(self) -> int:
        return self._total_ns

    @property
    def max_ns(self) -> int:
        return self._max_ns


class LatencyRegistry:
    """Process-wide collector of per-operation latency distributions.

    Percentiles are computed lazily on read (``snapshot``), never on the write path.
    """

    __slots__ = ("_ops", "_lock")

    def __init__(self) -> None:
        self._ops: dict[str, _Ring] = {}
        self._lock = Lock()

    def record_ns(self, op: str, elapsed_ns: int) -> None:
        ring = self._ops.get(op)
        if ring is None:
            # Only contended on first sight of an operation name.
            with self._lock:
                ring = self._ops.get(op)
                if ring is None:
                    ring = _Ring()
                    self._ops[op] = ring
        ring.add(elapsed_ns)

    def reset(self) -> None:
        with self._lock:
            self._ops.clear()

    def stats(self, op: str) -> dict[str, float] | None:
        ring = self._ops.get(op)
        if ring is None or ring.count == 0:
            return None
        samples = sorted(ring.samples())
        n = len(samples)

        def pct(p: float) -> float:
            # Nearest-rank percentile; exact for the sample set, no interpolation games.
            idx = min(n - 1, max(0, int(round(p / 100.0 * n + 0.5)) - 1))
            return samples[idx] / 1000.0

        return {
            "count": float(ring.count),
            "p50_us": pct(50),
            "p95_us": pct(95),
            "p99_us": pct(99),
            "max_us": ring.max_ns / 1000.0,
            "mean_us": (ring.total_ns / ring.count) / 1000.0,
        }

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {op: s for op in list(self._ops) if (s := self.stats(op)) is not None}

    def assert_budget(self, op: str, *, p99_us: float) -> dict[str, float]:
        """Fail if the p99 for ``op`` exceeds ``p99_us`` microseconds."""
        stats = self.stats(op)
        if stats is None:
            raise LatencyBudgetExceeded(f"no latency samples recorded for {op!r}")
        if stats["p99_us"] > p99_us:
            raise LatencyBudgetExceeded(
                f"{op}: p99 {stats['p99_us']:.1f}us exceeds budget {p99_us:.1f}us "
                f"(p50={stats['p50_us']:.1f}us max={stats['max_us']:.1f}us n={int(stats['count'])})"
            )
        return stats


REGISTRY = LatencyRegistry()


@contextmanager
def measure(op: str, registry: LatencyRegistry | None = None) -> Iterator[None]:
    """Context manager timing a block at ns resolution."""
    reg = registry or REGISTRY
    start = now_ns()
    try:
        yield
    finally:
        reg.record_ns(op, now_ns() - start)


def timed(op: str, registry: LatencyRegistry | None = None) -> Callable[[F], F]:
    """Decorator recording wall time of a *synchronous* control-plane function."""

    def decorate(fn: F) -> F:
        reg = registry or REGISTRY

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = now_ns()
            try:
                return fn(*args, **kwargs)
            finally:
                reg.record_ns(op, now_ns() - start)

        return wrapper  # type: ignore[return-value]

    return decorate
