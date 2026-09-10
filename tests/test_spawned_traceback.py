"""Spawned transformer failures retain user frames without outer machinery."""

import re

import pytest

from seamless.transformer import spawn
from seamless_transformer.worker import shutdown_workers


def fail(error_name):
    import builtins

    def inner():
        raise getattr(builtins, error_name)("spawned traceback regression")

    inner()


@pytest.fixture
def spawned_workers():
    spawn(2)
    try:
        yield
    finally:
        shutdown_workers()


@pytest.mark.parametrize("error_name", ["ValueError", "NotImplementedError", "AssertionError"])
def test_spawned_traceback_starts_at_transformer(make_context, spawned_workers, error_name):
    ctx = make_context()
    ctx.func = fail
    ctx.func.pins.error_name = error_name
    ctx.compute(timeout=30)

    assert ctx.func.state == "failed"
    error = str(ctx.func.exception)
    assert f"{error_name}: spawned traceback regression" in error
    filenames = re.findall(r'File "([^"]+)"', error)
    assert filenames, error
    assert filenames[0].startswith("transformer-") and not filenames[0].endswith(".py"), error
    assert "in inner" in error, error
