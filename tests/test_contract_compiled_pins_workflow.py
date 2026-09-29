"""Contract tests: compiled pins in bound workflows and graph import.

Oracle: ``seamless/docs/agent/contracts/compiled-pins.md`` §4, §5, §9, §10;
§12 "Implementation status" gaps are covered here by focused contract tests.
Complements ``test_compiled_celltype_workflow.py`` (not repeated here).
"""

import copy
import shutil
import warnings

import pytest
from seamless_transformer import Transformer
from seamless_transformer.compiled_validation import CompiledPinCelltypeWarning
from seamless_workflow import Context

from seamless import Buffer, Cell

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
def test_import_incompatible_declaration_is_kept_and_reported(declared):
    """§4/§5: incompatible declarations import as declared and are reported
    as a Stage 1 failure (state pinned by test_stage1_failure_is_state_failed)."""

    def modify(entry):
        entry["pins"]["x"]["celltype"] = declared

    graph = import_modified(modify)
    with Context() as ctx:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        ctx.compute()
        assert ctx.tf.celltypes.x == declared
        assert isinstance(ctx.tf.exception, str)
        assert "'x'" in ctx.tf.exception and "incompatible" in ctx.tf.exception
        assert "incompatible" in repr(ctx.tf.schema_celltypes)
        assert ctx.tf.schema_celltypes["x"] == "int"


def test_import_celltype_no_schema_allows_is_kept_and_reported():
    """§4: a celltype no schema allows can arrive by graph import; it is kept
    and reported as a Stage 1 failure."""

    def modify(entry):
        entry["pins"]["x"]["celltype"] = "plain"

    graph = import_modified(modify)
    with Context() as ctx:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        ctx.compute()
        assert ctx.tf.celltypes.x == "plain"
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


def test_legacy_char_input_imports_as_missing_declaration():
    """§10 D4: a legacy 1-D char input imports as a Stage 1 failure (missing
    declaration) until bytes/text/binary is declared."""

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
        assert "explicit" in ctx.tf.exception
        ctx.tf.celltypes.x = "bytes"
        ctx.tf.code = (
            "#include <stdint.h>\nint transform(unsigned int N,const unsigned char *x,"
            "int32_t *result) {*result=N;return 0;}"
        )
        ctx.tf.pins.x = b"abc"
        ctx.compute()
        assert ctx.tf.run() == 3


def test_executor_side_schema_error_is_failed_and_names_pin_and_class():
    """Rulings 2026-09-26 (rule 3 vs compiled): every pin has a valid checksum
    but the compiled transformer cannot use it -> state failed, tf.exception
    set; §9 D5: it carries the class name and names the pin."""
    with Context() as ctx:
        ctx.tf = builder("mixed")
        ctx.src = Cell("mixed")
        ctx.src.set(2**40)
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert ctx.tf.state == "failed"
        assert ctx.tf.block_reason is None
        exc = ctx.tf.exception
        assert isinstance(exc, str)
        assert "'x'" in exc
        assert "CompiledPinSchemaError" in exc


def test_bound_mixed_container_is_failed_and_names_pin():
    """§3a: a JSON list is a valid mixed checksum the compiled transformer
    cannot use -> transformer failed, tf.exception set (names the pin and the
    other allowed declarations)."""
    with Context() as ctx:
        ctx.tf = builder("mixed")
        ctx.src = Cell("mixed")
        ctx.src.set([1, 2])
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert ctx.tf.state == "failed"
        assert ctx.tf.block_reason is None
        assert "'x'" in ctx.tf.exception
        assert "binary" in ctx.tf.exception  # lists other allowed declarations


def _conversion_failure(ctx):
    ctx.tf = builder("int")
    ctx.src = Cell("plain")
    ctx.src.set("abc")
    ctx.tf.pins.x = ctx.src
    ctx.compute()


def test_conversion_failure_is_recorded_on_pin_and_blocks():
    """§9 + pins.md: a conversion failure (no valid pin checksum) is recorded
    on the pin (failed, pin.exception names pin and celltypes) and blocks the
    transformer with blocked-by-error."""
    with Context() as ctx:
        _conversion_failure(ctx)
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"x": "blocked-by-error"}
        pin = ctx.tf.pins.x
        assert pin.state == "failed"
        exc = pin.exception
        assert isinstance(exc, str)
        assert "'x'" in exc and "plain" in exc and "int" in exc
        assert "CompiledPin" not in exc and "CompiledMixed" not in exc


def test_conversion_failure_leaves_transformer_exception_none():
    with Context() as ctx:
        _conversion_failure(ctx)
        assert ctx.tf.exception is None


# ------------------------------------------------------------------
# Ruling 6 (contract-clarity-rulings.md, 2026-09-26): a compiled Stage-1
# failure is the transformer's own failure -- state "failed".  Under
# ruling 4 / node-state-lifecycle.md, block_reason is None outside
# unwired/miswired/blocked/waiting.  §9 D5: .exception is a string that
# carries the class name and message.

def _stage1_schema_change(ctx):
    ctx.tf = builder("int")
    with pytest.warns(CompiledPinCelltypeWarning):
        ctx.tf.schema = SCHEMA.replace("int32}", "float64}", 1)
    return "incompatible"


def _stage1_missing_declaration(ctx):
    ctx.tf = builder("int")
    with pytest.warns(CompiledPinCelltypeWarning):
        ctx.tf.schema = SCHEMA.replace("dtype: int32}", "dtype: char, shape: [N]}", 1)
        ctx.tf.celltypes.x = "mixed"
    return "explicit"


def _stage1_metavars(ctx):
    ctx.tf = builder("int")
    ctx.tf.schema = SCHEMA.replace(
        "name: result, dtype: int32", "name: result, dtype: int32, shape: [K]"
    )
    return "metavars"


def _stage1_import(modify, hint):
    def scenario(ctx):
        # Stage 1 needs no code buffer, so the exporting context may close.
        with Context() as source:
            source.tf = builder("int")
            graph = copy.deepcopy(source.get_graph())
            modify(next(n for n in graph["nodes"] if n["type"] == "transformer"))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", CompiledPinCelltypeWarning)
                ctx.set_graph(graph)
        return hint

    return scenario


def _set_celltype(value):
    def modify(entry):
        entry["pins"]["x"]["celltype"] = value

    return modify


def _bad_schema(entry):
    entry["schema"] = "broken: ["


def _legacy_char(entry):
    entry["pins"]["x"].pop("celltype")
    entry["schema"] = SCHEMA.replace("dtype: int32}", "dtype: char, shape: [N]}", 1)


STAGE1_SCENARIOS = {
    "schema-change": _stage1_schema_change,
    "missing-declaration": _stage1_missing_declaration,
    "metavars": _stage1_metavars,
    "import-incompatible": _stage1_import(_set_celltype("float"), "incompatible"),
    "import-unknown-celltype": _stage1_import(_set_celltype("plain"), "plain"),
    "import-unparsable-schema": _stage1_import(_bad_schema, ""),
    "import-legacy-char": _stage1_import(_legacy_char, "explicit"),
}


@pytest.mark.parametrize("scenario", sorted(STAGE1_SCENARIOS))
def test_stage1_failure_is_state_failed(scenario):
    with Context() as ctx:
        hint = STAGE1_SCENARIOS[scenario](ctx)
        ctx.compute()
        assert isinstance(ctx.tf.exception, str) and hint in ctx.tf.exception
        assert ctx.tf.state == "failed"
        assert ctx.tf.block_reason is None


@pytest.mark.parametrize("scenario", sorted(STAGE1_SCENARIOS))
def test_stage1_failure_never_runs_and_reports(scenario):
    """Stage 1 reports its diagnostic and resolves no pin (the state label,
    'failed', is pinned by test_stage1_failure_is_state_failed)."""
    with Context() as ctx:
        hint = STAGE1_SCENARIOS[scenario](ctx)
        ctx.compute()
        assert isinstance(ctx.tf.exception, str) and hint in ctx.tf.exception
        assert not ctx.tf._workflow_backend._node().pin_states


def test_stage1_exception_carries_class_name():
    with Context() as ctx:
        _stage1_schema_change(ctx)
        ctx.compute()
        assert "CompiledPinCelltypeError" in ctx.tf.exception


def test_bound_mixed_value_exception_carries_class_name():
    """§9 D5 (confirmed 2026-09-26): .exception carries the class name."""
    with Context() as ctx:
        ctx.tf = builder("mixed")
        ctx.src = Cell("mixed")
        ctx.src.set([1, 2])
        ctx.tf.pins.x = ctx.src
        ctx.compute()
        assert "CompiledMixedValueError" in ctx.tf.exception


# ------------------------------------------------------------------
# §4 reporting after import / restore, and inspection on bound transformers


def test_import_incompatible_declaration_warns_as_if_it_had_just_arisen():
    """§4: after graph import the incompatibility is reported as a
    CompiledPinCelltypeWarning, as if it had just arisen."""
    graph = import_modified(_set_celltype("float"))
    with Context() as ctx:
        with pytest.warns(CompiledPinCelltypeWarning, match="'x'"):
            ctx.set_graph(graph)


def test_binding_incompatible_builder_reports_on_the_bound_transformer():
    """§4 (snapshot restore): a standalone builder with an incompatible
    declaration, bound into a context, is reported the same way."""
    tf = builder("int")
    with pytest.warns(CompiledPinCelltypeWarning):
        tf.schema = SCHEMA.replace("int32}", "float64}", 1)
    with Context() as ctx:
        ctx.tf = tf
        ctx.compute()
        assert ctx.tf.celltypes.x == "int"
        assert "incompatible" in ctx.tf.exception
        assert "incompatible" in repr(ctx.tf.schema_celltypes)


def test_bound_schema_celltypes_empty_while_schema_unparsable():
    """§4: schema_celltypes is empty while the schema is unparsable;
    declarations are kept as imported, without validation."""
    graph = import_modified(_bad_schema)
    with Context() as ctx:
        with warnings.catch_warnings():
            warnings.simplefilter("error", CompiledPinCelltypeWarning)
            ctx.set_graph(graph)
        assert dict(ctx.tf.schema_celltypes) == {}
        assert ctx.tf.celltypes.x == "int"


def test_bound_schema_celltypes_is_derived_and_read_only():
    """§4: tf.schema_celltypes on a bound transformer is a read-only mapping
    derived from the schema, not from the declarations."""
    with Context() as ctx:
        ctx.tf = builder("binary")
        view = ctx.tf.schema_celltypes
        assert dict(view) == {"x": "int"}
        with pytest.raises(TypeError):
            view["x"] = "binary"
        text = repr(ctx.tf)
        assert "'binary'" in text and "'int'" in text


# ------------------------------------------------------------------
# §5 Stage 2 table, row 1: checksum not deserializable as the pin celltype

ARRAY_SCHEMA = SCHEMA.replace("dtype: int32}", "dtype: float64, shape: [N]}", 1)


def _undeserializable_checksum(ctx):
    tf = Transformer("c", compiled=True)
    tf.schema = ARRAY_SCHEMA
    tf.celltypes.x = "binary"
    tf.code = CODE
    ctx.tf = tf
    buf = Buffer(b"not an npy buffer")
    buf.tempref()
    ctx.tf.pins.x.set_checksum(buf.get_checksum())
    ctx.compute()


def test_undeserializable_pin_checksum_blocks_with_pin_failure():
    """§5 row 1: a checksum not deserializable as the pin celltype is a pin
    without a valid checksum -> blocked/blocked-by-error, failure on the pin."""
    with Context() as ctx:
        _undeserializable_checksum(ctx)
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"x": "blocked-by-error"}
        assert ctx.tf.pins.x.state == "failed"
        assert "'x'" in ctx.tf.pins.x.exception


def test_undeserializable_pin_checksum_leaves_transformer_exception_none():
    with Context() as ctx:
        _undeserializable_checksum(ctx)
        assert ctx.tf.exception is None
