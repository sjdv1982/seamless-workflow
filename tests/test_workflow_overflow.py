"""Draft: aging temprefs past 2048 s must not kill the controller or the close."""

import gc
import time

import pytest

from seamless import Buffer, Cell
from seamless.caching import buffer_cache as bc
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import audit_reference_accounting
from seamless_workflow import Context


@pytest.fixture
def aged(monkeypatch):
    """Return a function that ages every tempref by `seconds` (default 2100)."""
    real = time.time

    def age(seconds=2100.0):
        monkeypatch.setattr(time, "time", lambda: real() + seconds)

    return age


def test_set_keeps_deriving_and_close_leaves_empty_audit_after_aging(aged, caplog):
    ctx = Context()
    ctx.a = 1
    ctx.compute(timeout=10)
    aged(2100.0)  # all temprefs are now past the 2048 s overflow threshold
    # Several later updates: the controller must stay alive (no ControllerFailedError).
    for i in range(2, 6):
        ctx.a = i
        ctx.compute(timeout=10)
        assert ctx.a.value == i
    ctx.close()  # must not raise
    gc.collect()
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(cache=get_buffer_cache(), holders=[])
    unattributed = [r for r in caplog.records if "unattributed" in r.getMessage()]
    assert not unattributed, [r.getMessage()[:120] for r in unattributed]
    assert not [c for c in get_buffer_cache().reference_snapshot().values() if c[0]]
