from __future__ import annotations

import time
from uuid import uuid4


SLEEP_SECONDS = 5.0
PROMPT_LIMIT = 0.75


def slow_identity(value, delay, nonce):
    import time

    time.sleep(delay)
    return value


def increment(value):
    return value + 1


def _node(context, name):
    return context._graph.nodes[(name,)]


def test_slow_transformer_never_blocks_the_mutating_caller(make_context):
    """Port the legacy sleep/across-barrier shape as latency assertions.

    The unique nonce prevents a previous process-local cache hit from removing
    the five-second observation window.
    """

    context = make_context()
    context.slow = slow_identity
    context.slow.pins.delay = SLEEP_SECONDS
    context.slow.pins.nonce = uuid4().hex

    started = time.monotonic()
    context.slow.pins.value = 41
    mutation_elapsed = time.monotonic() - started

    assert mutation_elapsed < PROMPT_LIMIT
    assert _node(context, "slow").state == "computing"
    assert _node(context, "slow").current_checksum is None

    time.sleep(0.2)
    assert _node(context, "slow").state == "computing"
    assert _node(context, "slow").current_checksum is None

    context.after = increment
    started = time.monotonic()
    context.after.pins.value = context.slow
    connection_elapsed = time.monotonic() - started

    assert connection_elapsed < PROMPT_LIMIT
    assert _node(context, "after").state == "waiting"
    assert _node(context, "after").current_checksum is None

    started = time.monotonic()
    context.compute()
    barrier_elapsed = time.monotonic() - started

    assert barrier_elapsed >= SLEEP_SECONDS - 1.0
    assert barrier_elapsed < SLEEP_SECONDS + 3.0
    assert _node(context, "slow").state == "complete"
    assert _node(context, "after").state == "complete"
    assert context.after.result.value == 42

