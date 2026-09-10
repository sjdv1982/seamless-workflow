from __future__ import annotations

import gc

import pytest

from seamless import Buffer, Cell
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import audit_reference_accounting
from seamless_workflow import Context


def identity(value):
    return value


def _count(checksum):
    return get_buffer_cache().reference_snapshot().get(checksum, (0, 0, False))[0]


def test_checksum_backed_cell_binding_adopts_before_builder_release():
    buffer = Buffer(17, "int")
    checksum = buffer.get_checksum()
    cell = Cell(checksum, celltype="int")
    ctx = Context()
    assert _count(checksum) == 1
    ctx.value = cell
    assert cell._refholds_released is True
    assert _count(checksum) == 2  # graph producer plus current result
    assert any(role == "cell:value:literal" for _, role in ctx._refheld_checksums())
    del ctx
    del cell
    del buffer
    gc.collect()
    assert _count(checksum) == 0


def test_checksum_backed_transformer_binding_releases_standalone_builder():
    from seamless.transformer import delayed

    buffer = Buffer(19, "int")
    checksum = buffer.get_checksum()
    builder = delayed(identity)
    builder.args.value = checksum
    standalone_count = _count(checksum)
    ctx = Context()
    ctx.transformer = builder
    node = ctx._graph.nodes[("transformer",)]
    assert builder._refholds_released is True
    ctx.compute(timeout=10)
    assert _count(checksum) == standalone_count + 1
    assert any(
        role == "transformer:transformer:pin:value"
        for _, role in ctx._refheld_checksums()
    )
    assert any(
        role == "transformer:transformer:code"
        for _, role in ctx._refheld_checksums()
    )
    assert node.transformer_pin_producers["value"].checksum == checksum


def test_failed_builder_binding_rolls_back_and_keeps_builder_standalone():
    cell = Cell("value", celltype="not-a-celltype")
    ctx = Context()
    with pytest.raises(TypeError):
        ctx.value = cell
    assert ("value",) not in ctx._graph.nodes
    assert cell._workflow_backend is None
    assert cell._refholds_released is False
    cell._release_refholds()


def test_namespace_deletion_removes_descendants_and_roles():
    ctx = Context()
    ctx.sub = Context()
    ctx.sub.value = {"token": "namespace-delete"}
    checksum = ctx._graph.nodes[("sub", "value")].cell_root_producer.checksum
    assert _count(checksum) == 2
    del ctx.sub
    assert not any(path[:1] == ("sub",) for path in ctx._graph.nodes)
    assert ("sub",) not in ctx._graph.namespaces
    assert _count(checksum) == 0


def test_context_code_claim_is_derived_from_graph_state(caplog):
    ctx = Context()
    ctx.transformer = identity
    path = ("transformer",)
    checksum = ctx._graph.nodes[path].transformer_config.code_checksum
    checksum.decref_refholder()  # deliberate omitted acquisition
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx])
    assert "live claims" in caplog.text
    checksum.incref_refholder()
    ctx._release_refholds()


def test_checksum_module_replacement_releases_old_role():
    ctx = Context()
    ctx.transformer = identity
    first = Buffer(b"module-replacement-first").get_checksum()
    second = Buffer(b"module-replacement-second").get_checksum()

    ctx.transformer.modules.example = first
    assert _count(first) == 1
    ctx.transformer.modules.example = second

    assert _count(first) == 0
    assert _count(second) == 1
    assert (second, "transformer:transformer:module:example") in tuple(
        ctx._refheld_checksums()
    )

    ctx._release_refholds()
    assert _count(second) == 0


@pytest.mark.parametrize("failing_step", ["pin", "code", "module"])
def test_failed_transformer_staging_preserves_graph_and_roles(monkeypatch, caplog, failing_step):
    """A replacement that fails before publication is a rejected request (§12.3).

    Staging acquires the pin producer, then the code checksum, then module
    checksums.  Failing at each step checks that whatever was staged before it
    is released, that the live node is untouched, and that the Context stays
    usable.
    """
    import seamless_workflow.context as context_module
    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = 31
    ctx.compute(timeout=10)
    path = ("transformer",)
    node = ctx._graph.nodes[path]
    old_config = node.transformer_config
    old_producer = node.transformer_pin_producers["value"]
    old_claims = tuple(ctx._refheld_checksums())

    replacement_checksum = Buffer(32, "int").get_checksum()
    module_checksum = Buffer(b"staging-failure-module").get_checksum()
    replacement = delayed(identity)
    replacement.args.value = replacement_checksum
    replacement.modules.example = module_checksum
    staged = {
        "pin": replacement_checksum,
        "code": old_config.code_checksum,  # same code: staged as a second hold
        "module": module_checksum,
    }
    counts = {step: _count(checksum) for step, checksum in staged.items()}

    normalize_checksum = context_module.normalize_checksum

    def fail_at_step(checksum):
        if checksum == staged[failing_step]:
            raise RuntimeError("forced replacement staging failure")
        return normalize_checksum(checksum)

    with monkeypatch.context() as patch:
        patch.setattr(context_module, "normalize_checksum", fail_at_step)
        with pytest.raises(RuntimeError, match="forced replacement staging failure"):
            ctx.transformer = replacement

    node = ctx._graph.nodes[path]
    assert node.transformer_config is old_config
    assert node.transformer_pin_producers["value"] is old_producer
    assert tuple(ctx._refheld_checksums()) == old_claims
    assert {step: _count(checksum) for step, checksum in staged.items()} == counts
    assert replacement._workflow_backend is None
    assert replacement._refholds_released is False

    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx, replacement])
    assert caplog.text == ""

    # A rejected request neither poisons the Context nor fails the live node.
    assert ctx.transformer.state == "complete"
    assert ctx.transformer.result.value == 31

    replacement._release_refholds()
    ctx._release_refholds()


def test_failure_after_transformer_publication_poisons_the_context(monkeypatch, caplog):
    """An internal failure after publication must poison or stop the Context (§12.3).

    Publication is the commit point and MOD-5 forbids rolling it back, so a
    Context that stays live after this failure reports the old node's result
    under the new configuration.
    """
    from seamless.transformer import delayed
    from seamless_workflow.errors import ClosedContextError, ControllerFailedError

    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = 31
    ctx.compute(timeout=10)
    handle = ctx.transformer

    replacement_buffer = Buffer(32, "int")  # keeps the new pin resolvable
    replacement = delayed(identity)
    replacement.args.value = replacement_buffer.get_checksum()

    def fail_after_publication():
        raise RuntimeError("forced failure after publication")

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "_derive_all", fail_after_publication)
        with pytest.raises(RuntimeError, match="forced failure after publication"):
            ctx.transformer = replacement

    operations = {
        "state": lambda: ctx.transformer.state,
        "result": lambda: ctx.transformer.result.value,
        "held handle": lambda: handle.result.value,
        "barrier": lambda: ctx.compute(timeout=10),
        "write": lambda: setattr(ctx, "other", 1),
    }
    still_live = {}
    for name, operation in operations.items():
        try:
            still_live[name] = operation()
        except (ControllerFailedError, ClosedContextError):
            pass
    assert still_live == {}, f"the Context stayed live after an internal failure: {still_live}"

    ctx.close()
    assert ctx._refheld_checksums() == ()
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx, replacement])
    assert caplog.text == ""
    replacement._release_refholds()


def test_transformer_replacement_transfers_pin_and_module_roles():
    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = identity
    old_pin = Buffer(41, "int").get_checksum()
    old_module = Buffer(b"replacement-old-module").get_checksum()
    ctx.transformer.pins.value = old_pin
    ctx.compute(timeout=10)
    ctx.transformer.modules.example = old_module

    new_pin = Buffer(42, "int").get_checksum()
    new_module = Buffer(b"replacement-new-module").get_checksum()
    replacement = delayed(identity)
    replacement.args.value = new_pin
    replacement.modules.example = new_module

    ctx.transformer = replacement

    ctx.compute(timeout=10)
    assert _count(old_pin) == 0
    assert _count(old_module) == 0
    assert _count(new_pin) == 1
    assert _count(new_module) == 1
    assert replacement._refholds_released is True
    assert replacement._workflow_backend is not None

    ctx._release_refholds()
    assert _count(new_pin) == 0
    assert _count(new_module) == 0
