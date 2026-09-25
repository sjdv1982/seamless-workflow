"""The six projection writes, bound: pathed read-modify-set writes to the parent.

Paired with seamless-core/tests/test_cells_projection_writes.py, where the same
rule now holds standalone (cells.md, register/cells-RULINGS.md round 9). The
documented write bugs are xfailed individually; authority failures never count
as successful writes.
"""
import pytest
from seamless import Buffer, Cell
from seamless.cell_errors import AuthorityError


FORMS = ["value", "buffer", "checksum"]


def write(cell, form, method, value):
    if method:
        getattr(cell, {"value": "set", "buffer": "set_buffer", "checksum": "set_checksum"}[form])(value)
    else:
        setattr(cell, form, value)


@pytest.mark.parametrize("form", [
    "value",
    pytest.param("buffer", marks=pytest.mark.xfail(strict=False, reason="feature 5 bug 1: subpath buffer writes pass unsupported input_celltype")),
    pytest.param("checksum", marks=pytest.mark.xfail(strict=False, reason="feature 5 bug 1: subpath checksum writes pass unsupported input_celltype")),
])
@pytest.mark.parametrize("method", [False, True])
def test_projection_write_matrix(make_context, form, method):
    ctx = make_context()
    ctx.root = Cell("plain")
    ctx.root.set({"a": 1, "other": 2})
    buffer = Buffer(3, "plain")
    hold = buffer.tempref()
    try:
        value = {"value": 3, "buffer": buffer, "checksum": buffer.get_checksum()}[form]
        write(ctx.root["a"], form, method, value)
        ctx.compute(timeout=10)
        assert ctx.root.value == {"a": 3, "other": 2}
        assert ctx.root["a"].value == 3
    finally:
        hold.clear()


@pytest.mark.parametrize("form", [
    "value",
    pytest.param("buffer", marks=pytest.mark.xfail(strict=False, reason="feature 5 bug 1: checksum_rhs must reach authority validation")),
    pytest.param("checksum", marks=pytest.mark.xfail(strict=False, reason="feature 5 bug 1: checksum_rhs must reach authority validation")),
])
@pytest.mark.parametrize("method", [False, True])
def test_projection_write_under_source_is_refused(make_context, form, method):
    ctx = make_context()
    ctx.source = {"a": 1}
    ctx.root = ctx.source
    buffer = Buffer(3, "mixed")
    hold = buffer.tempref()
    try:
        value = {"value": 3, "buffer": buffer, "checksum": buffer.get_checksum()}[form]
        with pytest.raises(AuthorityError):
            write(ctx.root["a"], form, method, value)
        ctx.compute(timeout=10)
        assert ctx.root.value == ctx.source.value == {"a": 1}
    finally:
        hold.clear()


def test_subpath_assignment_and_augmented_assignment_are_pathed_root_writes(make_context):
    ctx = make_context()
    ctx.root = Cell("plain")
    ctx.root.a.b = 2
    assert ctx.root.value == {"a": {"b": 2}}
    ctx.root.a.b += 3
    assert ctx.root.value == {"a": {"b": 5}}
    retained = ctx.root.a.b
    retained += 7
    assert ctx.root.value == {"a": {"b": 12}}
    # A self-edge would block subsequent writes or corrupt recomputation.
    ctx.root.a.b = 17
    ctx.compute(timeout=10)
    assert ctx.root.value == {"a": {"b": 17}}


def test_sibling_write_preserves_join_edge_and_root_method_checks_covered_edge(make_context):
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.source.set(7)
    ctx.root = Cell("plain")
    ctx.root.set({"owned": 1})
    ctx.root["connected"] = ctx.source
    ctx.compute(timeout=10)
    ctx.root.owned.set(11)
    with pytest.raises(AuthorityError):
        ctx.root.set({"owned": 13})
    with pytest.raises(AuthorityError):
        ctx.root.connected.value = 17
    ctx.source.set(19)
    ctx.compute(timeout=10)
    assert ctx.root.value == {"owned": 11, "connected": 19}
    del ctx.root["connected"]
    ctx.source.set(23)
    ctx.compute(timeout=10)
    assert ctx.root.value == {"owned": 11}


def test_null_root_does_not_bootstrap_a_mapping(make_context):
    ctx = make_context()
    ctx.root = Cell("plain")
    ctx.root.set(None)
    with pytest.raises((TypeError, ValueError)):
        ctx.root.a.b = 1
    assert ctx.root.value is None


def test_connection_targets_are_one_point_component(make_context):
    from seamless_workflow.errors import PathError
    ctx = make_context()
    ctx.source = Cell("plain")
    ctx.source.set(29)
    ctx.root = Cell("plain")
    ctx.root.set({"a": {"b": 1}})
    with pytest.raises(PathError):
        ctx.root.a.b = ctx.source
    with pytest.raises(PathError):
        ctx.root[:] = ctx.source


def test_delete_invalidates_retained_handle_but_null_does_not(make_context):
    from seamless_workflow.errors import StaleWorkflowHandleError
    ctx = make_context()
    ctx.root = Cell("plain")
    retained = ctx.root
    ctx.root = None
    assert retained.value is None
    assert retained.state == "complete"
    del ctx.root
    with pytest.raises(StaleWorkflowHandleError):
        _ = retained.checksum
