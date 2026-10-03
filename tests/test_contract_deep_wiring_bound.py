"""Bound wiring around a deep step: refusals at assignment, and their message.

contracts/deep-celltypes.md, *Bound wiring around the step*, and the wiring
rule's refusal text of contracts/cells.md, *Connecting*, for sources and
targets that are deep.
"""
import copy
import re

import pytest

from seamless import Buffer, Cell


def _held(value, celltype=None):
    buffer = Buffer(value, celltype) if celltype else Buffer(value)
    buffer.tempref()
    return buffer


def _deep(ctx, celltype="deepcell", name="d"):
    """A deep cell whose index holds one member, ``"member"``."""
    if celltype == "deepcell":
        member = _held({"x": 5}, "mixed")
    else:
        member = _held(b"member bytes", "bytes")
    index = _held({"member": member.get_checksum().hex()}, "plain")
    setattr(ctx, name, Cell(celltype, checksum=index.get_checksum()))
    ctx.compute(timeout=10)
    return member, index


def identity(value):
    return value


def make_mapping():
    return {"a": 1}


def _refusal_lines(info):
    message = str(info.value)
    assert "ctx.dmember" not in message
    return message.splitlines()


# --- The refusal message names the real node and path -----------------------


@pytest.mark.parametrize("target", ["cell", "pin"])
def test_refusal_of_the_bare_deep_handle_names_the_node_and_path(make_context, target):
    """cells.md, *Connecting*: the header is `would convert <in> -> <out> behind a
    projection.`, "followed by the two spellings, projection first:
    `…[k].as_celltype("<out>")` and then `….as_celltype("<out>")[k]`, where `k`
    is the step that was written". deep-celltypes.md, *Bound wiring around the
    step*: "The reference form must be spelled out.
    `ctx.x = ctx.d["k"].as_celltype("checksum")` gives it. Assigning the bare
    handle `ctx.d["k"]` into an existing `checksum` cell is refused with the
    wiring rule's `TypeError`". Converting the index itself to `checksum` is
    outside the deep table, so that reading is not offered.
    """
    ctx = make_context()
    _deep(ctx)
    if target == "cell":
        ctx.x = Cell("checksum")
        name = r"ctx\.x"
    else:
        ctx.tf = identity
        ctx.tf.celltypes.value = "checksum"
        name = r"ctx\.tf\.pins\.value"
    before = copy.deepcopy(ctx.get_graph())
    with pytest.raises(TypeError) as info:
        if target == "cell":
            ctx.x = ctx.d["member"]
        else:
            ctx.tf.pins.value = ctx.d["member"]
    lines = _refusal_lines(info)
    assert lines[0] == "would convert mixed -> checksum behind a projection."
    assert re.match(
        r'^\s*' + name + r' = ctx\.d\["member"\]\.as_celltype\("checksum"\)\s+# item "member" of ',
        lines[1],
    )
    assert len(lines) == 2, "ctx.d.as_celltype(\"checksum\") is an illegal deep conversion"
    assert ctx.get_graph() == before


def test_refusal_into_a_plain_join_offers_both_legal_spellings(make_context):
    """cells.md, *Connecting*: "It points at both readings, because the whole
    problem is that the spelling does not choose between them". Both readings
    are legal for a deep source converted to `plain` (deep-celltypes.md,
    *Zero-path conversions*: `deepcell → plain`), and each offered spelling
    wires without a refusal.
    """
    ctx = make_context()
    member, index = _deep(ctx)
    ctx.j = Cell("plain")
    ctx.j.set({})
    with pytest.raises(TypeError) as info:
        ctx.j["left"] = ctx.d["member"]
    lines = _refusal_lines(info)
    assert lines[0] == "would convert mixed -> plain behind a projection."
    assert len(lines) == 3
    assert re.match(
        r'^\s*ctx\.j\["left"\] = ctx\.d\["member"\]\.as_celltype\("plain"\)\s+# item "member" of ',
        lines[1],
    )
    assert re.match(
        r'^\s*ctx\.j\["left"\] = ctx\.d\.as_celltype\("plain"\)\["member"\]\s+# item "member" of ',
        lines[2],
    )
    ctx.j["left"] = ctx.d["member"].as_celltype("plain")
    ctx.j["right"] = ctx.d.as_celltype("plain")["member"]
    ctx.compute(timeout=10)
    assert ctx.j.value == {"left": {"x": 5}, "right": member.get_checksum().hex()}


def test_refusal_spells_string_keys_and_nested_paths_in_bracket_form(make_context):
    """cells.md, *Connecting*: the two spellings `…[k].as_celltype("<out>")`
    and `….as_celltype("<out>")[k]`, "where `k` is the step that was written",
    each carrying "a `# item k of …` gloss saying which reading it means".
    """
    ctx = make_context()
    ctx.p = Cell("plain")
    ctx.p.set({"a": [1, 2]})
    ctx.q = Cell("int")
    with pytest.raises(TypeError) as info:
        ctx.q = ctx.p["a"][1]
    lines = _refusal_lines(info)
    assert lines[0] == "would convert plain -> int behind a projection."
    assert len(lines) == 3
    assert re.match(r'^\s*ctx\.q = ctx\.p\["a"\]\[1\]\.as_celltype\("int"\)\s+# item \["a"\]\[1\] of ', lines[1])
    assert re.match(r'^\s*ctx\.q = ctx\.p\.as_celltype\("int"\)\["a"\]\[1\]\s+# item \["a"\]\[1\] of ', lines[2])
    # An item of a plain value is not a character.
    assert "(a character)" not in str(info.value)


def test_refusal_names_a_transformer_result_source(make_context):
    """cells.md, *Connecting*: the spellings name the source as it is written."""
    ctx = make_context()
    ctx.tf = make_mapping
    ctx.tf.celltypes.result = "plain"
    ctx.x = Cell("int")
    with pytest.raises(TypeError) as info:
        ctx.x = ctx.tf.result["a"]
    lines = _refusal_lines(info)
    assert lines[0] == "would convert plain -> int behind a projection."
    assert re.match(r'^\s*ctx\.x = ctx\.tf\.result\["a"\]\.as_celltype\("int"\)\s+# item "a" of ', lines[1])


# --- A pathless link's implicit conversion into a cell ------------------------


ILLEGAL_IMPLICIT = [
    ("deepcell", "int"),
    ("deepcell", "mixed"),
    ("folder", "plain"),
    ("deepfolder", "deepcell"),
    ("mixed", "deepcell"),
    ("plain", "folder"),
]

LEGAL_IMPLICIT = [
    ("deepcell", "plain"),
    ("deepcell", "deepfolder"),
    ("deepfolder", "folder"),
    ("folder", "deepfolder"),
    ("folder", "mixed"),
]


def _source(ctx, celltype):
    if celltype in ("deepcell", "deepfolder", "folder"):
        _deep(ctx, celltype, name="s")
    else:
        ctx.s = Cell(celltype)
        ctx.s.set({"a": 1})
        ctx.compute(timeout=10)
    return ctx.s


@pytest.mark.parametrize("source,target", ILLEGAL_IMPLICIT)
def test_implicit_illegal_deep_conversion_into_an_existing_cell_raises(make_context, source, target):
    """deep-celltypes.md, *Bound wiring around the step*: "**Writing an
    ill-formed deep link raises `ValueError`** [...] This covers [...] an
    illegal deep conversion, and the graph is left unchanged." Zero-path
    conversions: "everything else | **illegal**".
    """
    ctx = make_context()
    _source(ctx, source)
    ctx.x = Cell(target)
    before = copy.deepcopy(ctx.get_graph())
    with pytest.raises(ValueError):
        ctx.x = ctx.s
    assert ctx.get_graph() == before
    ctx.compute(timeout=10)
    assert ctx.x.source is None
    assert ctx.x.celltype == target
    assert ctx.s.state == "complete"


@pytest.mark.parametrize("source,target", LEGAL_IMPLICIT)
def test_implicit_legal_deep_conversion_into_an_existing_cell_is_accepted(make_context, source, target):
    """deep-celltypes.md, *Zero-path conversions*: these pairs are legal; every
    one but `folder → mixed` is free and checksum-preserving.
    """
    ctx = make_context()
    _source(ctx, source)
    ctx.x = Cell(target)
    ctx.x = ctx.s
    ctx.compute(timeout=10)
    assert ctx.x.state == "complete", ctx.x.exception
    if (source, target) != ("folder", "mixed"):
        assert ctx.x.checksum == ctx.s.checksum


def test_set_graph_derives_miswired_for_an_implicit_illegal_deep_conversion(make_context):
    """node-state-lifecycle.md, *`miswired` is a static defect*: "`Context.set_graph`
    **derives** the state rather than rejecting the graph". A node whose incoming
    link is statically ill-formed is `miswired`, and its dependents are blocked
    by miswiring.
    """
    ctx = make_context()
    _deep(ctx)
    ctx.x = Cell("int")
    ctx.y = ctx.x
    graph = ctx.get_graph()
    graph["connections"].append({"type": "connection", "source": {"node": ["d"]}, "target": ["x"]})
    restored = make_context()
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.x.state == "miswired"
    assert restored.x.exception is None
    assert restored.y.state == "blocked"
    assert restored.y.block_reason == "blocked-by-miswiring"
