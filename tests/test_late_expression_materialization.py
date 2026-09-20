"""Appendix B.6(c): a superseded Expression result must not reach its node."""

import asyncio
import threading

from seamless.checksum import expression as expression_mod
from seamless_workflow import Context


def test_expression_result_arriving_after_supersession_does_not_reach_node(
    monkeypatch, make_context
):
    ctx = make_context()
    ctx.source = {"value": "old materialization"}
    old_checksum = ctx.source.checksum
    started = threading.Event()
    release = threading.Event()
    delivered = threading.Event()
    accepted = threading.Event()
    original_evaluate = expression_mod.evaluate_expression_remote
    original_accept = Context._accept_fact

    async def delayed_result(checksum, path, *args, **kwargs):
        result = await original_evaluate(checksum, path, *args, **kwargs)
        if checksum == old_checksum and path == "value":
            started.set()
            # A source may finish even after cancellation. Make the old result
            # arrive only after the replacement has reached the destination.
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            delivered.set()
        return result

    def observe_accept(self, key, lease, error):
        try:
            return original_accept(self, key, lease, error)
        finally:
            if (self is ctx and key[0] == "expression"
                    and key[1] == old_checksum.hex() and delivered.is_set()):
                accepted.set()

    monkeypatch.setattr(expression_mod, "evaluate_expression_remote", delayed_result)
    monkeypatch.setattr(Context, "_accept_fact", observe_accept)
    try:
        ctx.projected = ctx.source["value"]
        assert started.wait(5), "old projection never entered evaluation"
        ctx.source = {"value": "replacement materialization"}
        ctx.compute(timeout=10)
        assert ctx.projected.value == "replacement materialization"
        replacement_checksum = ctx.projected.checksum

        release.set()
        # Observe processing of the old completion, not merely release of its
        # gate: otherwise the value assertion could run before the bad write.
        assert accepted.wait(5), "late completion was not processed"
        ctx.compute(timeout=10)
        assert ctx.projected.checksum == replacement_checksum
        assert ctx.projected.value == "replacement materialization"
        assert ctx.projected.exception is None
    finally:
        release.set()
