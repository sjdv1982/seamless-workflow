"""§14.1 — "There is no exception for cache hits."

    It is tempting to let a turn install ``complete`` immediately when the
    result is already in the in-process transformation cache.  It should not.

Three reasons, all of which this file is the acceptance test for: a general
cache check is a coroutine (in-process map, then remote database, then execute),
special-casing the local hit would reimplement a partial prefix of
``transformation_cache.run()`` and have to be kept in agreement with it forever,
and it would make node state *history-dependent* — re-setting a pin to a
previously computed value would complete in-turn while the first setting did not,
so every test asserting a pending state after a write would silently depend on
what the process had computed earlier.

§15 defers this test to A4, which is also the first phase where a Context
transformer and a ``delayed`` transformer share a cache entry at all.

**Both mechanisms, because they answer different questions and can disagree.**
The observation log counts what the graph *did*: one ``cache-miss`` for the
transformation across both Contexts means the body ran once, and a ``cache-hit``
means the second Context asked and the cache answered.  The clock measures what
the caller *experienced*.  Neither implies the other, and each catches what the
other misses:

* a hit that still costs a body duration to serve — a real defect, in the
  delivery path rather than the cache — passes the count and fails the clock;
* a second execution that happens to be fast, because the OS cache is warm or
  the body is cheap, passes the clock and fails the count.

Wall-clock alone is what §15 A4 warns against ("flaky under load, and infers
what you want rather than measuring it"), so the count is the primary
assertion and the clock is the corroborating one.  A file that keeps only the
clock would be measuring a proxy; one that keeps only the count would call a
five-second cache hit a success.

Today both fail, and the count says why more precisely than the clock can:
`_derive_transformer` calls the Python callable directly and never populates the
transformation cache, so the second Context records a second **miss** rather than
a hit — the premise of a "cache hit" does not hold yet at all.
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    PENDING,
    SHORT_BODY_SECONDS,
    settle,
    states,
    timed,
)
from seamless_workflow import Context


def slow_add(x, y, delay):
    import time

    time.sleep(delay)
    return x + y


def _graph(delay):
    ctx = Context()
    ctx.tf = slow_add
    ctx.tf.pins.x = 41
    ctx.tf.pins.delay = delay
    return ctx


@pytest.mark.a4
@pytest.mark.slow
def test_a_cached_result_still_leaves_the_node_pending_for_one_turn(
    transformation_observations,
):
    observations = transformation_observations

    first = _graph(SHORT_BODY_SECONDS)
    first.tf.pins.y = 1
    settle(first, timeout=60)
    assert first.tf.result.value == 42

    # The premise, asserted rather than assumed: the body ran exactly once, so
    # anything the second Context needs is genuinely in the cache.  Without this
    # the test below could "prove" a cache hit against an empty cache.
    assert len(observations.misses("tf")) == 1, observations.entries()

    second = _graph(SHORT_BODY_SECONDS)

    second.tf.pins.y = 1  # identical transformation: the result is cached

    assert second.tf.state in PENDING, states(second)
    assert second.tf.result.checksum is None


@pytest.mark.a4
@pytest.mark.slow
def test_a_cache_hit_settles_without_re_running_the_body(
    transformation_observations,
):
    """The hit is real: settling costs a lookup, not another body duration."""

    observations = transformation_observations

    first = _graph(SHORT_BODY_SECONDS)
    first.tf.pins.y = 1
    settle(first, timeout=60)
    assert len(observations.misses("tf")) == 1, observations.entries()

    second = _graph(SHORT_BODY_SECONDS)
    with timed() as elapsed:
        second.tf.pins.y = 1
        settle(second, timeout=60)

    assert second.tf.result.value == 42

    # What the graph did.
    assert len(observations.misses("tf")) == 1, (
        f"the body ran {len(observations.misses('tf'))} times across two Contexts "
        f"sharing one transformation identity; the second was a cache hit and must "
        f"not have executed — {observations.entries()}"
    )
    assert observations.hits("tf"), (
        f"the second Context never recorded a cache hit, so nothing consulted the "
        f"cache: {observations.entries()}"
    )
    assert len(set(observations.checksums("tf"))) == 1, (
        f"the two Contexts computed different transformation identities for the "
        f"same inputs, so there was nothing to hit: "
        f"{set(observations.checksums('tf'))}"
    )

    # What the caller experienced.  Corroborating, not primary: a hit that costs
    # a body duration to deliver would satisfy every assertion above.
    assert elapsed.seconds < SHORT_BODY_SECONDS, elapsed.seconds
