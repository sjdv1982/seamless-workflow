"""Bound Cell rules from contracts/cells.md not pinned by the existing suites.

Companion of test_cells_contract_alignment.py / test_cells_wiring_contract.py
(feature 5 alignment pass, 2026-09-22); this file adds bound-cell contract
cases not covered by those suites.
"""
import copy
import re

import pytest

from seamless import Buffer, Cell, Checksum


def _text_source(ctx, name="b", value="[10, 20, 30, 40]"):
    setattr(ctx, name, Cell("text"))
    getattr(ctx, name).set(value)
    return getattr(ctx, name)


# --- Connecting: the wiring rule on bound assignment --------------------------

def test_rewiring_existing_cell_through_projection_and_conversion_raises(make_context):
    ctx = make_context()
    _text_source(ctx)
    ctx.a = Cell("plain")
    with pytest.raises(TypeError) as info:
        ctx.a = ctx.b[3]
    # Ruling 8: the refusal text is contract (cells.md §Connecting, verbatim example).
    lines = str(info.value).splitlines()
    assert lines[0] == "would convert text -> plain behind a projection."
    assert len(lines) == 3
    assert re.match(r'^\s*ctx\.a = ctx\.b\[3\]\.as_celltype\("plain"\)\s+'
                    r'# item 3 of the text \(a character\), as plain$', lines[1])
    assert re.match(r'^\s*ctx\.a = ctx\.b\.as_celltype\("plain"\)\[3\]\s+'
                    r'# item 3 of the parsed list$', lines[2])
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
    # cells.md §Connecting: the spelling "would reload ... and come back miswired" -- loading an
    # ill-formed edge is a valid request that leaves a node condition; it is not refused.
    restored.set_graph(graph)
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


def test_set_graph_checks_the_invariant_on_symbol_table_entries(make_context):
    graph, symbol = _anonymous_graph(make_context)
    entry = graph["anonymous_nodes"][symbol]
    # sym -> {source: b (text), celltype: text, path: "[3]"}: make the entry both project and convert.
    entry["celltype"] = "plain"
    restored = make_context()
    # set_graph checks entries exactly as it checks edges: the entry's node is miswired, and
    # the named result fed only by it (a root-only input) is blocked with a single reason.
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.result.state == "blocked"
    assert restored.result.block_reason == "blocked-by-miswiring"
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
        # item()/slice()/attribute projections are live handles over the parent's current checksum.
        handles = (ctx.a["x"], ctx.a["x"][0:2], ctx.a.x)
        assert [h.value for h in handles] == [[1, 2, 3], [1, 2], [1, 2, 3]]
        # A derivation never mutates the bound node.
        assert ctx.a.celltype == "plain"
        assert ctx.a.value == {"x": [1, 2, 3]}
        ctx.a.set({"x": [7, 8, 9]})
        ctx.compute(timeout=10)
        assert [h.value for h in handles] == [[7, 8, 9], [7, 8], [7, 8, 9]]
    finally:
        hold.clear()


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



# --- Ruling 4: block_reason of a complete join -------------------------------------------

def test_complete_join_has_no_block_reason(make_context):
    """ruling 4 / node-state-lifecycle.md: the dict holds only inputs that are not complete or waiting."""
    ctx = make_context()
    ctx.left = Cell("plain")
    ctx.left.set(1)
    ctx.join = Cell("plain")
    ctx.join["left"] = ctx.left
    ctx.compute(timeout=10)
    assert ctx.join.state == "complete"
    assert ctx.join.block_reason is None


# --- Ruling 8: the wiring refusal text is contract ------------------------------------------

def _pin_identity(x):
    return x


def test_pin_wiring_refusal_message_names_both_spellings(make_context):
    ctx = make_context()
    _text_source(ctx, value="[10, 20]")
    ctx.tf = _pin_identity
    ctx.tf.celltypes.x = "plain"
    with pytest.raises(TypeError) as info:
        ctx.tf.pins.x = ctx.b[3]
    # Same format as pins.md §Wiring (test_contract_pins_bound.py pins the pin side).
    lines = str(info.value).splitlines()
    assert lines[0] == "would convert text -> plain behind a projection."
    assert len(lines) == 3
    assert re.match(r'^\s*ctx\.tf\.pins\.x = ctx\.b\[3\]\.as_celltype\("plain"\)\s+'
                    r'# item 3 of the text \(a character\), as plain$', lines[1])
    assert re.match(r'^\s*ctx\.tf\.pins\.x = ctx\.b\.as_celltype\("plain"\)\[3\]\s+'
                    r'# item 3 of the parsed list$', lines[2])



# --- Clarity rulings, 2026-09-26 -------------------------------------------------------------

def test_root_edge_plus_sub_path_edge_is_refused(make_context):
    ctx = make_context()
    ctx.base = Cell("plain")
    ctx.base.set({"a": 1})
    ctx.other = Cell("plain")
    ctx.other.set(2)
    ctx.join = ctx.base
    before = copy.deepcopy(ctx.get_graph())
    with pytest.raises(Exception):  # the exception class is unruled
        ctx.join["k"] = ctx.other
    assert ctx.get_graph() == before


def test_literal_root_plus_sub_path_edges_is_a_legal_join(make_context):
    """With sub-path edges the root may hold a checksum (a literal), just not a source."""
    ctx = make_context()
    ctx.other = Cell("plain")
    ctx.other.set(2)
    ctx.join = Cell("plain")
    ctx.join.set({"a": 1})
    ctx.join["k"] = ctx.other
    ctx.compute(timeout=10)
    assert ctx.join.value == {"a": 1, "k": 2}


def test_join_refusal_message_names_both_spellings(make_context):
    ctx = make_context()
    _text_source(ctx, name="t", value="[10, 20]")
    ctx.j = Cell("plain")
    ctx.j.set({})
    with pytest.raises(TypeError) as info:
        ctx.j["left"] = ctx.t[3]
    lines = str(info.value).splitlines()
    assert lines[0] == "would convert text -> plain behind a projection."
    assert len(lines) == 3
    # "Unknown names are omitted ... the same applies at a join target ... wherever a name is
    # not known": whether this target's name is known is not ruled, so accept either rendering.
    prefix = r'^\s*(?:ctx\.j\["left"\] = ctx\.t|…|\.\.\.)'
    assert re.match(prefix + r'\[3\]\.as_celltype\("plain"\)\s+'
                    r'# item 3 of the text \(a character\), as plain$', lines[1])
    assert re.match(prefix + r'\.as_celltype\("plain"\)\[3\]\s+'
                    r'# item 3 of the parsed list$', lines[2])
    assert ctx.get_graph()["connections"] == []


def test_reassigning_a_mounted_cell_clears_the_mount_and_the_cell(make_context, tmp_path):
    ctx = make_context()
    ctx.m = Cell("text")
    ctx.m.set("hello")
    ctx.m.mount(str(tmp_path / "m.txt"), mode="w")
    ctx.compute(timeout=10)
    ctx.m = Cell("text")
    ctx.compute(timeout=10)
    assert ctx.m.state == "unwired"
    assert ctx.m.checksum is None
    assert ctx.m.mount.status.get("state") != "active"
    ctx.m.celltype = "str"  # no longer mounted, so retyping is allowed
    assert ctx.m.celltype == "str"


def _deep_ctx(ctx, celltype, members):
    member_celltype = "mixed" if celltype == "deepcell" else "bytes"
    buffers = {key: Buffer(value, member_celltype) for key, value in members.items()}
    holds = [buffer.tempref() for buffer in buffers.values()]
    index = Buffer({key: buffer.get_checksum().hex() for key, buffer in buffers.items()}, "plain")
    holds.append(index.tempref())
    ctx.d = Cell(celltype, checksum=index.get_checksum())
    ctx.compute(timeout=10)
    return holds


@pytest.mark.parametrize("form", ["checksum", "set_checksum"])
def test_bound_member_checksum_write_replaces_the_index_entry(make_context, form):
    ctx = make_context()
    holds = _deep_ctx(ctx, "deepcell", {"k": 1, "other": 2})
    replacement = Buffer(5, "mixed")
    holds.append(replacement.tempref())
    try:
        if form == "checksum":
            ctx.d["k"].checksum = replacement.get_checksum()
        else:
            ctx.d["k"].set_checksum(replacement.get_checksum())
        ctx.compute(timeout=10)
        assert ctx.d.value["k"] == replacement.get_checksum()
        assert ctx.d.value["other"] == Buffer(2, "mixed").get_checksum()
    finally:
        for hold in holds:
            hold.clear()


@pytest.mark.parametrize("celltype, value", [("deepcell", {"x": 5}), ("folder", b"folder bytes")])
def test_bound_member_value_write_is_serialized_at_the_member_celltype(make_context, celltype, value):
    ctx = make_context()
    member_celltype = "mixed" if celltype == "deepcell" else "bytes"
    holds = _deep_ctx(ctx, celltype, {"k": {"old": 1} if celltype == "deepcell" else b"old"})
    try:
        ctx.d["k"].set(value)
        ctx.compute(timeout=10)
        entry = ctx.d.value["k"]
        assert isinstance(entry, Checksum)
        assert entry == Buffer(value, member_celltype).get_checksum()
    finally:
        for hold in holds:
            hold.clear()


def test_bound_writes_below_a_deep_member_are_illegal(make_context):
    """Writes below k are illegal; the exception class is unruled."""
    ctx = make_context()
    holds = _deep_ctx(ctx, "deepcell", {"k": {"x": 1}})
    try:
        before = ctx.d.checksum
        with pytest.raises(Exception):
            ctx.d["k"]["x"].set(3)
        ctx.compute(timeout=10)
        assert ctx.d.checksum == before
    finally:
        for hold in holds:
            hold.clear()


@pytest.mark.parametrize("form", ["buffer", "set_buffer"])
def test_bound_member_buffer_write_inserts_the_buffer_checksum(make_context, form):
    ctx = make_context()
    holds = _deep_ctx(ctx, "deepcell", {"k": 1, "other": 2})
    replacement = Buffer({"x": 5}, "mixed")
    holds.append(replacement.tempref())
    try:
        if form == "buffer":
            ctx.d["k"].buffer = replacement
        else:
            ctx.d["k"].set_buffer(replacement)
        ctx.compute(timeout=10)
        assert ctx.d.value["k"] == replacement.get_checksum()
        assert ctx.d.value["other"] == Buffer(2, "mixed").get_checksum()
    finally:
        for hold in holds:
            hold.clear()


# --- The input: .source on a projection answers for that path ---------------------------------

def test_projection_source_resolves_the_edge_for_that_path(make_context):
    """cells.md §The input: the one-level edge targeting that path, else the nearest enclosing
    source, else None; deeper paths walk up. A join with a literal root reports root .source None."""
    ctx = make_context()
    ctx.left = Cell("text")
    ctx.left.set("hi")
    ctx.a = Cell("plain")
    ctx.a.set({"b": {"c": 1}, "z": 1})
    ctx.a["b"] = ctx.left
    ctx.r = Cell("plain")
    ctx.r = ctx.a
    ctx.compute(timeout=10)
    assert ctx.a.source is None
    member = ctx.a.b.source
    assert member.celltype == "text" and member.checksum == ctx.left.checksum
    deeper = ctx.a.b.c.source  # never an edge of its own: walks up to b's edge
    assert deeper.celltype == "text" and deeper.checksum == ctx.left.checksum
    assert ctx.a.z.source is None  # no edge at z or above
    enclosing = ctx.r.z.source  # no edge at z: the enclosing root edge
    assert enclosing.celltype == "plain" and enclosing.checksum == ctx.a.checksum


# --- Authority --------------------------------------------------------------------------------

def test_bound_authority_ancestor_covered_and_sibling(make_context):
    """cells.md §Authority: with an edge at ctx.a.b, ctx.a.b.c.set raises (ancestor edge),
    ctx.a.set raises (edge covered by the root write), ctx.a.other.set succeeds."""
    from seamless.cell_errors import AuthorityError
    ctx = make_context()
    ctx.src = Cell("plain")
    ctx.src.set({"c": 1})
    ctx.a = Cell("plain")
    ctx.a.set({"other": 0})
    ctx.a["b"] = ctx.src
    ctx.compute(timeout=10)
    with pytest.raises(AuthorityError):
        ctx.a.b.c.set(2)
    with pytest.raises(AuthorityError):
        ctx.a.set({"other": 5})
    # Sub-path writes never detach: the declare-family spelling checks too.
    with pytest.raises(AuthorityError):
        ctx.a.b.c.value = 2
    ctx.a.other.set(2)
    ctx.compute(timeout=10)
    assert ctx.a.value == {"other": 2, "b": {"c": 1}}


# --- Cell-level joins -----------------------------------------------------------------------------

def test_join_with_a_progressing_and_a_failed_member_is_blocked(make_context, monkeypatch):
    import asyncio
    from threading import Event
    from seamless.checksum import expression as expression_module

    ctx = make_context()
    ctx.broken = Cell("str")
    ctx.broken.set("not an integer")
    ctx.broken.celltype = "int"
    ctx.src = Cell("plain")
    ctx.src.set({"a": 1})
    ctx.compute(timeout=10)
    assert ctx.broken.state == "failed"
    entered, release = Event(), Event()
    original = expression_module._evaluate_expression_async

    async def gated(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(expression_module, "_evaluate_expression_async", gated)
    try:
        ctx.slow = ctx.src["a"]
        ctx.join = Cell("plain")
        ctx.join.set({})
        ctx.join["left"] = ctx.slow
        ctx.join["right"] = ctx.broken
        assert entered.wait(5)
        assert ctx.slow.state == "waiting"
        # A waiting input loses to everything: it contributes no entry.
        assert ctx.join.state == "blocked"
        assert ctx.join.block_reason == {"right": "blocked-by-error"}
    finally:
        release.set()


def test_a_join_observes_no_transformation(make_context, transformation_observations):
    """cells.md §Cell-level joins: a join is plain local Python -- no transformation is observed."""
    ctx = make_context()
    ctx.left = Cell("plain")
    ctx.left.set(1)
    ctx.join = Cell("plain")
    ctx.join.set({"kept": True})
    ctx.join["left"] = ctx.left
    ctx.compute(timeout=10)
    assert ctx.join.value == {"kept": True, "left": 1}
    assert transformation_observations.entries() == []


def test_integer_index_connection_requires_an_existing_sequence(make_context):
    """cells.md §Projections: an integer-index connection never infers or grows a list; the check
    happens when the value is assembled (TypeError in evaluate_cell, recorded on the join)."""
    ctx = make_context()
    ctx.x = Cell("plain")
    ctx.x.set(7)
    ctx.seq = Cell("plain")
    ctx.seq[0] = ctx.x
    ctx.compute(timeout=10)
    assert ctx.seq.state == "failed"
    assert "Integer Cell connection targets require an existing sequence" in ctx.seq.exception
    ctx.ok = Cell("plain")
    ctx.ok.set([0, 1])
    ctx.ok[1] = ctx.x
    ctx.compute(timeout=10)
    assert ctx.ok.value == [0, 7]


def test_connection_targets_deeper_than_one_level_raise_path_error_text(make_context):
    """cells.md §Projections: `ctx.a.b.c = ctx.x` raises PathError with the quoted text."""
    from seamless_workflow.errors import PathError
    ctx = make_context()
    ctx.x = Cell("plain")
    ctx.x.set(1)
    ctx.a = Cell("plain")
    ctx.a.set({"b": {"c": 0}})
    with pytest.raises(PathError, match="Cell connection targets are limited to one point component"):
        ctx.a.b.c = ctx.x
    assert ctx.get_graph()["connections"] == []
