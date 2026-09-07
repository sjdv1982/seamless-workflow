"""A direct port of legacy ``tests/workflow/delay.py``.

The legacy script is one of the ~47 sleeping-transformer tests in the legacy
suite, and it is the canonical shape: two chained transformers with a ``delay``
pin, an edit, and observations taken at fixed offsets.  Its expected output
(``tests/workflow/test-outputs/delay.out``) records what the two observations
had to show::

    START 1
    FUNC 1 5
    Status: pending None       <- 0.5 s after the edit
    START 2
    Status: pending None       <- a further 2 s later, still pending
    ...
    <Silk: 2001.7 >            <- after the barrier

Two changes in the port, both required by §15 A0.

* Legacy observed intermediate state with ``ctx.compute(0.5)`` in some scripts
  and with a ``sleep`` helper here; the ``sleep`` form is the one that carries
  over, because a dedicated controller thread makes the graph advance without
  anyone pumping it.
* Legacy asserted by printing ``Status: pending``.  That string cannot
  distinguish ``waiting`` from ``computing`` (``_PUBLIC_STATUS`` maps both to
  it), so the port asserts internal state and additionally records which of the
  two each node is in — which is the assertion legacy could not write.

Arithmetic is legacy's: ``func(a, delay) = a + 0.1 * delay + 1000``, chained
twice, so the value carries the delays it was computed with and a stale result
is visible as a wrong number rather than as a missing one.
"""

from __future__ import annotations

import time

import pytest

from contract_helpers import (
    PENDING,
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    compute_or_settle,
    states,
    timed,
    try_settle,
)
from seamless_workflow import Context


def func(a, delay):
    import time

    time.sleep(delay)
    return a + 0.1 * delay + 1000


TAIL_DELAY = 1.0


def _chain():
    ctx = Context()
    ctx.tf1 = func
    ctx.tf1.pins.a = 1
    ctx.tf1.pins.delay = 0
    ctx.intermediate = ctx.tf1
    ctx.tf2 = func
    ctx.tf2.pins.a = ctx.intermediate
    ctx.tf2.pins.delay = 0
    ctx.result = ctx.tf2
    return ctx


@pytest.mark.a1
@pytest.mark.slow
def test_editing_an_upstream_delay_makes_the_result_pending_immediately():
    """Legacy's "START 1" observation, without waiting for it."""

    ctx = _chain()
    try_settle(ctx, timeout=30)

    with timed() as elapsed:
        ctx.tf1.pins.delay = SHORT_BODY_SECONDS

    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds
    assert ctx.tf1.state == "computing", states(ctx)
    assert ctx.tf2.state == "waiting", states(ctx)
    assert ctx.result.state == "waiting", states(ctx)
    assert ctx.result.value is None


@pytest.mark.a4
@pytest.mark.slow
def test_the_chain_reports_pending_at_both_legacy_observation_points():
    ctx = _chain()
    compute_or_settle(ctx)
    assert ctx.result.value == pytest.approx(2001.0)

    ctx.tf2.pins.delay = TAIL_DELAY
    compute_or_settle(ctx)

    ctx.tf1.pins.delay = SHORT_BODY_SECONDS

    time.sleep(SHORT_BODY_SECONDS / 4)  # legacy: sleep(0.5) after "START 1"
    assert ctx.result.state in PENDING, states(ctx)
    assert ctx.result.value is None

    time.sleep(SHORT_BODY_SECONDS)  # legacy: sleep(2) after "START 2"
    assert ctx.result.state in PENDING, states(ctx)
    assert ctx.result.value is None

    compute_or_settle(ctx)
    assert ctx.result.value == pytest.approx(
        1 + 0.1 * SHORT_BODY_SECONDS + 1000 + 0.1 * TAIL_DELAY + 1000
    )


@pytest.mark.a4
@pytest.mark.slow
def test_the_intermediate_cell_never_shows_the_previous_result():
    """The value channel carries no stale reading while the chain re-runs."""

    ctx = _chain()
    compute_or_settle(ctx)
    first = ctx.intermediate.value
    assert first == pytest.approx(1001.0)

    ctx.tf1.pins.delay = SHORT_BODY_SECONDS

    assert ctx.intermediate.value is None, "stale intermediate result observed"
    time.sleep(SHORT_BODY_SECONDS / 2)
    assert ctx.intermediate.value is None, "stale intermediate result observed"

    compute_or_settle(ctx)
    assert ctx.intermediate.value == pytest.approx(1 + 0.1 * SHORT_BODY_SECONDS + 1000)
