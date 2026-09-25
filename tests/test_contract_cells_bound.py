"""Bound Cell rules from contracts/cells.md not pinned by the existing suites.

Companion of test_cells_contract_alignment.py / test_cells_wiring_contract.py
(feature 5 alignment pass, 2026-09-22); this file only adds what that pass
missed. Known gaps assert the intended result under non-strict xfail.
"""
import copy

import pytest

from seamless import Buffer, Cell, Checksum


def gap(reason):
    section, _, why = reason.partition(": ")
    return pytest.mark.xfail(strict=False, reason=f"cells.md {section}: contract ahead of code: {why}")


def _text_source(ctx, name="b", value="[10, 20, 30, 40]"):
    setattr(ctx, name, Cell("text"))
    getattr(ctx, name).set(value)
    return getattr(ctx, name)


# --- Connecting: the wiring rule on bound assignment --------------------------

@gap("§Connecting: rewiring an existing cell through a projection must not convert (ctx.a = ctx.b[3] is accepted today)")
def test_rewiring_existing_cell_through_projection_and_conversion_raises(make_context):
    ctx = make_context()
    _text_source(ctx)
    ctx.a = Cell("plain")
    with pytest.raises(TypeError):
        ctx.a = ctx.b[3]
    assert ctx.a.celltype == "plain"
    assert ctx.a.source is None
    assert ctx.get_graph()["connections"] == []


def test_projected_source_is_legal_when_celltypes_match(make_context):
    """cells.md §Connecting: legal iff the source carries no path or its celltype equals the cell's;
    a new cell copies the source's celltype once, so a fresh name is always legal."""
    ctx = make_context()
    _text_source(ctx)
    ctx.existing = Cell("text")
    ctx.existing = ctx.b[3]
    ctx.fresh = ctx.b[3]
    ctx.compute(timeout=10)
    assert ctx.fresh.celltype == "text"
    assert ctx.existing.value == ctx.fresh.value == ","


@gap("§Connecting: retyping a node fed through a path must be refused (accepted today)")
def test_retyping_a_path_fed_node_is_refused(make_context):
    ctx = make_context()
    _text_source(ctx, value="abc")
    ctx.child = ctx.b[1]
    ctx.compute(timeout=10)
    with pytest.raises(TypeError):
        ctx.child.celltype = "int"
    assert ctx.child.celltype == "text"


def test_retyping_a_source_with_projecting_consumers_is_not_refused(make_context):
    """cells.md §Connecting: retyping a source that has projecting consumers is a valid request."""
    ctx = make_context()
    _text_source(ctx, value="[1, 2]")
    ctx.child = ctx.b[1]
    ctx.compute(timeout=10)
    ctx.b.celltype = "plain"
    assert ctx.b.celltype == "plain"
    ctx.compute(timeout=10)
    assert ctx.b.value == [1, 2]


@gap("§Connecting: an ill-formed edge in a loaded graph must leave its target miswired (graph loads and answers ',' today)")
def test_loaded_edge_that_projects_and_converts_is_miswired(make_context):
    ctx = make_context()
    _text_source(ctx)
    ctx.a = Cell("text")
    ctx.a = ctx.b[3]
    graph = copy.deepcopy(ctx.get_graph())
    for node in graph["nodes"]:
        if node["path"] == ["a"]:
            node["celltype"] = "plain"
    restored = make_context()
    try:
        restored.set_graph(graph)
    except (TypeError, ValueError):
        return  # refusing the ill-formed graph outright also honours the invariant
    restored.compute(timeout=10)
    assert restored.a.state == "miswired"
    assert restored.a.checksum is None


# --- Anonymous-node symbol table ------------------------------------------------

def _anonymous_graph(make_context):
    ctx = make_context()
    _text_source(ctx)
    ctx.result = ctx.b[3].as_celltype("plain")
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    assert graph["__seamless_workflow__"] == "0.5"
    symbols = list(graph["anonymous_nodes"])
    assert len(symbols) == 1
    return graph, symbols[0]


def _rename(value, old, new):
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, dict):
        return {(new if key == old else key): _rename(item, old, new) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rename(item, old, new) for item in value]
    return value


@gap("§Connecting: anonymous_nodes / graph 0.5 not implemented; a user cell and a symbol of one name must not collide")
def test_user_cell_named_like_a_symbol_does_not_collide(make_context):
    graph, symbol = _anonymous_graph(make_context)
    graph = _rename(graph, symbol, "abcde")
    restored = make_context()
    restored.abcde = Cell("int")
    restored.abcde.set(5)
    named = restored.get_graph()["nodes"]
    graph["nodes"] = list(graph["nodes"]) + [n for n in named if n["path"] == ["abcde"]]
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.result.value == ","
    assert restored.abcde.value == 5
    assert set(restored.get_graph()["anonymous_nodes"]) == {"abcde"}


@gap("§Connecting: set_graph must check the wiring invariant on anonymous_nodes entries (not implemented)")
def test_set_graph_checks_the_invariant_on_symbol_table_entries(make_context):
    graph, symbol = _anonymous_graph(make_context)
    entry = graph["anonymous_nodes"][symbol]
    # sym -> (b, text, "[3]"): make the entry both project and convert.
    if isinstance(entry, dict):
        entry["celltype"] = "plain"
    else:
        entry = list(entry)
        entry[1] = "plain"
        graph["anonymous_nodes"][symbol] = entry
    restored = make_context()
    try:
        restored.set_graph(graph)
    except (TypeError, ValueError):
        return
    restored.compute(timeout=10)
    assert restored.result.state in ("miswired", "blocked")
    assert restored.result.checksum is None


# --- Work: bound builder methods -------------------------------------------------

def test_bound_with_derivations_are_standalone_and_navigation_stays_bound(make_context):
    """cells.md §Work, notes: with_validator/with_input give standalone snapshots; item/slice stay bound."""
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"x": [1, 2, 3]})
    ctx.compute(timeout=10)
    other = Buffer({"y": 1}, "plain")
    hold = other.tempref()
    try:
        derived = {
            "with_validator": ctx.a.with_validator(Checksum("ab" * 32)),
            "with_input": ctx.a.with_input(other.get_checksum()),
        }
        for name, cell in derived.items():
            assert cell._workflow_endpoint() is None, name
        assert derived["with_input"].value == {"y": 1}
        for cell in (ctx.a["x"], ctx.a["x"][0:2], ctx.a.x):
            assert cell._workflow_endpoint() is not None
        assert ctx.a["x"][0:2].value == [1, 2]
        # A derivation never mutates the bound node.
        assert ctx.a.celltype == "plain"
        assert ctx.a.value == {"x": [1, 2, 3]}
    finally:
        hold.clear()


@gap("§Connecting / §Work: a bound as_celltype returns a standalone snapshot, not an anonymous handle over the parent's current checksum")
def test_bound_as_celltype_is_a_live_handle_not_a_snapshot(make_context):
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"x": 1})
    ctx.compute(timeout=10)
    handle = ctx.a.as_celltype("mixed")
    assert handle.value == {"x": 1}
    ctx.a.set({"x": 2})
    ctx.compute(timeout=10)
    assert handle.value == {"x": 2}


# --- Scratch policy ---------------------------------------------------------------

def test_standalone_scratch_flag_travels_into_the_context(make_context):
    """cells.md §Scratch policy: a standalone Cell assigned into a Context brings its flag along."""
    ctx = make_context()
    cell = Cell("int")
    cell.scratch = True
    cell.set(3)
    ctx.s = cell
    assert ctx.s.scratch is True
    nodes = ctx.get_graph()["nodes"]
    assert [n["scratch"] for n in nodes if n["path"] == ["s"]] == [True]


def test_bound_as_celltype_starts_non_scratch(make_context):
    """cells.md §Scratch policy: a retyping is a new Cell that owns its own result."""
    ctx = make_context()
    ctx.s = Cell("int")
    ctx.s.set(3)
    ctx.s.scratch = True
    assert ctx.s.as_celltype("float").scratch is False


@gap("§Scratch policy: with_input()/with_validator() keep the flag; the bound derivation drops it today")
@pytest.mark.parametrize("derive", ["with_input", "with_validator"])
def test_bound_modified_copies_keep_the_scratch_flag(make_context, derive):
    ctx = make_context()
    ctx.s = Cell("int")
    ctx.s.set(3)
    ctx.s.scratch = True
    if derive == "with_input":
        copy_ = ctx.s.with_input(Buffer(4, "int").get_checksum())
    else:
        copy_ = ctx.s.with_validator(Checksum("ab" * 32))
    assert copy_.scratch is True


# --- The input: .checksum table ---------------------------------------------------

def test_reformatting_conversion_changes_the_connected_checksum(make_context):
    """cells.md §The input: a reformatting conversion (str -> text) reports a different checksum."""
    ctx = make_context()
    ctx.s = Cell("str")
    ctx.s.set("hello")
    ctx.t = ctx.s
    ctx.t.celltype = "text"
    ctx.compute(timeout=10)
    assert ctx.t.input_celltype == "str"
    assert ctx.t.checksum != ctx.s.checksum
    assert ctx.t.checksum == Buffer("hello", "text").get_checksum()
    assert ctx.t.value == "hello"


def test_standalone_capture_of_a_bound_endpoint_is_not_a_binding(make_context):
    """cells.md §Binding: Cell(source=ctx.a) references the node; it binds nothing."""
    ctx = make_context()
    ctx.a = Cell("plain")
    ctx.a.set({"k": 1})
    ctx.compute(timeout=10)
    captured = Cell(source=ctx.a)
    assert captured._workflow_endpoint() is None
    assert captured.source is not None
    assert captured.celltype == "plain"
    assert captured.value == {"k": 1}
    assert [n["path"] for n in ctx.get_graph()["nodes"]] == [["a"]]
    assert ctx.a is not ctx.a


# --- Celltypes: mounted retyping ----------------------------------------------------

@pytest.mark.parametrize("mode", ["r", "w", "rw"])
def test_mounted_cell_cannot_be_retyped(make_context, tmp_path, mode):
    """cells.md §Celltypes: retyping is refused on a mounted cell, whatever the mount mode (round 8, ruling 4)."""
    ctx = make_context()
    path = tmp_path / f"m_{mode}.txt"
    path.write_text("hello\n")
    ctx.m = Cell("text")
    if mode != "r":
        ctx.m.set("hello")
    ctx.m.mount(str(path), mode=mode)
    try:
        ctx.compute(timeout=10)
        assert ctx.m.value == "hello"
        with pytest.raises(ValueError, match="unmount first"):
            ctx.m.celltype = "str"
        assert ctx.m.celltype == "text"
    finally:
        del ctx.m.mount
    ctx.m.celltype = "str"
    assert ctx.m.celltype == "str"
