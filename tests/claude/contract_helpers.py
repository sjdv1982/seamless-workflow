"""Instruments shared by the phase-A0 contract suite.

Design reference: ``seamless/attachments-and-mount-design.md``, §14 and §15 (A0).

These helpers do three things and nothing else.

* They read node state out of the implementation in **exactly one place**
  (:func:`runtime`, :func:`state`, :func:`states`), so that a later phase
  changes one function rather than every test.  §15 A0 requires transition
  tests to assert on *internal* state, because ``_PUBLIC_STATUS`` maps both
  ``waiting`` and ``computing`` to ``"Status: pending"``.  ``get_graph(runtime=True)``
  is the public read-out of that internal state, so the suite does not reach
  into ``ctx._graph``.

* They name the barrier entry points the design specifies but the current
  implementation does not have (:func:`context_compute`,
  :func:`context_computation`, :func:`node_computation`), so that a missing
  barrier fails with a sentence rather than with
  ``TypeError: 'MissingView' object is not callable``.

* They observe settling **without pumping** (:func:`settle`, :func:`wait_until`).
  Legacy ``ctx.compute(0.5)`` was a pump: in legacy the event loop ran in the
  caller's thread, so the barrier *was* what advanced the computation.  A
  dedicated controller thread (§4) removes that role, so this suite advances
  nothing by waiting; it only looks.  See
  ``quiescence-barrier/test_barrier_no_pump.py``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable


#: Node states that mean "this node's checksum is not settled yet" (§14.2).
PENDING: frozenset[str] = frozenset({"waiting", "computing"})

#: Body duration for the flagship latency test (§15 A0 uses five seconds).
BODY_SECONDS: float = float(os.environ.get("SEAMLESS_CONTRACT_BODY_SECONDS", "5"))

#: Body duration where five seconds would only make the suite slower.
SHORT_BODY_SECONDS: float = float(os.environ.get("SEAMLESS_CONTRACT_SHORT_BODY_SECONDS", "2"))

#: What "returns promptly" means for a public call that must not execute a body.
PROMPT_SECONDS: float = float(os.environ.get("SEAMLESS_CONTRACT_PROMPT_SECONDS", "1"))

#: Upper bound for observing a graph settle on its own.
SETTLE_TIMEOUT: float = float(os.environ.get("SEAMLESS_CONTRACT_SETTLE_TIMEOUT", "60"))


# ---------------------------------------------------------------- node state


def runtime(ctx) -> dict[str, dict[str, Any]]:
    """Runtime entry per node, keyed by dotted node path."""

    graph = ctx.get_graph(runtime=True)
    return {".".join(node["path"]): node["runtime"] for node in graph["nodes"]}


def state(ctx, path: str) -> str:
    """Internal state of one node (``unwired``/``blocked``/``waiting``/``computing``/``complete``/``failed``)."""

    return runtime(ctx)[path]["state"]


def block_reason(ctx, path: str) -> str | None:
    """``blocked-by-unwired`` / ``blocked-by-error`` / ``None``."""

    return runtime(ctx)[path]["block_reason"]


def result_checksum(ctx, path: str) -> str | None:
    """Current checksum of one node, as hex, or ``None``."""

    return runtime(ctx)[path]["checksum"]


def states(ctx) -> dict[str, str]:
    """``{"tf": "computing", "downstream": "waiting", ...}`` — for readable assertions."""

    return {path: entry["state"] for path, entry in runtime(ctx).items()}


def pending_nodes(ctx) -> dict[str, str]:
    return {path: value for path, value in states(ctx).items() if value in PENDING}


def quiescent(ctx) -> bool:
    """§14.2: no node is ``waiting`` or ``computing``.  Graph-only, by definition."""

    return not pending_nodes(ctx)


# ------------------------------------------------------------------ barriers


def _bound(obj, name: str, what: str):
    method = getattr(type(obj), name, None)
    if not callable(method):
        raise AssertionError(
            f"{what} does not exist: {type(obj).__name__}.{name}() is not a callable "
            f"attribute of the class.  §14.2 requires it; without it the caller has "
            f"no way to wait for a result that no longer arrives synchronously."
        )
    return method


def context_compute(ctx, *args):
    """``ctx.compute()`` — the Context-wide quiescence barrier of §14.2.

    Looked up on the class on purpose: ``Context.__getattr__`` answers unknown
    attribute names with a ``MissingView``, so ``ctx.compute`` is never missing,
    it is merely not callable.
    """

    return _bound(ctx, "compute", "The Context-wide quiescence barrier")(ctx, *args)


def context_computation(ctx):
    """``await ctx.computation()`` — the async form of the same barrier (§14.2)."""

    return _bound(ctx, "computation", "The async Context-wide quiescence barrier")(ctx)


def node_computation(handle):
    """``await ctx.a.computation()`` — the async node-local barrier (§14.2).

    On a bound ``Cell`` this name is currently *taken*: attribute access on a
    Cell handle is sub-path projection, so ``ctx.a.computation`` resolves to a
    projection of ``a``'s value and ``ctx.a.computation()`` builds an
    ``Expression``.  Whoever implements the barrier has to claim the name as a
    real method, exactly as ``compute``/``run`` are claimed today.
    """

    return _bound(handle, "computation", "The async node-local quiescence barrier")(handle)


# ------------------------------------------------------- observation, timing


def wait_until(predicate: Callable[[], bool], timeout: float = SETTLE_TIMEOUT, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until true or ``timeout``.  Advances nothing; only looks."""

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def settle(ctx, timeout: float = SETTLE_TIMEOUT) -> bool:
    """Wait for graph quiescence **without calling a barrier**.

    Used wherever a settled graph is a test *precondition*, so that a missing
    barrier is never mistaken for a broken transition, and so that the A1 limbo
    (in which the Context computes nothing) times out here instead of
    deadlocking.  Returns whether quiescence was reached.
    """

    return wait_until(lambda: quiescent(ctx), timeout)


class timed:
    """Context manager measuring wall-clock seconds of the block it wraps.

    ``with timed() as t: ...`` then ``t.seconds``.
    """

    def __init__(self) -> None:
        self.seconds = float("nan")
        self._start = 0.0

    def __enter__(self) -> "timed":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.seconds = time.monotonic() - self._start
        return False


# ------------------------------------------------------- execution counting


class ExecutionLog:
    """File-backed execution counter for transformer bodies.

    A module-level counter only works while transformer code runs in the test
    process, which is true today and false from A4 on, when execution moves to
    a worker.  A file is the one instrument that survives that move.

    The log path is passed to the body as an ordinary pin, which makes it part
    of the transformation identity: two ``ExecutionLog``s are two different
    computations, so a test never silently measures a cache hit from an earlier
    run.  That also means a body using this log is deliberately impure; it is
    an instrument, not an example of a well-formed transformer.
    """

    def __init__(self, path) -> None:
        self.path = str(path)

    def entries(self) -> list[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return [line.rstrip("\n") for line in handle if line.strip()]
        except FileNotFoundError:
            return []

    def count(self) -> int:
        return len(self.entries())


__all__ = [
    "BODY_SECONDS",
    "ExecutionLog",
    "PENDING",
    "PROMPT_SECONDS",
    "SETTLE_TIMEOUT",
    "SHORT_BODY_SECONDS",
    "block_reason",
    "context_computation",
    "context_compute",
    "node_computation",
    "pending_nodes",
    "quiescent",
    "result_checksum",
    "runtime",
    "settle",
    "state",
    "states",
    "timed",
    "wait_until",
]
