"""§15 A0 — the test that makes the whole defect self-evident.

The design writes this one out as a script and says it should be written first:

    set the last pin        -> returns in well under a second
                            -> state is computing, no result checksum
    during the next ~5 s    -> state stays computing
    ctx.compute()           -> takes ~5 s, then state is complete
    connect a downstream    -> returns promptly; downstream is waiting, not computing

"The absence of this test is why every other gap in this list stayed invisible."

Measured against the current implementation with a two-second body: setting the
last pin blocks the caller for **two body durations** and returns ``complete``
with a result checksum already installed.  (The design records one body duration
for the same shape; the multiple depends on how many passes ``_derive_all``
needs to converge, which depends on node count and name order — which is itself
part of the point: the number of times user code runs per public call is not a
property anyone currently controls.)

The completion half of the script is written here with a plain observation loop
rather than with ``ctx.compute()``, so that a missing barrier cannot be mistaken
for a latency defect.  The barrier's own timing is asserted in
``quiescence-barrier/``.
"""

from __future__ import annotations

import time

import pytest

from contract_helpers import (
    BODY_SECONDS,
    PENDING,
    PROMPT_SECONDS,
    compute_or_settle,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def _armed_transformer(ctx, delay=None):
    """A transformer with every pin set but one."""

    ctx.tf = slow_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = BODY_SECONDS if delay is None else delay
    return ctx


@pytest.mark.a1
@pytest.mark.slow
def test_setting_the_last_pin_returns_before_the_body_finishes():
    ctx = _armed_transformer(Context())

    with timed() as elapsed:
        ctx.tf.pins.y = 2

    assert elapsed.seconds < PROMPT_SECONDS, (
        f"setting the last pin blocked the caller for {elapsed.seconds:.1f}s; a "
        f"public call must not execute a {BODY_SECONDS:.0f}s transformer body"
    )
    assert ctx.tf.state == "computing", states(ctx)
    assert ctx.tf.result.checksum is None
    assert ctx.tf.result.value is None


@pytest.mark.a1
@pytest.mark.slow
def test_the_node_stays_pending_while_the_body_runs():
    ctx = _armed_transformer(Context())
    ctx.tf.pins.y = 2

    time.sleep(BODY_SECONDS / 2)

    assert ctx.tf.state in PENDING, states(ctx)
    assert ctx.tf.result.checksum is None


@pytest.mark.a1
@pytest.mark.slow
def test_reads_during_computation_do_not_block_on_the_body():
    """A read is a checksum snapshot (§7), not a request to compute."""

    ctx = _armed_transformer(Context())
    ctx.tf.pins.y = 2

    with timed() as elapsed:
        assert ctx.tf.result.checksum is None
        assert ctx.tf.result.value is None
        assert ctx.tf.state == "computing"

    assert elapsed.seconds < PROMPT_SECONDS, elapsed.seconds


@pytest.mark.a4
@pytest.mark.slow
def test_the_result_arrives_within_about_one_body_duration():
    """The upper bound is loose on purpose; the point is that it is *one* body."""

    ctx = _armed_transformer(Context())

    with timed() as elapsed:
        ctx.tf.pins.y = 2
        compute_or_settle(ctx)

    assert ctx.tf.result.value == 42
    assert ctx.tf.state == "complete"
    assert elapsed.seconds < BODY_SECONDS * 2, (
        f"the graph took {elapsed.seconds:.1f}s to settle a single "
        f"{BODY_SECONDS:.0f}s body; the body ran more than once"
    )
