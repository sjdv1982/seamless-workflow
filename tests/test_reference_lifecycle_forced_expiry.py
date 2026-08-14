from __future__ import annotations

import gc
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from seamless import Buffer, CacheMissError, Cell
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims
from seamless_workflow import Context

sys.path.insert(0, str(Path(__file__).parent / "helpers"))
from reference_lifecycle import force_expiry  # noqa: E402


def _unique(label: str) -> str:
    return f"{label}-{uuid4().hex}"


def identity(value):
    return value


def _guard_remote(monkeypatch):
    try:
        import seamless_remote.buffer_remote as buffer_remote
    except ImportError:
        return

    async def missing(checksum):
        return None

    monkeypatch.setattr(buffer_remote, "get_buffer", missing)


def _assert_miss(monkeypatch, checksum):
    _guard_remote(monkeypatch)
    with pytest.raises((CacheMissError, RuntimeError, ValueError)):
        checksum.resolve()


def test_cell_literal_and_current_result_are_independent_roles(monkeypatch):
    ctx = Context()
    ctx.value = {"token": _unique("cell-literal")}
    checksum = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    claims = collect_refholder_claims([ctx])[checksum]
    assert {role for _, role in claims} == {"cell:value:literal", "node:value:current"}
    assert get_buffer_cache().reference_snapshot()[checksum][0] == 2
    force_expiry(checksum)
    assert ctx.value.value["token"].startswith("cell-literal-")
    ctx._release_refholds()
    force_expiry(checksum)
    _assert_miss(monkeypatch, checksum)


def test_transformer_pin_code_and_module_roles_survive_bound_api_expiry(monkeypatch):
    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = _unique("workflow-pin")
    pin_checksum = ctx._graph.nodes[("transformer",)].transformer_pin_producers[
        "value"
    ].checksum
    module_buffer = Buffer(_unique("workflow-module").encode(), "text")
    module_checksum = module_buffer.get_checksum()
    ctx.transformer.modules.example = module_checksum
    code_checksum = ctx._graph.nodes[("transformer",)].transformer_config.code_checksum
    claims = collect_refholder_claims([ctx])
    assert (ctx, "transformer:transformer:pin:value") in claims[pin_checksum]
    assert (ctx, "transformer:transformer:module:example") in claims[module_checksum]
    assert (ctx, "transformer:transformer:code") in claims[code_checksum]
    del module_buffer
    gc.collect()

    for checksum in (pin_checksum, module_checksum, code_checksum):
        force_expiry(checksum)
    assert ctx.transformer.pins.value.startswith("workflow-pin-")
    assert module_checksum.resolve("text").startswith("workflow-module-")
    assert code_checksum.resolve() is not None
    # Handles are views; registering one must not add a second claim.
    handle = ctx.transformer
    assert all(holder is ctx for holder, _role in collect_refholder_claims([ctx, handle]).get(pin_checksum, []))
    ctx._release_refholds()
    for checksum in (pin_checksum, module_checksum, code_checksum):
        force_expiry(checksum)
        _assert_miss(monkeypatch, checksum)


def test_transformer_current_result_is_independent_from_pin_role(monkeypatch):
    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = _unique("current-result")
    result_checksum = ctx._graph.nodes[("transformer",)].current_checksum
    pin_checksum = ctx._graph.nodes[("transformer",)].transformer_pin_producers[
        "value"
    ].checksum
    assert result_checksum is not None
    assert pin_checksum == result_checksum
    assert get_buffer_cache().reference_snapshot()[result_checksum][0] >= 2
    assert any(role == "node:transformer:current" for _, role in collect_refholder_claims([ctx])[result_checksum])
    force_expiry(result_checksum)
    assert ctx.transformer.result.value.startswith("current-result-")
    ctx._release_refholds()
    force_expiry(result_checksum)
    _assert_miss(monkeypatch, result_checksum)


def test_same_checksum_replacement_acquires_new_state_before_release():
    checksum = Buffer(314159, "int").get_checksum()
    ctx = Context()
    ctx.value = 314159
    original = ctx._graph.nodes[("value",)].cell_root_producer.checksum
    assert original == checksum
    ctx.value = Cell(checksum, celltype="int")
    assert ctx._graph.nodes[("value",)].cell_root_producer.checksum == checksum
    assert get_buffer_cache().reference_snapshot()[checksum][0] == 2
    ctx._release_refholds()
    assert get_buffer_cache().reference_snapshot().get(checksum, (0, 0, False))[0] == 0


def test_literal_connection_replaces_producer_role_without_leaking():
    ctx = Context()
    ctx.source = {"token": _unique("source")}
    ctx.target = {"token": _unique("target")}
    target_checksum = ctx._graph.nodes[("target",)].cell_root_producer.checksum
    ctx.target = ctx.source
    assert get_buffer_cache().reference_snapshot().get(target_checksum, (0, 0, False))[0] == 1
    source_checksum = ctx._graph.nodes[("source",)].current_checksum
    assert source_checksum is not None
    ctx._release_refholds()
    assert get_buffer_cache().reference_snapshot().get(source_checksum, (0, 0, False))[0] == 0


def test_superseded_result_is_held_until_deterministic_cap_and_prune(monkeypatch):
    import seamless_workflow.scheduler as scheduler

    now = [100.0]
    monkeypatch.setattr(scheduler, "time", lambda: now[0])
    ctx = Context()
    ctx._runtime.scheduler.self_edit_hold_seconds = 15.0
    ctx.value = 1
    first = ctx.get_graph(runtime=True)["nodes"][0]["runtime"]["run"]["current"]["result"]
    ctx.value = 2
    records = list(ctx._runtime.superseded_runs[("value",)])
    assert records[0].result_checksum.hex() == first
    assert records[0].hold_deadline == 115.0
    assert get_buffer_cache().reference_snapshot()[records[0].result_checksum][0] >= 1
    now[0] = 116.0
    assert ctx.prune() == {"cancelled": 1}
    assert get_buffer_cache().reference_snapshot().get(records[0].result_checksum, (0, 0, False))[0] == 0
    ctx._release_refholds()
    assert get_buffer_cache().reference_snapshot().get(records[0].result_checksum, (0, 0, False))[0] == 0


def test_eager_and_non_eager_contexts_have_distinct_current_claim_policy():
    eager = Context(eager=True)
    eager.value = _unique("eager")
    eager_checksum = eager._graph.nodes[("value",)].current_checksum
    assert eager_checksum is not None
    assert any(role == "node:value:current" for _, role in collect_refholder_claims([eager])[eager_checksum])

    lazy = Context(eager=False)
    lazy.value = _unique("lazy")
    lazy_node = lazy._graph.nodes[("value",)]
    assert lazy_node.cell_root_producer is not None
    # A literal producer remains owned; non-eager scheduling may omit its
    # derived current result until it has an active consumer.
    lazy_checksum = lazy_node.cell_root_producer.checksum
    assert any(role == "cell:value:literal" for _, role in collect_refholder_claims([lazy])[lazy_checksum])
    eager._release_refholds()
    lazy._release_refholds()


def test_namespace_deletion_and_graph_copy_keep_independent_claims(monkeypatch):
    ctx = Context()
    ctx.sub = Context()
    ctx.sub.value = {"token": _unique("namespace")}
    checksum = ctx._graph.nodes[("sub", "value")].cell_root_producer.checksum
    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert get_buffer_cache().reference_snapshot()[checksum][0] == 4
    force_expiry(checksum)
    assert clone.sub.value.value["token"].startswith("namespace-")
    del ctx.sub
    assert get_buffer_cache().reference_snapshot()[checksum][0] == 2
    del clone.sub
    clone._release_refholds()
    force_expiry(checksum)
    _assert_miss(monkeypatch, checksum)


def test_retention_cap_prune_releases_superseded_claims():
    ctx = Context()
    for value in range(6):
        ctx.value = {"token": _unique(f"retention-{value}")}
    records = list(ctx._runtime.superseded_runs[("value",)])
    assert len(records) == 3
    checksums = [record.result_checksum for record in records]
    assert all(get_buffer_cache().reference_snapshot()[checksum][0] >= 1 for checksum in checksums)
    ctx.prune()
    assert all(get_buffer_cache().reference_snapshot().get(checksum, (0, 0, False))[0] == 0 for checksum in checksums)
    ctx._release_refholds()


def test_repeated_replacement_reaches_steady_protected_working_set():
    ctx = Context()
    historical = []
    for index in range(8):
        ctx.value = {"token": _unique(f"steady-{index}")}
        historical.append(ctx._graph.nodes[("value",)].current_checksum)
    ctx.prune()
    current = historical[-1]
    assert current is not None
    cache = get_buffer_cache()
    old_soft_cap, old_hard_cap = cache.soft_cap, cache.hard_cap
    cache.soft_cap = 0
    cache.hard_cap = 0
    try:
        cache.run_eviction_once()
        assert current in cache.strong_cache
        assert all(
            cache.reference_snapshot().get(checksum, (0, 0, False))[0] == 0
            for checksum in historical[:-1]
        )
    finally:
        cache.soft_cap, cache.hard_cap = old_soft_cap, old_hard_cap
        ctx._release_refholds()
