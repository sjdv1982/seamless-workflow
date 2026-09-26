"""Bound (Context) side of contracts/deep-celltypes.md, per the 2026-09-26 cells rulings.

- one step below a deep parent carries the member celltype (cells.md, Projections: line 253);
- checksum/buffer writes through that handle are resolved at the member celltype
  (cells.md, Writes through a handle).
Known gaps are non-strict xfails that assert the contract, never the bug.
"""
import pytest

from seamless import Buffer, CacheMissError, Cell, Checksum
from seamless.checksum.calculate_checksum import calculate_checksum

DEEP_MEMBER = [("deepcell", "mixed"), ("deepfolder", "bytes"), ("folder", "bytes")]

_BOUND_MEMBER_GAP = pytest.mark.xfail(
    strict=False,
    reason="deep-celltypes.md §Implementation status (Bound handles keep the deep celltype): "
    "contract ahead of code: a bound "
    "one-step projection of a deep parent keeps the parent's deep celltype, so reading it "
    "raises 'Illegal deep path ... deepcell -> deepcell'",
)
_BOUND_WRITE_GAP = pytest.mark.xfail(
    strict=False,
    reason="deep-celltypes.md §Implementation status (Bound writes at k fail or use the wrong "
    "celltype): contract ahead of code: through ctx.a['k'] the checksum forms raise TypeError (_edit() got an unexpected "
    "keyword argument 'input_celltype') and the buffer forms are validated at the parent's "
    "deep celltype instead of the member celltype (ValueError / HashTypeValidationError)",
)


def _held(value, celltype=None):
    buffer = Buffer(value, celltype) if celltype else Buffer(value)
    buffer.tempref()
    return buffer


def _member(celltype, tag):
    if celltype == "deepcell":
        return _held({"v": tag}, "mixed")
    return _held(f"member {tag}".encode())


@_BOUND_MEMBER_GAP
@pytest.mark.parametrize("celltype,member", DEEP_MEMBER)
def test_bound_one_step_projection_carries_the_member_celltype(make_context, celltype, member):
    old = _member(celltype, "old")
    index = _held({"k": old.get_checksum().hex()}, "plain")
    ctx = make_context()
    ctx.a = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    handle = ctx.a["k"]
    assert handle.celltype == member
    assert handle.input_celltype == celltype
    assert handle.checksum == old.get_checksum()


@_BOUND_WRITE_GAP
@pytest.mark.parametrize("form", ["checksum", "set_checksum", "buffer", "set_buffer"])
@pytest.mark.parametrize("celltype,member", DEEP_MEMBER)
def test_bound_handle_write_below_a_deep_parent_replaces_the_member_checksum(
    make_context, celltype, member, form
):
    old = _member(celltype, "old")
    new = _member(celltype, "new")
    index = _held({"k": old.get_checksum().hex(), "other": old.get_checksum().hex()}, "plain")
    ctx = make_context()
    ctx.a = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    handle = ctx.a["k"]
    argument = new.get_checksum() if "checksum" in form else new
    if form.startswith("set_"):
        getattr(handle, form)(argument)
    else:
        setattr(handle, form, argument)
    ctx.compute(timeout=10)
    assert ctx.a.value == {"k": new.get_checksum(), "other": old.get_checksum()}


@_BOUND_WRITE_GAP
@pytest.mark.parametrize("celltype,member", DEEP_MEMBER)
def test_bound_handle_write_with_absent_buffer_raises_cache_miss_and_records_nothing(
    make_context, celltype, member
):
    old = _member(celltype, "old")
    index = _held({"k": old.get_checksum().hex()}, "plain")
    ctx = make_context()
    ctx.a = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    absent = Checksum(calculate_checksum(b"bound deep handle write, never stored " + celltype.encode()))
    with pytest.raises(CacheMissError):
        ctx.a["k"].checksum = absent
    ctx.compute(timeout=10)
    assert ctx.a.checksum == index.get_checksum()


# --- Value writes at k and writes below k ------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason="deep-celltypes.md §Implementation status (Bound writes at k fail or use the wrong "
    "celltype: the value forms do not produce the member checksum): contract ahead of code",
)
@pytest.mark.parametrize("celltype,member", DEEP_MEMBER)
def test_bound_value_write_at_k_inserts_the_member_checksum(make_context, celltype, member):
    old = _member(celltype, "old")
    index = _held({"k": old.get_checksum().hex(), "other": old.get_checksum().hex()}, "plain")
    ctx = make_context()
    ctx.a = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    new_value = {"v": "new"} if member == "mixed" else b"new bytes"
    ctx.a["k"].set(new_value)
    ctx.compute(timeout=10)
    expected = Buffer(new_value, member).get_checksum()
    assert ctx.a.value == {"k": expected, "other": old.get_checksum()}


@pytest.mark.parametrize("form", ["set", "checksum", "buffer"])
@pytest.mark.parametrize("celltype,member", DEEP_MEMBER)
def test_bound_write_below_k_is_refused_and_records_nothing(make_context, celltype, member, form):
    """Ruling: writes below a deep parent are illegal below k (exception type unruled)."""
    old = _held({"v": 1}, "mixed")
    index = _held({"k": old.get_checksum().hex()}, "plain")
    ctx = make_context()
    ctx.a = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    replacement = _held(5, "mixed")
    with pytest.raises(Exception):
        target = ctx.a["k"]["v"]
        if form == "set":
            target.set(5)
        elif form == "checksum":
            target.checksum = replacement.get_checksum()
        else:
            target.buffer = replacement
        ctx.compute(timeout=10)
    ctx.compute(timeout=10)
    assert ctx.a.checksum == index.get_checksum()


# --- Graph edges into a deep cell --------------------------------------------


def test_pin_celltypes_is_the_set_of_sub_path_edge_targets():
    from seamless_workflow.context import PIN_CELLTYPES

    assert PIN_CELLTYPES == {"plain", "mixed", "deepcell", "deepfolder", "folder"}


@pytest.mark.parametrize("celltype", ["plain", "mixed", "deepcell", "deepfolder", "folder"])
def test_sub_path_edge_into_a_container_capable_cell_is_accepted(make_context, celltype):
    ctx = make_context()
    ctx.x = Cell("mixed")
    ctx.c = Cell(celltype)
    ctx.c["k"] = ctx.x  # edge construction must not raise


@pytest.mark.parametrize("celltype", ["int", "text", "bytes", "checksum"])
def test_sub_path_edge_into_another_celltype_is_a_path_error(make_context, celltype):
    from seamless_workflow.errors import PathError

    ctx = make_context()
    ctx.x = Cell("mixed")
    ctx.c = Cell(celltype)
    with pytest.raises(PathError, match="Cell subvalue connections require a container-capable Cell"):
        ctx.c["k"] = ctx.x
