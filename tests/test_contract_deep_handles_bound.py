"""Bound handles and cells one step below a deep parent.

Pins the "Bound handles keep the deep celltype" gap of
contracts/deep-celltypes.md, *Implementation status*, together with the deep
step's fusion barrier (contracts/expressions.md, *Fusion*), for the workflow
Context: projection and ``as_celltype`` handles, named cells, anonymous chains,
transformer pins, ``build()`` and the ``set_graph(get_graph())`` round trip.

Bound and standalone *writes* at ``k`` are a separate gap and are not covered
here (test_contract_deep_celltypes_bound.py covers them).
"""
import pytest

from seamless import Buffer, Cell, Checksum
from seamless_workflow.builder_state import _path_string

DEEP_MEMBER = [("deepcell", "mixed"), ("deepfolder", "bytes"), ("folder", "bytes")]


def _held(value, celltype):
    buffer = Buffer(value, celltype)
    buffer.tempref()
    return buffer


def _member(celltype):
    if celltype == "deepcell":
        return _held({"x": 5}, "mixed")
    return _held(b"member bytes", "bytes")


def _member_value(celltype):
    return {"x": 5} if celltype == "deepcell" else b"member bytes"


def _deep(ctx, celltype="deepcell", name="d"):
    """A deep cell whose index holds one member, ``"member"``."""
    member = _member(celltype)
    index = _held({"member": member.get_checksum().hex()}, "plain")
    setattr(ctx, name, Cell(celltype, checksum=index.get_checksum()))
    ctx.compute(timeout=10)
    return member, index


def plus_one(value):
    return value + 1


def make_deep():
    return {"member": {"x": 5}}


def _anonymous_roles(ctx):
    return sorted(
        (checksum.hex(), role)
        for checksum, role in ctx._refheld_checksums()
        if role.startswith("anonymous:")
    )


# --- Item 1: one step below a deep parent ------------------------------------


@pytest.mark.parametrize("celltype,member_type", DEEP_MEMBER)
def test_bound_one_step_handle_reads_the_child(make_context, celltype, member_type):
    """deep-celltypes.md, *Handles and writes one step below a deep parent*:
    "`d["k"].celltype` is the member celltype, `d["k"].checksum` is the child's
    checksum, and `d["k"].value` is the child's value."
    """
    ctx = make_context()
    member, _ = _deep(ctx, celltype)
    handle = ctx.d["member"]
    assert handle.celltype == member_type
    assert handle.checksum == member.get_checksum()
    assert handle.value == _member_value(celltype)
    assert handle.state == "complete"
    assert handle.exception is None


@pytest.mark.parametrize("celltype,member_type", DEEP_MEMBER)
def test_bound_one_step_handle_builds_the_one_step_expression(make_context, celltype, member_type):
    """deep-celltypes.md, *What the one step yields*: the one-step Expression
    "`(index, "['k']", deep, member celltype)`" has as result checksum "the
    child's own checksum", with no new buffer.
    """
    ctx = make_context()
    member, index = _deep(ctx, celltype)
    built = ctx.d["member"].build()
    assert built.source is None
    assert built.input_checksum == index.get_checksum()
    assert built.path == _path_string(("member",))
    assert (built.input_celltype, built.celltype) == (celltype, member_type)
    assert built.compute() == member.get_checksum()


@pytest.mark.parametrize("celltype,member_type", DEEP_MEMBER)
def test_bound_named_one_step_cell_takes_the_member_celltype_and_the_child_checksum(
    make_context, celltype, member_type
):
    """cells.md, *Projections*: "A projection's own `celltype` is its parent's —
    except one step below a deep parent, where it is the member celltype
    (`mixed` for `deepcell`, `bytes` for `deepfolder` and `folder`)."
    deep-celltypes.md, *What the one step yields*: "Its result checksum *is*
    the child's".
    """
    ctx = make_context()
    member, _ = _deep(ctx, celltype)
    ctx.k = ctx.d["member"]
    ctx.compute(timeout=10)
    assert ctx.k.celltype == member_type
    assert ctx.k.state == "complete"
    assert ctx.k.checksum == member.get_checksum()
    assert ctx.k.value == _member_value(celltype)


# --- Item 2: an anonymous chain past the deep step -----------------------------


def test_bound_anonymous_chain_past_a_deep_step_is_evaluated_over_the_child(make_context):
    """expressions.md, *Fusion*: "A deep step is a barrier and forms no pair.
    [...] The run ends at the deep step, and anything after it (a further path,
    or the child's own conversion) is a separate Expression over the child
    checksum."
    """
    ctx = make_context()
    _deep(ctx)
    ctx.r = ctx.d["member"]["x"]
    ctx.compute(timeout=10)
    assert ctx.r.state == "complete"
    assert ctx.r.celltype == "mixed"
    assert ctx.r.value == 5


def test_bound_handle_past_a_deep_step_reads_and_builds_over_the_child(make_context):
    """expressions.md, *Fusion*: "anything after it (a further path, or the
    child's own conversion) is a separate Expression over the child checksum."
    The handle's recipe and the named node's recipe are both a two-member chain.
    """
    ctx = make_context()
    member, index = _deep(ctx)
    handle = ctx.d["member"]["x"]
    assert handle.celltype == "mixed"
    assert handle.value == 5
    ctx.r = handle
    ctx.compute(timeout=10)
    for built in (handle.build(), ctx.r.build()):
        assert (built.path, built.input_celltype, built.celltype) == (
            _path_string(("x",)), "mixed", "mixed",
        )
        step = built.source
        assert step is not None, "the deep step is fused into the outer Expression"
        assert step.input_checksum == index.get_checksum()
        assert (step.path, step.input_celltype, step.celltype) == (
            _path_string(("member",)), "deepcell", "mixed",
        )
        assert built.compute() == Buffer(5, "mixed").get_checksum()
        assert built.input_checksum == member.get_checksum()


def test_bound_deep_step_anonymous_cell_is_not_elided(make_context):
    """cells.md, *Anonymous cells, symbols and elision*: "An anonymous cell that
    is not elided is the ordinary consequence of a fusion barrier — a conversion
    that produces a new buffer, or a deep step [...]. It is then a real member of
    the chain: the Context evaluates it like any other node, so its Expression is
    built and its result checksum is produced and cached".
    """
    ctx = make_context()
    member, _ = _deep(ctx)
    ctx.r = ctx.d["member"]["x"]
    ctx.compute(timeout=10)
    graph = ctx.get_graph()
    (edge,) = [edge for edge in graph["connections"] if edge["target"] == ["r"]]
    outer = edge["source"]["symbol"]
    entries = graph["anonymous_nodes"]
    step = entries[outer]["source"]["symbol"]
    assert entries == {
        step: {"source": {"node": ["d"]}, "celltype": "mixed", "path": _path_string(("member",))},
        outer: {"source": {"symbol": step}, "celltype": "mixed", "path": _path_string(("x",))},
    }
    held = [
        (checksum, role)
        for checksum, role in ctx._refheld_checksums()
        if role.startswith("anonymous:")
    ]
    assert held == [(member.get_checksum(), f"anonymous:{step}:current")]


# --- Item 3: a named chain through the deep step ------------------------------


def test_bound_named_chain_through_a_deep_step(make_context):
    """deep-celltypes.md, *What the one step yields*: "Its result checksum *is*
    the child's, so `compute()` answers the child's checksum, and every later
    Expression over that result is keyed at the **child**".
    """
    ctx = make_context()
    member, _ = _deep(ctx)
    ctx.m = ctx.d["member"]
    ctx.r = ctx.m["x"]
    ctx.compute(timeout=10)
    assert (ctx.m.state, ctx.m.celltype) == ("complete", "mixed")
    assert ctx.m.checksum == member.get_checksum()
    assert (ctx.r.state, ctx.r.value) == ("complete", 5)
    built = ctx.r.build()
    assert built.source is None
    assert built.input_checksum == member.get_checksum()
    assert (built.path, built.input_celltype, built.celltype) == (
        _path_string(("x",)), "mixed", "mixed",
    )


def test_bound_retyped_deep_parent_still_miswires_its_one_step_consumer(make_context):
    """node-state-lifecycle.md, *`miswired` is a static defect*: "retyping a
    source that has projecting consumers is never refused, and each affected
    consumer becomes `miswired`." A deep step exempts its consumer from the
    wiring rule only while the parent is deep.
    """
    ctx = make_context()
    _deep(ctx)
    ctx.k = ctx.d["member"]
    ctx.compute(timeout=10)
    assert ctx.k.state == "complete"
    ctx.d.celltype = "plain"
    ctx.compute(timeout=10)
    assert ctx.k.state == "miswired"


# --- Refusals: the deep table still applies -----------------------------------


ILL_FORMED = {
    "integer item": lambda ctx: ctx.d[0],
    "slice": lambda ctx: ctx.d[0:1],
    "illegal deep conversion": lambda ctx: ctx.d.as_celltype("int"),
    "conversion into a deep celltype": lambda ctx: ctx.d["member"].as_celltype("deepcell"),
}


@pytest.mark.parametrize("target", ["new cell", "existing cell", "pin", "join member"])
@pytest.mark.parametrize("shape", sorted(ILL_FORMED))
def test_bound_ill_formed_deep_link_raises_at_assignment(make_context, shape, target):
    """node-state-lifecycle.md, *`miswired` is a static defect*: "**Writing an
    ill-formed link directly raises.**" cells.md, *Connecting*: "What is legal
    is the table in `contracts/deep-celltypes.md`: the zero-path conversions,
    and the exactly-one-string-item step whose result is `checksum` or the
    member's own celltype, and nothing else."
    """
    ctx = make_context()
    _deep(ctx)
    if target == "existing cell":
        ctx.r = Cell("mixed")
    elif target == "pin":
        ctx.tf = plus_one
    elif target == "join member":
        ctx.j = Cell("mixed")
    edges = list(ctx._graph.edges)
    nodes = set(ctx._graph.nodes)
    with pytest.raises(ValueError):
        handle = ILL_FORMED[shape](ctx)
        if target in ("new cell", "existing cell"):
            ctx.r = handle
        elif target == "pin":
            ctx.tf.pins.value = handle
        else:
            ctx.j["a"] = handle
    assert ctx._graph.edges == edges
    assert set(ctx._graph.nodes) == nodes
    # The refusal changed nothing, and the Context stays usable.
    ctx.compute(timeout=10)
    assert ctx.d.state == "complete"


@pytest.mark.parametrize("shape", sorted(ILL_FORMED))
def test_bound_handle_with_an_ill_formed_deep_link_reports_miswired(make_context, shape):
    """cells.md, *Reads*: "`x.state` is passive and local to the handle. [...]
    it reports `miswired` for an ill-formed link."
    """
    ctx = make_context()
    _deep(ctx)
    handle = ILL_FORMED[shape](ctx)
    assert handle.checksum is None
    assert handle.state == "miswired"
    assert handle.exception is None


def test_set_graph_derives_miswired_for_an_ill_formed_deep_link(make_context):
    """node-state-lifecycle.md, *`miswired` is a static defect*:
    "`Context.set_graph` **derives** the state rather than rejecting the graph".
    """
    ctx = make_context()
    _deep(ctx)
    graph = ctx.get_graph()
    graph["nodes"].append({"type": "cell", "path": ["r"], "celltype": "mixed", "value": None})
    graph["connections"].append({"type": "connection", "source": ["d", 0], "target": ["r"]})
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.r.state == "miswired"


@pytest.mark.parametrize("fed", ["single link", "chain"])
def test_set_graph_treats_a_two_step_deep_entry_as_ill_formed(make_context, fed):
    """cells.md, *Anonymous cells, symbols and elision*: "**The wiring invariant
    holds on table entries too**, and `set_graph` checks them exactly as it
    checks edges". deep-celltypes.md, *Paths*: "A deep Expression admits exactly
    one path step".

    Fed through a chain, the entry's anonymous cell is ill-formed, so its
    consumer is blocked by miswiring (*The handle and the node*). Fed by an
    identity edge from that single link, the edge is looked through: "A dummy
    edge from a single link is looked through" (*The input*), the consumer
    holds the ill-formed link itself, and is `miswired`. Either way the graph
    saves and reloads to the same state: the link is not collapsed into an
    own edge, which would read back as a legal deep step plus a path.
    """
    ctx = make_context()
    _deep(ctx)
    graph = ctx.get_graph()
    anonymous = {"aaaaa": {"source": {"node": ["d"]}, "celltype": "mixed", "path": "member.x"}}
    if fed == "single link":
        celltype, source = "mixed", "aaaaa"
    else:
        anonymous["bbbbb"] = {"source": {"symbol": "aaaaa"}, "celltype": "plain", "path": ""}
        celltype, source = "plain", "bbbbb"
    graph["nodes"].append({"type": "cell", "path": ["r"], "celltype": celltype, "value": None})
    graph["anonymous_nodes"] = anonymous
    graph["connections"].append({"type": "connection", "source": {"symbol": source}, "target": ["r"]})
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    expected = (
        ("miswired", None) if fed == "single link" else ("blocked", "blocked-by-miswiring")
    )
    assert (restored.r.state, restored.r.block_reason) == expected
    assert restored.r.checksum is None
    saved = restored.get_graph()
    assert saved["anonymous_nodes"] == anonymous
    assert saved["connections"] == graph["connections"]
    again = make_context()
    again.set_graph(saved)
    again.compute(timeout=10)
    assert (again.r.state, again.r.block_reason) == expected


# --- Item 4: as_celltype on a deep parent, then the step ----------------------


CONVERT_THEN_STEP = [
    ("deepcell", "deepfolder", "bytes"),
    ("deepcell", "deepcell", "mixed"),
    ("deepfolder", "folder", "bytes"),
    ("folder", "deepfolder", "bytes"),
]


@pytest.mark.parametrize("celltype,converted,member_type", CONVERT_THEN_STEP)
def test_bound_as_celltype_then_one_step_carries_the_member_celltype(
    make_context, celltype, converted, member_type
):
    """deep-celltypes.md, *Handles and writes one step below a deep parent*: "One
    step below a deep parent, a projection or handle carries the member
    celltype — `mixed` below a `deepcell`, `bytes` below a `deepfolder` or
    `folder`". *Zero-path conversions*: `deepcell → deepfolder`, `folder →
    deepfolder` and `deepfolder → folder` are free and checksum-preserving.
    """
    ctx = make_context()
    member, _ = _deep(ctx, celltype)
    handle = ctx.d.as_celltype(converted)["member"]
    assert handle.celltype == member_type
    assert handle.checksum == member.get_checksum()
    assert handle.state == "complete"
    assert handle.exception is None
    assert handle.build().compute() == member.get_checksum()
    ctx.e = ctx.d.as_celltype(converted)["member"]
    ctx.compute(timeout=10)
    assert (ctx.e.state, ctx.e.celltype) == ("complete", member_type)
    assert ctx.e.checksum == member.get_checksum()


def test_bound_conversion_after_a_deep_step_is_a_separate_expression_over_the_child(make_context):
    """expressions.md, *Fusion*: "anything after it (a further path, or the
    child's own conversion) is a separate Expression over the child checksum."
    deep-celltypes.md, *Agent guidance*: "apply further conversions as separate
    Expressions, keyed at the child."
    """
    ctx = make_context()
    member, _ = _deep(ctx)
    handle = ctx.d["member"].as_celltype("plain")
    assert handle.celltype == "plain"
    assert handle.value == {"x": 5}
    built = handle.build()
    assert (built.path, built.input_celltype, built.celltype) == ("", "mixed", "plain")
    assert built.compute() == Buffer({"x": 5}, "plain").get_checksum()
    assert built.input_checksum == member.get_checksum()
    ctx.p = handle
    ctx.compute(timeout=10)
    assert (ctx.p.state, ctx.p.celltype, ctx.p.value) == ("complete", "plain", {"x": 5})
    ctx.q = ctx.d["member"].as_celltype("plain")["x"]
    ctx.compute(timeout=10)
    assert (ctx.q.state, ctx.q.value) == ("complete", 5)


def test_bound_reference_form_one_step_below_a_deep_parent(make_context):
    """deep-celltypes.md, *Handles and writes one step below a deep parent*:
    "`d["k"].as_celltype("checksum")` gives the reference form."
    """
    ctx = make_context()
    member, _ = _deep(ctx)
    handle = ctx.d["member"].as_celltype("checksum")
    assert handle.celltype == "checksum"
    assert Checksum(handle.value) == member.get_checksum()
    ctx.c = handle
    ctx.compute(timeout=10)
    assert ctx.c.state == "complete"
    assert Checksum(ctx.c.value) == member.get_checksum()


# --- Transformers ----------------------------------------------------------------


@pytest.mark.parametrize("named", [False, True])
def test_bound_pin_fed_past_a_deep_step(make_context, named):
    """expressions.md, *Fusion*: a deep step is a barrier, and what follows it is
    a separate Expression over the child checksum -- for a pin as for a cell.
    """
    ctx = make_context()
    _deep(ctx)
    ctx.tf = plus_one
    if named:
        ctx.m = ctx.d["member"]
        ctx.tf.pins.value = ctx.m["x"]
    else:
        ctx.tf.pins.value = ctx.d["member"]["x"]
    ctx.compute(timeout=30)
    assert ctx.tf.state == "complete"
    assert ctx.tf.result.value == 6


def test_bound_projection_of_a_deep_transformer_result_carries_the_member_celltype(make_context):
    """deep-celltypes.md, *The output side*: "a deep result is an index, exactly
    like a deep input." *Handles and writes one step below a deep parent*: "One
    step below a deep parent, a projection or handle carries the member
    celltype".
    """
    ctx = make_context()
    ctx.tf = make_deep
    ctx.tf.celltypes.result = "deepcell"
    ctx.compute(timeout=30)
    member = Buffer({"x": 5}, "mixed").get_checksum()
    handle = ctx.tf.result["member"]
    assert handle.celltype == "mixed"
    assert handle.checksum == member
    assert handle.value == {"x": 5}
    ctx.r = ctx.tf.result["member"]
    ctx.compute(timeout=30)
    assert (ctx.r.state, ctx.r.celltype, ctx.r.checksum) == ("complete", "mixed", member)


# --- The round trip ---------------------------------------------------------------


def _one_step(ctx):
    ctx.r = ctx.d["member"]


def _anonymous_chain(ctx):
    ctx.r = ctx.d["member"]["x"]


def _named_chain(ctx):
    ctx.m = ctx.d["member"]
    ctx.r = ctx.m["x"]


def _convert_then_step(ctx):
    ctx.r = ctx.d.as_celltype("deepfolder")["member"]


def _step_then_convert(ctx):
    ctx.r = ctx.d["member"].as_celltype("plain")


def _step_convert_path(ctx):
    ctx.r = ctx.d["member"].as_celltype("plain")["x"]


def _reference_form(ctx):
    ctx.r = ctx.d["member"].as_celltype("checksum")


ROUND_TRIP = {
    "one step": _one_step,
    "anonymous chain": _anonymous_chain,
    "named chain": _named_chain,
    "as_celltype then step": _convert_then_step,
    "step then as_celltype": _step_then_convert,
    "step, as_celltype, path": _step_convert_path,
    "reference form": _reference_form,
}


@pytest.mark.parametrize("case", sorted(ROUND_TRIP))
def test_deep_step_wiring_survives_a_graph_round_trip(make_context, case):
    """cells.md, *Anonymous cells, symbols and elision*: "**Which entries are
    serialized.** `get_graph()` includes every entry that an edge, or another
    serialized entry, names." and "A chain's dummy edge is saved as it is." A
    reloaded graph rebuilds the same nodes, the same recipes and the same
    non-elided anonymous cells.
    """
    ctx = make_context()
    _deep(ctx)
    ROUND_TRIP[case](ctx)
    ctx.compute(timeout=10)
    r = ctx.r
    assert r.state == "complete", r.exception
    graph = ctx.get_graph()

    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    s = restored.r
    assert (s.state, s.celltype, s.checksum) == (r.state, r.celltype, r.checksum)
    assert s.value == r.value
    assert restored.get_graph() == graph
    assert s.build() == r.build()
    assert _anonymous_roles(restored) == _anonymous_roles(ctx)


@pytest.mark.parametrize("named", [False, True])
def test_pin_fed_past_a_deep_step_survives_a_graph_round_trip(make_context, named):
    """cells.md, *Anonymous cells, symbols and elision*: "A chain's dummy edge is
    saved as it is." -- for a pin target as for a cell.
    """
    ctx = make_context()
    _deep(ctx)
    ctx.tf = plus_one
    if named:
        ctx.m = ctx.d["member"]
        ctx.tf.pins.value = ctx.m["x"]
    else:
        ctx.tf.pins.value = ctx.d["member"]["x"]
    ctx.compute(timeout=30)
    graph = ctx.get_graph()
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=30)
    assert restored.tf.state == "complete"
    assert restored.tf.result.value == 6
    assert restored.get_graph() == graph
