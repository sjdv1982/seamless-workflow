"""Regressions for building and deriving bound Expression chains."""

from seamless import Buffer, Cell, Expression
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.checksum import expression as expression_mod
from seamless.checksum.cached_calculate_checksum import checksum_cache
from seamless.checksum.expression import get_expression_cache
from seamless.transformer import delayed
import numpy as np
import uuid


def _suffix(word):
    return word + "!"


def test_handle_build_is_a_pure_snapshot(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("text")
    ctx.source.set('{"x": [1, 2]}')
    handle = ctx.source.as_celltype("plain")["x"][0]
    before = dict(get_expression_cache())

    built = handle.build()

    assert dict(get_expression_cache()) == before
    assert isinstance(built, Expression)
    assert built.compute() is not None


def test_text_to_mixed_derives_string(make_context):
    ctx = make_context(expression_execution="local")
    ctx.text = Cell("text")
    ctx.text.set("[1, 2]")
    ctx.mixed = Cell("mixed")
    ctx.mixed = ctx.text
    ctx.mixed.compute(timeout=10)

    assert ctx.mixed.value == "[1, 2]"


def test_mixed_to_plain_reinterpretation_failure_is_node_state(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("mixed")
    ctx.source.set({"array": np.arange(3)})
    ctx.target = Cell("plain")

    ctx.target = ctx.source
    ctx.target.compute(timeout=10)

    assert ctx.target.state == "failed"
    assert ctx.target.checksum is None
    assert ctx.target.exception is not None


def test_missing_scratch_named_input_is_recomputed_locally(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("text")
    ctx.source.set("h" + uuid.uuid4().hex)
    ctx.middle = Cell("mixed")
    ctx.middle.scratch = True
    ctx.middle = ctx.source
    middle = ctx.middle.compute(timeout=10)
    assert middle is not None
    cache = get_buffer_cache()
    with cache.lock:
        cache.weak_cache.pop(middle, None)
        cache.strong_cache.pop(middle, None)
    checksum_cache.pop(middle, None)
    expression_mod._expression_result_buffers.pop(middle, None)

    ctx.result = ctx.middle[0]
    ctx.result.compute(timeout=10)

    assert ctx.result.value == "h"


def test_missing_scratch_transformer_input_is_recomputed_locally(make_context):
    ctx = make_context(expression_execution="local")
    ctx.tf = delayed(_suffix)
    ctx.tf.scratch = True
    ctx.tf.pins.word = "a" + uuid.uuid4().hex
    result = ctx.tf.compute(timeout=10)
    assert result is not None
    cache = get_buffer_cache()
    with cache.lock:
        cache.weak_cache.pop(result, None)
        cache.strong_cache.pop(result, None)
    checksum_cache.pop(result, None)
    expression_mod._expression_result_buffers.pop(result, None)

    ctx.letter = ctx.tf.result[0]
    ctx.letter.compute(timeout=10)

    assert ctx.letter.value == "a"


def test_source_retains_outer_conversion(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("mixed")
    ctx.source.set({"x": 1})
    ctx.target = Cell("mixed")
    ctx.target = ctx.source.as_celltype("plain")

    assert ctx.target.source.celltype == ctx.target.input_celltype == "plain"


def test_retype_after_explicit_projection_conversion(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("mixed")
    ctx.source.set({"x": {"y": 1}})
    ctx.target = Cell("plain")
    ctx.target = ctx.source["x"].as_celltype("plain")

    ctx.target.celltype = "mixed"
    assert ctx.target.celltype == "mixed"


def test_bound_subpath_checksum_converts_before_write(make_context):
    ctx = make_context(expression_execution="local")
    ctx.data = Cell("plain")
    ctx.data.set({"b": 0})
    source = Buffer("7", "text").get_checksum()

    ctx.data["b"].set_checksum(source, input_celltype="text")

    assert ctx.data.value["b"] == 7


def test_same_position_conversions_keep_syntax_order(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("plain")
    ctx.target = Cell("str")
    ctx.target = ctx.source.as_celltype("text").as_celltype("str")

    edge = ctx._graph.edges[-1]
    assert [celltype for _, celltype, _ in edge.source_chain] == ["text", "str"]


def test_join_reports_its_own_miswired_slot(make_context):
    ctx = make_context(expression_execution="local")
    ctx.source = Cell("plain")
    ctx.source.set({"x": 1})
    ctx.join = Cell("plain")
    ctx.join["k"] = ctx.source["x"]

    ctx.source.celltype = "text"

    assert ctx.join.state == "miswired"
    assert ctx.join.block_reason == {"k": "miswired"}


def test_bound_standalone_chain_survives_graph_round_trip(make_context):
    standalone = Cell("text")
    standalone.set('{"a": 1}')
    ctx = make_context(expression_execution="local")
    ctx.result = standalone.as_celltype("plain")["a"]
    ctx.compute(timeout=10)
    assert ctx.result.value == 1

    graph = ctx.get_graph()
    assert graph["connections"]
    assert graph["anonymous_nodes"]

    restored = make_context(expression_execution="local")
    restored.set_graph(graph)
    restored.compute(timeout=10)
    assert restored.result.value == 1
