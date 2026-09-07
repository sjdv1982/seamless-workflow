"""§15 A0 — "connect a downstream -> returns promptly; downstream is waiting, not computing".

The second half of the latency script, and the one with the sharper measurement
behind it.  Against the current implementation, connecting one downstream
transformer to a node that has already produced its result **re-executes the
upstream body five more times** — six ``cache-miss`` observations for one
transformation checksum where one is correct.  ``_derive_all`` re-derives every
node once per convergence pass and makes up to one pass per node, and derivation
*is* execution, so the cost of adding a node to a settled graph is quadratic in
bodies run.  Wall-clock shows it; the observation log proves it, per node.

**The instrument is outside the transformation, not inside it.**
``seamless_transformer.observation`` records one line per transformation reaching
an execution decision — its checksum, whose it was, and whether a cache answered
— and is off unless a test switches it on.  The earlier version of this file
counted executions by passing a log path to the body as an ordinary pin, which
worked but put the instrument *inside* the transformation identity: an identity
that accidentally collided produced a spurious cache hit, a low count, and a
**passing** test.  An instrument whose failure mode is silent success, in the
same property the test is about, is the wrong instrument.  Nothing here is passed
to the body, so there is nothing to perturb.

Two numbers per node, and they answer different questions:

* ``misses(node)`` — how many times the body **ran**.  The direct successor of
  the old execution count, and the primary contract: one execution per
  transformation, whatever happens downstream.
* ``hits(node)`` — how many times the graph **asked again** and the cache
  answered.  Zero is the §14.1 contract (a settled node is not re-submitted at
  all), and separating the two makes a failure say *which* went wrong: a
  re-execution is a correctness defect, a re-ask is only waste.

Today every observation is a miss, because `_derive_transformer` calls the Python
callable directly and never consults the cache — which is the defect, and the
reason the recording site had to be there and not only in the cache.
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    PROMPT_SECONDS,
    SHORT_BODY_SECONDS,
    settle,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    if delay:
        time.sleep(delay)
    return x + y


def double(x):
    return 2 * x


@pytest.mark.a1
@pytest.mark.slow
def test_connecting_a_downstream_returns_promptly():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.y = 2

    with timed() as elapsed:
        ctx.tail = double
        ctx.tail.pins.x = ctx.tf

    assert elapsed.seconds < PROMPT_SECONDS, (
        f"connecting a downstream transformer blocked the caller for "
        f"{elapsed.seconds:.1f}s"
    )


@pytest.mark.a1
@pytest.mark.slow
def test_a_freshly_connected_downstream_is_waiting_not_computing():
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS
    ctx.tf.pins.y = 2

    ctx.tail = double
    ctx.tail.pins.x = ctx.tf

    assert ctx.tail.state == "waiting", states(ctx)
    assert ctx.tail.result.checksum is None


@pytest.mark.a4
@pytest.mark.slow
def test_connecting_a_downstream_does_not_re_execute_the_upstream_body(
    transformation_observations,
):
    """One transformation identity, one execution — whatever happens downstream."""

    observations = transformation_observations

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 40
    ctx.tf.pins.delay = SHORT_BODY_SECONDS

    ctx.tf.pins.y = 2
    settle(ctx, timeout=60)
    assert len(observations.misses("tf")) == 1, observations.entries()

    ctx.tail = double
    ctx.tail.pins.x = ctx.tf
    ctx.out = ctx.tail
    settle(ctx, timeout=60)

    assert ctx.out.value == 84
    assert len(observations.misses("tf")) == 1, (
        f"the upstream body ran {len(observations.misses('tf'))} times; connecting "
        f"a downstream node must not re-execute an upstream that has not changed "
        f"— {observations.entries()}"
    )
    assert observations.hits("tf") == [], (
        f"the upstream was re-submitted {len(observations.hits('tf'))} times and "
        f"served from cache; §14.1 asks that a settled node not be re-submitted at "
        f"all — this is waste rather than a wrong answer, but it is the same "
        f"re-derivation seen from the other side"
    )
    assert len(set(observations.checksums("tf"))) == 1, (
        f"the upstream produced more than one transformation identity without its "
        f"inputs changing: {set(observations.checksums('tf'))}"
    )


@pytest.mark.a4
@pytest.mark.slow
def test_an_unrelated_node_does_not_re_execute_an_existing_one(
    transformation_observations,
):
    """Adding an unrelated node is not a reason to recompute anything."""

    observations = transformation_observations

    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 1
    ctx.tf.pins.delay = 0
    ctx.tf.pins.y = 2
    settle(ctx, timeout=60)
    assert len(observations.misses("tf")) == 1, observations.entries()

    ctx.unrelated_cell = {"noise": True}
    ctx.another = double
    ctx.another.pins.x = 5
    settle(ctx, timeout=60)

    assert len(observations.misses("tf")) == 1, observations.entries()
    assert observations.hits("tf") == [], observations.entries()
    assert observations.count("another") <= 1, (
        f"the unrelated node itself was derived more than once: "
        f"{observations.entries()}"
    )
