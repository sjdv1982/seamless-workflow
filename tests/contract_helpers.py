"""Instruments shared by the phase-A0 contract suite.

Design reference: ``seamless/attachments-and-mount-design.md``, §14 and §15 (A0).

**A helper that spells something the user would spell differently is a bug; a
helper that spells something the user cannot spell at all is an instrument.**
Only the second kind lives here.  A test is also the worked example of the API
it tests, so a node's state is read as ``ctx.tf.state``, its checksum as
``ctx.tf.result.checksum`` or ``ctx.a.checksum``, and a barrier is called as
``ctx.compute()`` — in the tests, in the product's own spelling, not through a
wrapper that a reader would have to translate back.

Seven wrappers were deleted under that rule and must not come back:
``state``, ``block_reason``, ``result_checksum``, ``context_compute``,
``context_computation``, ``node_compute`` and ``node_computation``.  Two of
them were hiding a gap rather than closing one: ``ctx.a.state`` used to resolve
to a *sub-path projection* of the cell's value and ``ctx.tf.state`` to an
``AttributeError``, so ``assert ctx.a.state == "complete"`` failed for the wrong
reason.  ``Cell.state``/``Cell.block_reason`` and their ``Transformer``
counterparts now claim those names, next to ``status``.  ``computation`` is
still unclaimed on a bound ``Cell`` — attribute access there is projection, so
``ctx.a.computation()`` builds an ``Expression`` — and the barrier tests now say
so by failing on it directly, which is the finding rather than a docstring about
the finding.

What remains is three kinds of thing the API does not offer and should not:

* one **graph-wide snapshot** (:func:`runtime`, :func:`states`,
  :func:`quiescent`).  Per-node reads are the product's job; what the product
  cannot give is *simultaneity*.  ``ctx.tf1.state`` then ``ctx.tf2.state`` is two
  reads that may straddle a controller turn, so an assertion about several nodes
  *at one moment* — and every failure message that shows the whole graph — needs
  one read.  §24.8 (open question 8) is exactly this: whether a first version may
  publish a narrowly defined immutable status snapshot.  ``get_graph(runtime=True)``
  is that snapshot today.

* two **oracles** (:func:`settle`, :func:`try_settle`).  Polling for quiescence
  *without entering the controller* is deliberately not something the API does;
  that is what makes them usable where the barrier is absent, is the subject, or
  would contaminate the measurement.  Neither pumps: legacy ``ctx.compute(0.5)``
  was a pump because the event loop ran in the caller's thread, and a dedicated
  controller thread (§4) removes that role.  See
  ``quiescence-barrier/test_barrier_no_pump.py``.

* a **stopwatch** (:class:`timed`).

:func:`compute_or_settle` is the one deliberate exception, and it is dated: it is
not an alias for ``ctx.compute()`` but a phase shim that polls until A3 lands, at
which point it is deleted and its call sites become ``ctx.compute()``.  It is
used where the barrier is a *precondition*; where the barrier is the *subject* —
all of ``quiescence-barrier/`` — the tests call it by name and are red until it
exists.
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

#: How often :func:`_wait_until` looks.  See its docstring: looking is not free.
POLL_INTERVAL: float = float(os.environ.get("SEAMLESS_CONTRACT_POLL_INTERVAL", "0.02"))

#: Upper bound for observing a graph settle on its own.
SETTLE_TIMEOUT: float = float(os.environ.get("SEAMLESS_CONTRACT_SETTLE_TIMEOUT", "60"))


# ------------------------------------------------------------ graph snapshot


def runtime(ctx) -> dict[str, dict[str, Any]]:
    """One snapshot of every node's runtime entry, keyed by dotted node path.

    For *one* node, write ``ctx.tf.state``, ``ctx.tf.block_reason``,
    ``ctx.tf.result.checksum``, ``ctx.a.checksum``.  This is for the assertions
    that quantify over the whole graph — "no node is complete without a result
    checksum" — where the point is the quantifier, not the read.
    """

    graph = ctx.get_graph(runtime=True)
    return {".".join(node["path"]): node["runtime"] for node in graph["nodes"]}


def states(ctx) -> dict[str, str]:
    """``{"tf": "computing", "downstream": "waiting", ...}`` — one moment, every node.

    Two uses, both of which a per-node read cannot serve: an assertion about
    several nodes *simultaneously*, and the message of an assertion about one
    node, where the rest of the graph is what tells you why it failed.
    """

    return {path: entry["state"] for path, entry in runtime(ctx).items()}


def _pending_nodes(ctx) -> dict[str, str]:
    return {path: value for path, value in states(ctx).items() if value in PENDING}


def quiescent(ctx) -> bool:
    """§14.2: no node is ``waiting`` or ``computing``.  Graph-only, by definition."""

    return not _pending_nodes(ctx)


# ------------------------------------------------------- observation, timing


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float = SETTLE_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> bool:
    """Poll ``predicate`` until true or ``timeout``.  Does not pump.

    "Does not pump" is the strong claim and it holds: nothing here can advance
    the graph, because the controller thread (§4) is what advances it.  But
    polling is not free either — each :func:`quiescent` call is a public read,
    and §24.8 has not settled whether a diagnostic read is served from an
    immutable snapshot or serialised through the controller.  Under the second
    answer a 20 ms interval is fifty inbox messages a second for the whole wait,
    competing for the controller's turn budget with the work being observed.
    Hence ``interval``: a test that knows it is measuring something timing-
    sensitive should widen it.
    """

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def try_settle(ctx, timeout: float = SETTLE_TIMEOUT, interval: float = POLL_INTERVAL) -> bool:
    """Wait for quiescence if it is coming; report whether it arrived.

    The honest form for a test whose graph is **not expected** to settle — the
    A1–A3 limbo, where the Context computes nothing, and the unsatisfiable
    environment, which must not complete.  Those tests want to give the graph
    every chance to be wrong before asserting that it is not; a failure to
    settle is their normal case, not their failure case.

    Everything else should use :func:`settle`, which raises.  The two are spelled
    differently on purpose: before this split the difference between "must
    settle" and "settle if you can" was whether someone had typed ``assert`` in
    front, which is not a difference a reader can see.
    """

    return _wait_until(lambda: quiescent(ctx), timeout, interval)


def settle(ctx, timeout: float = SETTLE_TIMEOUT, interval: float = POLL_INTERVAL) -> None:
    """Wait for graph quiescence **without calling a barrier**.  Raises on expiry.

    This is an *oracle*, not a reference implementation of ``ctx.compute()``.  It
    shares exactly one of the barrier's postconditions — that the graph reached
    quiescence — and none of the rest: it never enters the controller, so it
    cannot stand in for [MOD-17]'s "installs a predicate, does not stall the
    frontier"; it returns nothing, so it cannot stand in for §10's correlated
    barrier-plus-read; and it can report a quiescence that was never a stable
    state, since a graph may pass through a quiescent-looking moment between two
    polls where an end-of-turn predicate could not.

    So it is used **sparingly**, for three reasons and no others:

    1. *the barrier does not exist yet* — preconditions in tests marked ``now``,
       ``a1`` or ``a2``, which must be able to go green at their own phase rather
       than waiting for the A3 barrier;
    2. *the barrier is the subject* — a test that asserts something about
       ``compute`` cannot use ``compute`` to reach its own precondition without
       becoming unfalsifiable;
    3. *the barrier would contaminate the measurement* — the tests that measure
       what ran.  ``latency/test_latency_downstream.py`` counts transformations
       through ``seamless_transformer.observation``;
       ``node-transition/test_transition_cache_hit.py`` uses that count *and* the
       clock, which is why it is the sharper case: a barrier that runs a
       derivation pass adds to both the count and the elapsed time, so it
       corrupts the two mechanisms that exist to cross-check each other.  This
       reason is independent of the phase markers and is the one to guard.

    Every other test waits through :func:`compute_or_settle`, so that a broken
    barrier fails them.

    Raising on expiry rather than returning ``False`` is §27.1.2 applied to the
    suite's own instrument: a bounded wait for quiescence must not return as if
    it had settled.  An oracle that answers "I don't know" in the same shape as
    "yes" is worse than no oracle.
    """

    if not _wait_until(lambda: quiescent(ctx), timeout, interval):
        raise TimeoutError(
            f"the graph did not reach quiescence within {timeout}s: {states(ctx)}.  "
            f"If this test does not expect quiescence, it wants try_settle()."
        )


def compute_or_settle(ctx, timeout: float = SETTLE_TIMEOUT):
    """Wait for quiescence the way a user would: ``ctx.compute()``.

    The ordinary wait, used by every test whose subject is a result, a state or
    an error rather than the barrier itself.  Those tests are barrier coverage by
    construction: from A3 on, a barrier that hangs, returns early or returns a
    non-quiescent graph fails all of them, across three directories, instead of
    failing only ``quiescence-barrier/``.

    **Until the barrier exists it falls back to polling**, and that is deliberate
    rather than defensive.  Whether ``Context.compute`` exists at all is a
    separate contract, asserted once and legibly in
    ``quiescence-barrier/test_barrier_context.py::test_the_context_wide_barrier_exists_and_is_callable``.
    Re-asserting it in seventeen result tests would replace seventeen on-target
    failures — the [MOD-15] bash result, the module-edit invalidation, the
    failure-propagation reasons — with seventeen copies of one missing-attribute
    message, which is the failure mode this suite exists to avoid.  Existence is
    tested in one place; behaviour is tested everywhere.

    This is the one place where the module's own rule is suspended, and it is
    suspended with a date on it: once A3 lands, the fallback is dead code, the
    body becomes ``ctx.compute()``, and the helper is deleted along with its call
    sites.  It survives now only because *here* the barrier is a precondition.
    Where the barrier is the subject — all of ``quiescence-barrier/`` — the tests
    call ``ctx.compute()`` directly and are red until it exists.
    """

    if callable(getattr(type(ctx), "compute", None)):
        return ctx.compute()
    return settle(ctx, timeout)


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


__all__ = [
    "BODY_SECONDS",
    "PENDING",
    "PROMPT_SECONDS",
    "SETTLE_TIMEOUT",
    "SHORT_BODY_SECONDS",
    "POLL_INTERVAL",
    "compute_or_settle",
    "quiescent",
    "runtime",
    "settle",
    "states",
    "try_settle",
    "timed",
]
