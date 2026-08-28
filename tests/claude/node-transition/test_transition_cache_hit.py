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
"""

from __future__ import annotations

import pytest

from contract_helpers import (
    PENDING,
    SHORT_BODY_SECONDS,
    settle,
    state,
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
def test_a_cached_result_still_leaves_the_node_pending_for_one_turn():
    first = _graph(SHORT_BODY_SECONDS)
    first.tf.pins.y = 1
    assert settle(first, timeout=60), states(first)
    assert first.tf.result.value == 42

    second = _graph(SHORT_BODY_SECONDS)

    second.tf.pins.y = 1  # identical transformation: the result is cached

    assert state(second, "tf") in PENDING, states(second)
    assert second.tf.result.checksum is None


@pytest.mark.a4
@pytest.mark.slow
def test_a_cache_hit_settles_without_re_running_the_body():
    """The hit is real: settling costs a lookup, not another body duration."""

    first = _graph(SHORT_BODY_SECONDS)
    first.tf.pins.y = 1
    assert settle(first, timeout=60), states(first)

    second = _graph(SHORT_BODY_SECONDS)
    with timed() as elapsed:
        second.tf.pins.y = 1
        assert settle(second, timeout=60), states(second)

    assert second.tf.result.value == 42
    assert elapsed.seconds < SHORT_BODY_SECONDS, elapsed.seconds
