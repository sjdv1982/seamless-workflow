"""Contract tests: compiled pins in bound workflows and graph import.

Oracle: ``seamless/docs/agent/contracts/compiled-pins.md`` §4, §5, §9, §10.
Complements ``test_compiled_celltype_workflow.py`` (not repeated here).
"""

import copy
import shutil
import warnings

import pytest
from seamless_transformer import Transformer
from seamless_transformer.compiled_validation import CompiledPinCelltypeWarning
from seamless_workflow import Context

from seamless import Cell

pytestmark = pytest.mark.skipif(not shutil.which("gcc"), reason="gcc required")

SCHEMA = """inputs:
  - {name: x, dtype: int32}
outputs:
  - {name: result, dtype: int32}
"""
CODE = "#include <stdint.h>\nint transform(int32_t x, int32_t *result) {*result=x;return 0;}"


def builder(celltype="int"):
    tf = Transformer("c", compiled=True)
    tf.schema = SCHEMA
    tf.celltypes.x = celltype
    tf.code = CODE
    return tf


def exported_graph():
    with Context() as ctx:
        ctx.tf = builder()
        return ctx.get_graph()


def import_modified(modify):
    graph = copy.deepcopy(exported_graph())
    entry = next(n for n in graph["nodes"] if n["type"] == "transformer")
    modify(entry)
    return graph


@pytest.mark.parametrize("declared", ["float", "bytes"])
def test_import_incompatible_declaration_is_kept_and_blocks(declared):
    """§4/§5: incompatible declarations import as declared and block (Stage 1)."""

    def modify(entry):
        entry["pins"]["x"]["celltype"] = declared

    graph = import_modified(modify)
    with Context() as ctx:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        ctx.compute()
        assert ctx.tf.celltypes.x == declared
        assert ctx.tf.state == "blocked"
        assert isinstance(ctx.tf.exception, str)
        assert "'x'" in ctx.tf.exception and "incompatible" in ctx.tf.exception
        assert "incompatible" in repr(ctx.tf.schema_celltypes)
        assert ctx.tf.schema_celltypes["x"] == "int"


def test_import_celltype_no_schema_allows_is_kept_and_blocks():
    """§4: a celltype no schema allows can arrive by graph import; it blocks."""

    def modify(entry):
        entry["pins"]["x"]["celltype"] = "plain"

    graph = import_modified(modify)
    with Context() as ctx:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        ctx.compute()
        assert ctx.tf.celltypes.x == "plain"
        assert ctx.tf.state == "blocked"
        assert "'x'" in ctx.tf.exception


@pytest.mark.parametrize("variant", ["no-celltype", "no-pin-entry"])
def test_legacy_graph_without_declaration_imports_as_mixed(variant):
    """§10 D4: an undeclared compiled input imports as mixed (auto)."""

    def modify(entry):
        if variant == "no-celltype":
            entry["pins"]["x"].pop("celltype")
        else:
            entry["pins"].pop("x")

    # Keep the exporting context alive: it holds the code buffer the import needs.
    with Context() as source, Context() as ctx:
        source.tf = builder()
        graph = copy.deepcopy(source.get_graph())
        modify(next(n for n in graph["nodes"] if n["type"] == "transformer"))
        ctx.set_graph(graph)
        assert ctx.tf.celltypes.x == "mixed"
        ctx.tf.pins.x = 6
        ctx.compute()
        assert ctx.tf.run() == 6


def test_legacy_char_input_imports_blocked_as_missing_declaration():
    """§10 D4: a legacy 1-D char input imports blocked until declared."""

    def modify(entry):
        entry["pins"]["x"].pop("celltype")
        entry["schema"] = SCHEMA.replace("dtype: int32}", "dtype: char, shape: [N]}", 1)

    with Context() as source, Context() as ctx:
        source.tf = builder()
        graph = copy.deepcopy(source.get_graph())
        modify(next(n for n in graph["nodes"] if n["type"] == "transformer"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        ctx.compute()
        assert ctx.tf.celltypes.x == "mixed"
        assert ctx.tf.state == "blocked"
        assert "explicit" in ctx.tf.exception
        ctx.tf.celltypes.x = "bytes"
        ctx.tf.code = (
            "#include <stdint.h>\nint transform(unsigned int N,const unsigned char *x,"
            "int32_t *result) {*result=N;return 0;}"
        )
        ctx.tf.pins.x = b"abc"
        ctx.compute()
        assert ctx.tf.run() == 3


def test_executor_side_schema_error_names_pin_and_class():
    """§5/§9: a data-dependent failure names the pin; D5 (b) class name in text."""
    with Context() as ctx:
        ctx.tf = builder("mixed")
        ctx.src = Cell("mixed")
        ctx.src.set(2**40)
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert ctx.tf.state in ("blocked", "failed")
        exc = ctx.tf.exception
        assert isinstance(exc, str)
        assert "'x'" in exc
        assert "CompiledPinSchemaError" in exc or "range" in exc


def test_bound_mixed_container_names_pin():
    """§3a/§5: a JSON list on a bound mixed int pin is rejected, naming the pin."""
    with Context() as ctx:
        ctx.tf = builder("mixed")
        ctx.src = Cell("mixed")
        ctx.src.set([1, 2])
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert ctx.tf.state in ("blocked", "failed")
        assert "'x'" in ctx.tf.exception
        assert "binary" in ctx.tf.exception  # lists other allowed declarations


def test_conversion_failure_is_recorded_on_pin_and_blocks():
    """§9: Expression conversion errors stay conversion errors, name pin and
    celltypes, and block the transformer (blocked-by-error)."""
    with Context() as ctx:
        ctx.tf = builder("int")
        ctx.src = Cell("plain")
        ctx.src.set("abc")
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"x": "blocked-by-error"}
        exc = ctx.tf.exception
        assert "'x'" in exc and "plain" in exc and "int" in exc
        assert "CompiledPin" not in exc and "CompiledMixed" not in exc
