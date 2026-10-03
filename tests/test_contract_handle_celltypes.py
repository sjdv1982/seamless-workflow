"""Handle celltypes, anonymous-link miswiring and the looked-through dummy edge.

contracts/cells.md: *The input: `.source` versus `.checksum`*, *Connecting*,
and *Anonymous cells, symbols and elision* (*The handle and the node*,
*Assigning an anonymous handle*, *Which entries are serialized*), with the
`miswired` sections of contracts/node-state-lifecycle.md.

Three rules:

1. A live bound handle's ``celltype`` follows its parent; only an edge fixes
   a celltype, in the anonymous cell it creates.
2. A running Context works out the miswiring of anonymous links from their
   recorded celltypes exactly as a reload does: the live state and the state
   after ``set_graph(get_graph())`` are always equal.
3. A dummy edge from a single link is looked through, for ``.source`` and
   ``input_celltype`` as for state, exactly as the saved form reads back.
"""
import pytest

from seamless import Buffer, Cell


def identity(x):
    return x


def _held(value, celltype):
    buffer = Buffer(value, celltype)
    buffer.tempref()
    return buffer


def _deep_index():
    member = _held({"x": 5}, "mixed")
    index = _held({"member": member.get_checksum().hex()}, "plain")
    return member, index


def _mapping(ctx, name="b", celltype="mixed", value=None):
    setattr(ctx, name, Cell(celltype))
    getattr(ctx, name).set({"x": [1, 2]} if value is None else value)
    ctx.compute(timeout=10)
    return getattr(ctx, name)


def _reload(ctx, make_context):
    restored = make_context()
    restored.set_graph(ctx.get_graph())
    restored.compute(timeout=10)
    return restored


def _source_key(handle):
    source = handle.source
    if source is None:
        return None
    endpoint = source._workflow_endpoint()
    return endpoint.node_path, endpoint.local_path, endpoint.celltype


def _cell_view(cell):
    return (
        cell.state,
        cell.block_reason,
        cell.celltype,
        cell.input_celltype,
        cell.checksum,
        _source_key(cell),
    )


def _transformer_view(tf, pin="x"):
    handle = getattr(tf.pins, pin)
    return (tf.state, tf.block_reason, handle.input_celltype, _source_key(handle))


def _assert_live_equals_reloaded(ctx, restored, cells=(), transformers=()):
    for name in cells:
        assert _cell_view(getattr(restored, name)) == _cell_view(getattr(ctx, name)), name
    for name in transformers:
        assert _transformer_view(getattr(restored, name)) == _transformer_view(getattr(ctx, name)), name
    assert restored.get_graph() == ctx.get_graph()


# --- 1. A live handle's celltype follows its parent -------------------------------


def test_projection_handle_celltype_follows_a_retyped_parent(make_context):
    """cells.md, *The input*: "**A bound handle's `celltype` follows its parent.**
    Retyping the parent retypes every live handle over it, so a retype never
    leaves a bound handle `miswired`"."""
    ctx = make_context()
    _mapping(ctx)
    handle = ctx.b["x"]
    assert handle.celltype == "mixed"
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    assert handle.celltype == "plain"
    assert handle.value == [1, 2]
    assert handle.state == "complete"
    assert handle.exception is None
    assert handle.checksum == Buffer([1, 2], "plain").get_checksum()


def test_new_name_from_a_handle_takes_the_parent_current_celltype(make_context):
    """cells.md, *The input*: "the celltype is fixed only when an edge creates the
    handle's anonymous cell". *Celltypes*, copy once, at creation: "assigned to a
    new name it feeds the new node through a dummy edge, and the new node keeps the
    handle's `celltype`" -- which, for a handle taken before the retype, is the
    parent's celltype now."""
    ctx = make_context()
    _mapping(ctx)
    handle = ctx.b["x"]
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    ctx.a = handle
    ctx.compute(timeout=10)
    assert (ctx.a.celltype, ctx.a.state, ctx.a.value) == ("plain", "complete", [1, 2])
    assert ctx.a.exception is None
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert [c["source"] for c in graph["connections"] if c["target"] == ["a"]] == [["b", "x"]]
    restored = _reload(ctx, make_context)
    assert restored.a.value == [1, 2]
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a"))


def test_as_celltype_on_a_handle_after_the_retype_links_at_the_new_celltype(make_context):
    """cells.md, *The handle and the node*: "The anonymous cell is created, and its
    symbol assigned, when an edge or another entry first refers to that recipe", at
    the celltype the recipe has then.  (Pins existing behaviour.)"""
    ctx = make_context()
    _mapping(ctx)
    handle = ctx.b["x"]
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    ctx.a = handle.as_celltype("plain")
    ctx.compute(timeout=10)
    assert (ctx.a.celltype, ctx.a.state, ctx.a.value) == ("plain", "complete", [1, 2])
    entries = ctx.get_graph()["anonymous_nodes"].values()
    assert {"source": {"node": ["b"]}, "celltype": "plain", "path": "x"} in entries


def test_explicit_conversions_keep_their_targets_when_the_parent_is_retyped(make_context):
    """cells.md, *Projections*: "`as_celltype` fixes the celltype from that point in
    the chain onward"; only the celltypes that come from the parent follow it.
    (Pins existing behaviour.)"""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    convert_then_project = ctx.b.as_celltype("plain")[3]
    project_then_convert = ctx.b[3].as_celltype("plain")
    assert (convert_then_project.celltype, convert_then_project.value) == ("plain", 40)
    assert (project_then_convert.celltype, project_then_convert.value) == ("plain", ",")
    ctx.b.celltype = "str"
    ctx.compute(timeout=10)
    # The str value "[10, 20, 30, 40]" converts to the plain string, whose item 3 is ",".
    for handle in (convert_then_project, project_then_convert):
        assert handle.celltype == "plain"
        assert handle.value == ","
        assert handle.state == "complete"


def test_handle_one_step_below_a_deep_parent_follows_the_parent(make_context):
    """cells.md, *The input*: "A projection's own `celltype` is its parent's --
    except one step below a deep parent, where it is the member celltype", and
    "A bound handle's `celltype` follows its parent".  An illegal deep path still
    makes a handle `miswired` (*Anonymous and projection handles*: "it reports
    `miswired` for an ill-formed link")."""
    ctx = make_context()
    member, index = _deep_index()
    ctx.d = Cell("deepcell", checksum=index.get_checksum())
    ctx.compute(timeout=10)
    handle = ctx.d["member"]
    illegal = ctx.d[0]
    assert handle.celltype == "mixed"
    assert illegal.state == "miswired"
    ctx.d.celltype = "deepfolder"
    ctx.compute(timeout=10)
    assert handle.celltype == "bytes"
    assert handle.checksum == member.get_checksum()
    assert handle.state == "complete"
    assert illegal.state == "miswired"
    assert illegal.checksum is None


# --- 2. Miswiring of anonymous links: live equals reloaded --------------------------


def test_anonymous_link_keeps_its_celltype_and_blocks_what_it_feeds(make_context):
    """cells.md, *The handle and the node*: "Retyping its source never retypes it
    [...] After `ctx.a = ctx.b["x"].as_celltype("plain")`, with `b` a `mixed` cell,
    retyping `b` to `plain` leaves the anonymous cell for `ctx.b["x"]` reading `b`
    at `mixed`. A link that carries a path then both projects and converts, so the
    anonymous cell becomes `miswired` [...], and the cells it feeds are `blocked`
    with reason `blocked-by-miswiring`. This holds while running and after a reload
    alike." """
    ctx = make_context()
    _mapping(ctx)
    ctx.a = ctx.b["x"].as_celltype("plain")
    ctx.c = ctx.a
    ctx.compute(timeout=10)
    assert ctx.a.value == ctx.c.value == [1, 2]
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    entries = ctx.get_graph()["anonymous_nodes"].values()
    assert {"source": {"node": ["b"]}, "celltype": "mixed", "path": "x"} in entries
    # A live handle follows; the anonymous cell does not.
    assert ctx.b["x"].celltype == "plain"
    for name in ("a", "c"):
        cell = getattr(ctx, name)
        assert (cell.state, cell.block_reason) == ("blocked", "blocked-by-miswiring"), name
        assert cell.checksum is None
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a", "c"))
    # Repair: retyping back makes the recorded link well-formed again, in both.
    for context in (ctx, restored):
        context.b.celltype = "mixed"
        context.compute(timeout=10)
        assert (context.a.state, context.a.value) == ("complete", [1, 2])
        assert (context.c.state, context.c.value) == ("complete", [1, 2])


def test_anonymous_link_into_a_pin_blocks_the_transformer_live_and_reloaded(make_context):
    """contracts/pins.md, *Wiring*: "A symbol whose entry carries a path counts as
    carrying a path"; cells.md, *The handle and the node*: "the cells it feeds are
    `blocked` with reason `blocked-by-miswiring`. This holds while running and
    after a reload alike." """
    ctx = make_context()
    _mapping(ctx)
    ctx.tf = identity
    ctx.tf.celltypes.x = "plain"
    ctx.tf.pins.x = ctx.b["x"].as_celltype("plain")
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == [1, 2]
    ctx.b.celltype = "plain"
    ctx.compute(timeout=30)
    assert ctx.tf.state == "blocked"
    assert ctx.tf.block_reason == {"x": "blocked-by-miswiring"}
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b",), transformers=("tf",))


def test_chain_past_a_deep_step_is_blocked_by_miswiring_live_and_reloaded(make_context):
    """cells.md, *Connecting*: "A deep source is governed by the deep celltype's own
    rules [...] a link outside that table is statically ill-formed"; *The handle and
    the node*: the anonymous cell keeps its recorded celltype, so after the deep
    parent is retyped the step's anonymous cell is `miswired` and what it feeds is
    `blocked-by-miswiring` -- "while running and after a reload alike"."""
    ctx = make_context()
    _member, index = _deep_index()
    ctx.d = Cell("deepcell", checksum=index.get_checksum())
    ctx.r = ctx.d["member"]["x"]
    ctx.s = ctx.r
    ctx.compute(timeout=10)
    assert ctx.r.value == ctx.s.value == 5
    ctx.d.celltype = "plain"
    ctx.compute(timeout=10)
    for name in ("r", "s"):
        cell = getattr(ctx, name)
        assert (cell.state, cell.block_reason) == ("blocked", "blocked-by-miswiring"), name
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("d", "r", "s"))
    for context in (ctx, restored):
        context.d.celltype = "deepcell"
        context.compute(timeout=10)
        assert (context.r.state, context.r.value) == ("complete", 5)


def test_single_link_consumers_are_miswired_live_and_reloaded(make_context):
    """node-state-lifecycle.md, *`miswired` is a static defect*: "retyping a source
    that has projecting consumers is never refused, and each affected consumer
    becomes `miswired`". cells.md, *Assigning an anonymous handle*: the dummy edge
    of a single link is collapsed on save, "so after a reload `a` holds the link
    itself" -- and a running Context looks it through the same way.  (The states
    pin existing behaviour; the pin's `input_celltype` used to keep the celltype
    recorded at connection time while running, and so disagreed with the reload.)"""
    ctx = make_context()
    _mapping(ctx)
    ctx.a = ctx.b["x"]
    ctx.c = ctx.a
    ctx.tf = identity
    ctx.tf.pins.x = ctx.b["x"]
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == [1, 2]
    ctx.b.celltype = "plain"
    ctx.compute(timeout=30)
    assert (ctx.a.state, ctx.a.block_reason) == ("miswired", None)
    assert (ctx.c.state, ctx.c.block_reason) == ("blocked", "blocked-by-miswiring")
    assert ctx.tf.state == "miswired"
    assert ctx.tf.block_reason == {"x": "miswired"}
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a", "c"), transformers=("tf",))


def test_single_link_into_a_join_slot_is_judged_alike_live_and_reloaded(make_context):
    """cells.md, *Connecting*: at a one-level target "`ctx.j["left"] = ctx.t[3]`, with
    a `text` source and a `plain` join, is `text` against `plain`"; a retype that
    makes the link project and convert leaves the slot's input `miswired`.  The
    identity edge from the single link is looked through, so the running Context
    judges the slot exactly as the reload, which holds the link as the slot's own
    edge.  (How a join reports a miswired member is a separate gap,
    node-state-lifecycle.md, *Implementation status*; only its entry for `k` is
    pinned here.)"""
    ctx = make_context()
    _mapping(ctx, celltype="plain")
    ctx.j = Cell("plain")
    ctx.j["k"] = ctx.b["x"]
    ctx.compute(timeout=10)
    assert ctx.j.value == {"k": [1, 2]}
    ctx.b.celltype = "mixed"
    ctx.compute(timeout=10)
    assert ctx.j.state != "complete"
    assert ctx.j.checksum is None
    assert ctx.j.block_reason["k"] in ("miswired", "blocked-by-miswiring")
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "j"))


def test_single_link_made_two_deep_steps_by_a_retype_is_miswired_live_and_reloaded(make_context):
    """cells.md, *Connecting*: over a deep source, what is legal is "the
    exactly-one-string-item step [...] and nothing else. [...] a link outside that
    table is statically ill-formed, and bound that leaves the node `miswired`".
    node-state-lifecycle.md: retyping the source "is never refused, and each
    affected consumer becomes `miswired`" -- while running and after a reload
    alike, so the ill-formed link is saved with its entry rather than as an own
    edge that would read back as a legal deep step plus a path."""
    ctx = make_context()
    _member, index = _deep_index()
    ctx.b = Cell("mixed")
    ctx.b.set({"member": {"x": 5}})
    ctx.a = ctx.b["member"]["x"]
    ctx.compute(timeout=10)
    assert (ctx.a.celltype, ctx.a.value) == ("mixed", 5)
    ctx.b.set_checksum(index.get_checksum())
    ctx.b.celltype = "deepcell"
    ctx.compute(timeout=10)
    assert (ctx.a.state, ctx.a.block_reason) == ("miswired", None)
    assert ctx.a.checksum is None
    graph = ctx.get_graph()
    assert list(graph["anonymous_nodes"].values()) == [
        {"source": {"node": ["b"]}, "celltype": "mixed", "path": "member.x"}
    ]
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a"))


# --- 3. A dummy edge from a single link is looked through ---------------------------


@pytest.mark.parametrize("target", ["new name", "existing name"])
def test_conversion_link_reports_the_link_source(make_context, target):
    """cells.md, *The input*: "**A dummy edge from a single link is looked
    through**: a cell fed that way [...] reports the link's own source, so after
    `ctx.a = ctx.b.as_celltype("plain")`, with `b` a `text` cell, `ctx.a.source` is
    `ctx.b` and `ctx.a.input_celltype` is `text`. That is what `a` reports after a
    save and reload too, since `get_graph()` saves exactly that link as `a`'s own
    edge."  An existing `plain` name fed the same way gets an identity edge too."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[1, 2]")
    if target == "existing name":
        ctx.a = Cell("plain")
    ctx.a = ctx.b.as_celltype("plain")
    ctx.compute(timeout=10)
    assert ctx.a.celltype == "plain"
    assert ctx.a.input_celltype == "text"
    assert ctx.a.source._workflow_endpoint() == ctx.b._workflow_endpoint()
    assert ctx.a.value == [1, 2]
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert [c["source"] for c in graph["connections"]] == [["b"]]
    restored = _reload(ctx, make_context)
    assert restored.a.input_celltype == "text"
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a"))


def test_projection_link_reports_the_link_source(make_context):
    """cells.md, *The input*: "A dummy edge from a single link is looked through: a
    cell fed that way [...] reports the link's own source", for `ctx.a = ctx.b["x"]`
    as for a conversion.  (Pins existing behaviour.)"""
    ctx = make_context()
    _mapping(ctx)
    ctx.a = ctx.b["x"]
    ctx.compute(timeout=10)
    assert _source_key(ctx.a) == (("b",), ("x",), "mixed")
    assert ctx.a.input_celltype == "mixed"
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "a"))


def test_non_identity_edge_from_a_single_link_is_not_looked_through(make_context):
    """cells.md, *Assigning an anonymous handle*: "An existing name gains an edge
    from the symbol. [...] `a` keeps its own `celltype` and converts into it"; and
    *Which entries are serialized*: only an identity is saved as the target's own
    link.  `input_celltype` is the anonymous cell's, live and reloaded.  (Pins
    existing behaviour.)"""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set('"hello"')
    ctx.s = Cell("str")
    ctx.s = ctx.b.as_celltype("plain")
    ctx.compute(timeout=10)
    assert (ctx.s.celltype, ctx.s.input_celltype, ctx.s.value) == ("str", "plain", "hello")
    graph = ctx.get_graph()
    assert list(graph["anonymous_nodes"].values()) == [
        {"source": {"node": ["b"]}, "celltype": "plain", "path": ""}
    ]
    restored = _reload(ctx, make_context)
    _assert_live_equals_reloaded(ctx, restored, cells=("b", "s"))


def test_retyping_the_target_ends_the_look_through(make_context):
    """cells.md, *Which entries are serialized*: the dummy edge is collapsed only
    while "the edge is an identity"; once the target is retyped it is an edge from
    the symbol that "converts into" the target's own celltype.  Live and reloaded
    agree before and after."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set('"hello"')
    ctx.a = ctx.b.as_celltype("plain")
    ctx.compute(timeout=10)
    assert ctx.a.input_celltype == "text"
    _assert_live_equals_reloaded(ctx, _reload(ctx, make_context), cells=("b", "a"))
    ctx.a.celltype = "str"
    ctx.compute(timeout=10)
    assert (ctx.a.celltype, ctx.a.input_celltype, ctx.a.value) == ("str", "plain", "hello")
    assert len(ctx.get_graph()["anonymous_nodes"]) == 1
    _assert_live_equals_reloaded(ctx, _reload(ctx, make_context), cells=("b", "a"))


def test_pin_fed_by_a_single_conversion_link_reports_the_link_source(make_context):
    """cells.md, *Which entries are serialized*: the identity is judged at "the
    celltype at the target -- a cell's, a join slot's or a pin's"; pins.md:
    `pin.input_celltype` "comes from the input, by the rules `contracts/cells.md`
    gives for a Cell"."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[1, 2]")
    ctx.tf = identity
    ctx.tf.celltypes.x = "plain"
    ctx.tf.pins.x = ctx.b.as_celltype("plain")
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == [1, 2]
    assert ctx.tf.pins.x.input_celltype == "text"
    assert ctx.tf.pins.x.source._workflow_endpoint() == ctx.b._workflow_endpoint()
    restored = _reload(ctx, make_context)
    assert restored.tf.result.value == [1, 2]
    _assert_live_equals_reloaded(ctx, restored, cells=("b",), transformers=("tf",))
