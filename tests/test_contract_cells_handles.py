"""Bound projection / as_celltype handles: the anonymous-handle model of cells.md.

Written against the 2026-09-25 revision of contracts/cells.md (rulings rounds
1-7, register/cells-RULINGS.md), on points where both candidate revisions
agree.
"""
import pytest

from seamless import Buffer, Cell
from seamless.cell_errors import AuthorityError
from seamless import CacheMissError


def _plain(ctx, name="a", value=None):
    setattr(ctx, name, Cell("plain"))
    getattr(ctx, name).set({"x": 1, "y": [1, 2]} if value is None else value)
    ctx.compute(timeout=10)
    return getattr(ctx, name)


def _graph_edges_from(graph, ref):
    return [edge for edge in graph["connections"] if edge["source"] == ref]


# --- Reads through a handle ------------------------------------------------------

def test_projection_handle_read_evaluates_over_parent_checksum(make_context):
    """§Reads, Anonymous and projection handles: reads build and run the handle's Expression."""
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"]
    assert handle.checksum == Buffer(1, "plain").get_checksum()
    assert handle.value == 1
    assert handle.compute() == Buffer(1, "plain").get_checksum()


def test_handles_are_fresh_objects(make_context):
    """§Connecting, The handle and the node: `a = ctx.a; a[3] is a[3]` is False."""
    ctx = make_context()
    _plain(ctx)
    a = ctx.a
    assert a["x"] is not a["x"]
    assert a.x is not a.x


def test_fresh_handle_state_is_passive_and_local(make_context):
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"]
    assert ctx.a.state == "complete"
    assert handle.state == "waiting"
    assert handle.exception is None
    assert handle.checksum is not None
    assert handle.state == "complete"
    # Another fresh handle starts clean: the state is local to the handle.
    assert ctx.a["x"].state == "waiting"


@pytest.mark.parametrize("operation", [
    "checksum",
    "compute",
    "compute-timeout",
    "run",
])
def test_handle_without_parent_checksum_returns_none_without_raising(make_context, operation):
    ctx = make_context()
    ctx.u = Cell("plain")
    handle = ctx.u["x"]
    if operation == "checksum":
        result = handle.checksum
    elif operation == "compute":
        result = handle.compute()
    elif operation == "compute-timeout":
        result = handle.compute(timeout=1)
    else:
        result = handle.run()
    assert result is None
    assert handle.exception is None
    # Over an *unwired* parent, whether the handle reports `unwired` or `waiting` is deferred
    # (cells.md *Open questions*, "Handle state over an unwired parent"); pin neither.
    assert handle.state in ("waiting", "unwired")


def test_handle_compute_never_waits_on_a_progressing_parent(make_context, monkeypatch):
    import asyncio
    import time
    from threading import Event
    from seamless.checksum import expression as expression_module

    ctx = make_context()
    _plain(ctx, name="src", value={"a": {"x": 1}})
    entered, release = Event(), Event()
    original = expression_module._evaluate_expression_async

    async def gated(*args, **kwargs):
        entered.set()
        await asyncio.to_thread(release.wait, 15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(expression_module, "_evaluate_expression_async", gated)
    try:
        ctx.p = ctx.src["a"]
        assert entered.wait(5)
        assert ctx.p.state == "waiting"
        handle = ctx.p["x"]
        start = time.monotonic()
        assert handle.compute(timeout=5) is None
        assert time.monotonic() - start < 2
        assert handle.checksum is None
        assert handle.exception is None
    finally:
        release.set()


def test_handle_failure_lives_on_that_handle_only(make_context):
    ctx = make_context()
    _plain(ctx)
    failing = ctx.a["nope"]
    assert failing.checksum is None
    assert failing.state == "failed"
    message = failing.exception
    assert isinstance(message, str) and message
    with pytest.raises(Exception):
        failing.run()
    fresh = ctx.a["nope"]
    assert fresh.exception is None
    assert fresh.state == "waiting"
    assert ctx.a.state == "complete"
    assert ctx.a.exception is None


def test_handle_memo_follows_parent_checksum_after_pathed_write(make_context):
    """§Writes through a handle: the next read after a write through the handle re-evaluates."""
    ctx = make_context()
    _plain(ctx)
    p = ctx.a.x
    assert p.value == 1
    p.set(10)
    assert p.value == 10
    p.value = 11
    assert p.value == 11
    p += 1
    assert ctx.a.value == {"x": 12, "y": [1, 2]}
    assert p.value == 12


# --- Writes through a handle -------------------------------------------------------

@pytest.mark.parametrize("method", [False, True])
def test_projection_handle_value_writes_are_pathed_parent_writes(make_context, method):
    """§Writes through a handle: retained and non-retained handles are one operation."""
    ctx = make_context()
    _plain(ctx)
    retained = ctx.a.y
    if method:
        retained.set([3])
        ctx.a.x.set(5)
    else:
        retained.value = [3]
        ctx.a.x.value = 5
    assert ctx.a.value == {"x": 5, "y": [3]}
    assert ctx.a.source is None
    assert ctx.get_graph()["connections"] == []


def test_set_checksum_with_input_celltype_converts_before_insertion(make_context):
    ctx = make_context()
    _plain(ctx)
    five = Buffer("5", "str")
    hold = five.tempref()
    try:
        ctx.a.x.set_checksum(five.get_checksum(), input_celltype="str")
        ctx.compute(timeout=10)
        assert ctx.a.value == {"x": "5", "y": [1, 2]}
    finally:
        hold.clear()


@pytest.mark.parametrize("form", ["checksum", "set_checksum"])
def test_unresolvable_checksum_write_raises_cache_miss_and_records_nothing(make_context, form):
    ctx = make_context()
    _plain(ctx)
    missing = Buffer({"never": "stored"}, "plain").get_checksum()
    with pytest.raises(CacheMissError):
        if form == "checksum":
            ctx.a.x.checksum = missing
        else:
            ctx.a.x.set_checksum(missing)
    assert ctx.a.value == {"x": 1, "y": [1, 2]}
    assert ctx.a.exception is None
    assert ctx.a.state == "complete"


@pytest.mark.parametrize("form", ["checksum", "buffer", "set_checksum"])
def test_clearing_a_sub_path_is_refused(make_context, form):
    ctx = make_context()
    _plain(ctx)
    with pytest.raises(ValueError) as info:
        if form == "set_checksum":
            ctx.a.x.set_checksum(None)
        else:
            setattr(ctx.a.x, form, None)
    message = str(info.value)
    assert "None" in message and "del" in message
    assert ctx.a.value == {"x": 1, "y": [1, 2]}


def test_storing_null_in_a_key_is_a_pathed_write(make_context):
    """§Null and None: `ctx.a.b = None` / `.set(None)` / `.value = None` store null in the key."""
    ctx = make_context()
    _plain(ctx)
    ctx.a.x = None
    assert ctx.a.value == {"x": None, "y": [1, 2]}
    ctx.a.y.set(None)
    assert ctx.a.value == {"x": None, "y": None}
    ctx.a.x.value = None
    assert ctx.a.state == "complete"


_AS_CELLTYPE_WRITES = {
    "set": lambda x: x.set(3),
    "value": lambda x: setattr(x, "value", 3),
    "set_buffer": lambda x: x.set_buffer(Buffer(3, "mixed")),
    "buffer": lambda x: setattr(x, "buffer", Buffer(3, "mixed")),
    "set_checksum": lambda x: x.set_checksum(Buffer(3, "mixed").get_checksum()),
    "checksum": lambda x: setattr(x, "checksum", Buffer(3, "mixed").get_checksum()),
}


def _iadd(x):
    x += 1


@pytest.mark.parametrize("form", [
    "set", "set_buffer", "set_checksum",
    "value", "buffer", "checksum",
    "iadd",
])
def test_writes_through_an_as_celltype_handle_raise_authority_error(make_context, form):
    ctx = make_context()
    _plain(ctx, value={"k": 1})
    x = ctx.a.as_celltype("mixed")
    with pytest.raises(AuthorityError):
        if form == "iadd":
            _iadd(x)
        else:
            _AS_CELLTYPE_WRITES[form](x)
    assert ctx.a.value == {"k": 1}


# --- Binding and assignment of handles -------------------------------------------------

@pytest.mark.parametrize("kind", ["projection", "as_celltype"])
def test_anonymous_handle_cannot_be_a_standalone_source(make_context, kind):
    """Clarity ruling (2026-09-26): capture of an anonymous/projection handle raises DependencyError."""
    from seamless_workflow.errors import DependencyError
    ctx = make_context()
    _plain(ctx)
    handle = ctx.a["x"] if kind == "projection" else ctx.a.as_celltype("mixed")
    with pytest.raises(DependencyError):
        Cell(source=handle)


def test_named_bound_node_remains_a_valid_standalone_source(make_context):
    """§Binding: a named bound source remains valid for Cell(source=...)."""
    ctx = make_context()
    _plain(ctx)
    assert Cell(source=ctx.a).value == {"x": 1, "y": [1, 2]}


@pytest.mark.parametrize("kind", ["projection", "as_celltype"])
def test_anonymous_handle_cannot_be_assigned_into_another_context(make_context, kind):
    """Clarity ruling (2026-09-26): cross-Context assignment of a handle raises DependencyError, as for named nodes."""
    from seamless_workflow.errors import DependencyError
    ctx = make_context()
    _plain(ctx)
    other = make_context()
    handle = ctx.a["x"] if kind == "projection" else ctx.a.as_celltype("mixed")
    with pytest.raises(DependencyError):
        other.z = handle
    # Assigning it into its own Context instead is the valid spelling.
    ctx.own = handle
    ctx.compute(timeout=10)
    assert ctx.own.value == (1 if kind == "projection" else {"x": 1, "y": [1, 2]})


def test_assigning_to_a_new_name_takes_the_anonymous_node_over(make_context):
    from seamless_workflow.errors import StaleWorkflowHandleError
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    x = ctx.b.as_celltype("plain")
    y = ctx.b.as_celltype("plain")
    ctx.a = x
    ctx.compute(timeout=10)
    assert ctx.a.celltype == "plain"
    assert ctx.a.value == [10, 20, 30, 40]
    assert x.path == ""
    assert x.value == [10, 20, 30, 40]
    graph = ctx.get_graph()
    assert graph["anonymous_nodes"] == {}
    assert sorted(node["path"] for node in graph["nodes"]) == [["a"], ["b"]]
    with pytest.raises(StaleWorkflowHandleError):
        _ = y.value
    # x is now a handle to a: a later assignment of x is `ctx.d = ctx.a`.
    ctx.d = x
    edges = ctx.get_graph()["connections"]
    assert any(edge["target"] == ["d"] and edge["source"] == {"node": ["a"]} for edge in edges)


def test_assigning_to_an_existing_name_adds_an_edge_from_the_symbol(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.existing = Cell("mixed")
    ctx.compute(timeout=10)
    x = ctx.b.as_celltype("plain")
    ctx.existing = x
    ctx.d = x
    ctx.compute(timeout=10)
    assert ctx.existing.celltype == "mixed"
    assert ctx.existing.value == ctx.d.value == [10, 20, 30, 40]
    graph = ctx.get_graph()
    symbols = list(graph["anonymous_nodes"])
    assert len(symbols) == 1
    targets = sorted(edge["target"] for edge in _graph_edges_from(graph, {"symbol": symbols[0]}))
    assert targets == [["d"], ["existing"]]
    # x is still an anonymous handle, not a handle to `existing` or `d`.
    assert x.celltype == "plain"


def test_as_celltype_handle_creates_anonymous_node_immediately(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[1]")
    held = ctx.b.as_celltype("plain")
    graph = ctx.get_graph()
    assert graph["__seamless_workflow__"] == "0.5"
    assert len(graph["anonymous_nodes"]) == 1
    entry, = graph["anonymous_nodes"].values()
    assert entry == {"source": {"node": ["b"]}, "celltype": "plain", "path": ""}
    assert held.celltype == "plain"


def test_projection_handle_creates_anonymous_node_immediately(make_context):
    ctx = make_context()
    ctx.b = Cell("plain")
    ctx.b.set({"x": 1})
    held = ctx.b["x"]
    graph = ctx.get_graph()
    assert len(graph["anonymous_nodes"]) == 1
    entry, = graph["anonymous_nodes"].values()
    assert entry == {"source": {"node": ["b"]}, "celltype": "plain", "path": "x"}
    assert held.value == 1


def test_projected_as_celltype_handle_creates_anonymous_node_immediately(make_context):
    ctx = make_context()
    ctx.b = Cell("mixed")
    ctx.b.set({"a": [1, 2]})
    held = ctx.b["a"].as_celltype("plain")
    graph = ctx.get_graph()
    assert len(graph["anonymous_nodes"]) == 2
    path_symbol, path_entry = next(
        (symbol, entry)
        for symbol, entry in graph["anonymous_nodes"].items()
        if entry["path"] == "a"
    )
    conversion_entry, = (
        entry for entry in graph["anonymous_nodes"].values() if entry["path"] == ""
    )
    assert path_entry == {
        "source": {"node": ["b"]},
        "celltype": "mixed",
        "path": "a",
    }
    assert conversion_entry == {
        "source": {"symbol": path_symbol},
        "celltype": "plain",
        "path": "",
    }
    assert held.value == [1, 2]

    restored = make_context()
    restored.set_graph(graph)
    restored_held = restored.b["a"].as_celltype("plain")
    assert restored_held.value == [1, 2]
    assert restored.get_graph()["anonymous_nodes"] == graph["anonymous_nodes"]


@pytest.mark.parametrize("target_exists", [False, True])
def test_projected_as_celltype_assignment_roundtrips_through_outermost_symbol(
    make_context, target_exists
):
    ctx = make_context()
    ctx.b = Cell("mixed")
    ctx.b.set({"a": [1, 2]})
    if target_exists:
        ctx.target = Cell("plain")
    held = ctx.b["a"].as_celltype("plain")
    target_name = "target" if target_exists else "new_target"
    setattr(ctx, target_name, held)
    ctx.compute(timeout=10)
    assert getattr(ctx, target_name).value == [1, 2]

    graph = ctx.get_graph()
    edge = next(
        edge for edge in graph["connections"] if edge["target"] == [target_name]
    )
    assert set(edge["source"]) == {"symbol"}
    outer_symbol = edge["source"]["symbol"]
    outer_entry = graph["anonymous_nodes"][outer_symbol]
    assert outer_entry["path"] == ""
    assert outer_entry["celltype"] == "plain"
    assert set(outer_entry["source"]) == {"symbol"}

    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert getattr(restored, target_name).value == [1, 2]


def test_same_recipe_shares_one_symbol_and_entries_use_tagged_refs(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.p = ctx.b.as_celltype("plain")[0]
    ctx.q = ctx.b.as_celltype("plain")[1]
    ctx.compute(timeout=10)
    assert (ctx.p.value, ctx.q.value) == (10, 20)
    graph = ctx.get_graph()
    entries = graph["anonymous_nodes"]
    assert len(entries) == 3
    (conversion_symbol,), = [
        (symbol,)
        for symbol, entry in entries.items()
        if entry == {"source": {"node": ["b"]}, "celltype": "plain", "path": ""}
    ]
    path_entries = {
        entry["path"]: (symbol, entry)
        for symbol, entry in entries.items()
        if entry["path"] in {"[0]", "[1]"}
    }
    assert set(path_entries) == {"[0]", "[1]"}
    for symbol, entry in path_entries.values():
        assert entry == {
            "source": {"symbol": conversion_symbol},
            "celltype": "plain",
            "path": entry["path"],
        }
        assert [edge["target"] for edge in _graph_edges_from(graph, {"symbol": symbol})] == [
            ["p"] if entry["path"] == "[0]" else ["q"]
        ]


def test_symbol_is_stable_across_value_changes_of_its_source(make_context):
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.result = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    before = set(ctx.get_graph()["anonymous_nodes"])
    ctx.b.set("[50, 60, 70, 80]")
    ctx.compute(timeout=10)
    assert ctx.result.value == 80
    assert set(ctx.get_graph()["anonymous_nodes"]) == before


@pytest.mark.parametrize("kind", ["item", "attribute", "slice"])
def test_projection_handle_starts_non_scratch(make_context, kind):
    """§Scratch policy: a projection is a new Cell that owns its own result, so it starts non-scratch."""
    ctx = make_context()
    _plain(ctx)
    ctx.a.scratch = True
    handle = {"item": lambda: ctx.a["y"], "attribute": lambda: ctx.a.y, "slice": lambda: ctx.a.y[0:1]}[kind]()
    assert ctx.a.scratch is True
    assert handle.scratch is False


# --- Round 8 rulings (register/cells-RULINGS.md) ----------------------------------------

def test_handle_is_miswired_when_its_parent_is_retyped(make_context):
    """Round 8, ruling 1: an anonymous entry's celltype is fixed at creation, so
    retyping the parent makes a pathed handle miswired rather than silently re-reading."""
    ctx = make_context()
    ctx.b = Cell("text")
    ctx.b.set("[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    handle = ctx.b[3]
    assert handle.celltype == "text"
    assert handle.value == ","
    ctx.b.celltype = "plain"
    ctx.compute(timeout=10)
    assert handle.celltype == "text"
    assert handle.state == "miswired"
    assert handle.checksum is None
    ctx.b.celltype = "text"
    ctx.compute(timeout=10)
    assert handle.value == ","


@pytest.mark.parametrize("source_celltype, elided", [("mixed", True), ("text", False)])
def test_conversion_then_path_is_elided_only_when_checksum_preserving(make_context, monkeypatch, source_celltype, elided):
    """Round 8, ruling 2: `ctx.a = ctx.b.as_celltype("plain")[3]` elides the anonymous
    conversion only when b -> plain preserves the checksum (mixed); from text it is a
    fusion barrier, and the Context builds and evaluates the intermediate."""
    from seamless import Expression
    ctx = make_context()
    ctx.b = Cell(source_celltype)
    ctx.b.set([10, 20, 30, 40] if source_celltype == "mixed" else "[10, 20, 30, 40]")
    ctx.compute(timeout=10)
    built = []
    initialize = Expression.__post_init__

    def record(expression):
        initialize(expression)
        built.append((expression.input_celltype, expression.celltype, expression.path))

    monkeypatch.setattr(Expression, "__post_init__", record)
    ctx.a = ctx.b.as_celltype("plain")[3]
    ctx.compute(timeout=10)
    assert ctx.a.value == 40
    intermediate = (source_celltype, "plain", "")
    assert (intermediate in built) is (not elided)
    # Named and anonymous intermediates build the same recipe either way.
    ctx.mid = Cell("plain")
    ctx.mid = ctx.b
    ctx.named = ctx.mid[3]
    ctx.compute(timeout=10)
    assert ctx.named.value == 40
    assert ctx.named.build().identity_key == ctx.a.build().identity_key
