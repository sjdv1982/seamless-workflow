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
