import pytest
from seamless_transformer import Transformer
from seamless_transformer.compiled_validation import CompiledPinCelltypeWarning
from seamless_workflow import Context

from seamless import Cell

SCHEMA = """inputs:
  - {name: x, dtype: int32}
outputs:
  - {name: result, dtype: int32}
"""


def builder():
    tf = Transformer("c", compiled=True)
    tf.schema = SCHEMA
    tf.celltypes.x = "int"
    tf.code = "#include <stdint.h>\nint transform(int32_t x, int32_t *result) {*result=x;return 0;}"
    return tf


def test_conservative_pin_liberal_workflow_and_roundtrip():
    with Context() as ctx, Context() as clone:
        ctx.tf = builder()
        ctx.source = Cell("mixed")
        ctx.source.set(7)
        ctx.tf.pins.x = ctx.source
        ctx.compute()
        assert ctx.tf.run() == 7
        graph = ctx.get_graph()
        clone.set_graph(graph)
        clone.compute()
        assert clone.tf.celltypes.x == "int"
        assert clone.tf.schema_celltypes["x"] == "int"
        assert clone.tf.run() == 7
        with pytest.warns(CompiledPinCelltypeWarning):
            clone.tf.schema = SCHEMA.replace("int32", "float64")
        clone.compute()
        assert clone.tf.state == "blocked"
        assert "incompatible" in clone.tf.exception
        assert clone.tf.celltypes.x == "int"


def test_import_optional_rejected_and_bad_schema_blocked():
    with Context() as ctx, Context() as clone:
        ctx.tf = builder()
        graph = ctx.get_graph()
        entry = next(n for n in graph["nodes"] if n["type"] == "transformer")
        entry["optional_pins"] = ["x"]
        with pytest.raises(TypeError, match="optional"):
            clone.set_graph(graph)
        entry["optional_pins"] = []
        entry["schema"] = "broken: ["
        clone.set_graph(graph)
        clone.compute()
        assert clone.tf.state == "blocked"
        assert clone.tf.celltypes.x == "int"
        assert clone.tf.exception


def test_stage1_precedes_pin_resolution():
    tf = builder()
    tf.celltypes.x = "binary"
    tf.schema = SCHEMA.replace("dtype: int32}", "dtype: char}")
    with Context() as ctx:
        ctx.tf = tf
        with pytest.warns(CompiledPinCelltypeWarning):
            ctx.tf.celltypes.x = "mixed"
        ctx.compute()
        assert ctx.tf.state == "blocked"
        assert "explicit" in ctx.tf.exception
        assert not ctx.tf._workflow_backend._node().pin_states


def test_bound_metavars_stage1_and_schema_rebuild():
    tf = builder()
    tf.schema = SCHEMA.replace(
        "name: result, dtype: int32", "name: result, dtype: int32, shape: [K]"
    )
    with Context() as ctx:
        ctx.tf = tf
        ctx.compute()
        assert ctx.tf.state == "blocked"
        assert "metavars" in ctx.tf.exception
        ctx.tf.metavars.maxK = 4
        assert ctx.tf.metavars.maxK == 4
        ctx.compute()
        assert ctx.tf.state == "unwired"
        ctx.tf.schema = SCHEMA
        with pytest.raises(AttributeError):
            _ = ctx.tf.metavars.maxK


def test_bound_literal_null_reports_compiled_pin():
    from seamless_transformer import CompiledPinSchemaError

    with Context() as ctx:
        ctx.tf = builder()
        with pytest.raises(CompiledPinSchemaError, match="'x'.*null"):
            ctx.tf.pins.x = None


def test_schema_rename_discards_old_pin_and_bound_code_remains_compiled():
    with Context() as ctx:
        ctx.tf = builder()
        ctx.tf.pins.x = 5
        ctx.compute()
        assert ctx.tf.run() == 5
        ctx.tf.schema = SCHEMA.replace("name: x", "name: y")
        assert ctx.tf.celltypes.y == "mixed"
        with pytest.raises(AttributeError):
            ctx.tf.pins.x = 4
        ctx.tf.code = "#include <stdint.h>\nint transform(int32_t y,int32_t *result) {*result=y+1;return 0;}"
        ctx.tf.pins.y = 7
        ctx.compute()
        assert ctx.tf.run() == 8
        entry = next(n for n in ctx.get_graph()["nodes"] if n["type"] == "transformer")
        assert set(entry["pins"]) == {"y"}
        assert set(entry["producers"]) == {"y"}


def test_conservative_text_pin_accepts_dict_edge_and_typed_checksum():
    from seamless import Buffer

    tf = Transformer("c", compiled=True)
    with pytest.warns(CompiledPinCelltypeWarning):
        tf.schema = SCHEMA.replace(
            "name: x, dtype: int32", "name: x, dtype: char, shape: [N]"
        )
    tf.celltypes.x = "text"
    tf.code = "#include <stdint.h>\nint transform(unsigned int N,const unsigned char *x,int32_t *result) {*result=N;return 0;}"
    value = {"answer": 42}
    buffer = Buffer(value, "plain")
    expected = len(buffer.get_value("text").encode())
    with Context() as ctx:
        ctx.tf = tf
        ctx.source = Cell("plain")
        ctx.source.set(value)
        ctx.tf.pins.x = ctx.source
        ctx.compute()
        assert ctx.tf.run() == expected
        ctx.tf.pins.x.checksum = None
        ctx.tf.pins.x.set_checksum(buffer.get_checksum(), input_celltype="plain")
        ctx.compute()
        assert ctx.tf.run() == expected
