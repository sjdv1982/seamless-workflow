"""Bound Pin contract coverage (seamless/docs/agent/contracts/pins.md).

Complements test_pin_handles.py, test_optional_pin_contract.py,
test_pin_celltype_changes.py, test_transformer_block_reason.py and
test_canonical_handles.py for the bound (workflow Context) mode.
"""
import re

import pytest
from seamless import Buffer, Cell, Checksum
from seamless_transformer import delayed
from seamless_workflow import Context

NULL = Buffer(None, "plain").get_checksum()


def identity(value):
    return value


def optional_identity(value=None):
    return value


def boom():
    raise RuntimeError("upstream failed")


# --- Reaching pins ---------------------------------------------------------

def test_bound_args_is_an_exact_alias_of_pins():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.args.value = 4
        assert ctx.tf.pins.value.value == 4
        ctx.tf.pins.value = 5
        ctx.compute(timeout=10)
        assert ctx.tf.args.value.value == 5
        assert dir(ctx.tf.args) == dir(ctx.tf.pins)
        # The same collection, not the same object (pins.md §Reaching pins).
        assert ctx.tf.args is not ctx.tf.pins


def test_bound_result_is_never_a_pin_name():
    with Context() as ctx:
        ctx.tf = identity
        ctx.loose = delayed("result = value")
        for tf in (ctx.tf, ctx.loose):
            for operation in (lambda: tf.pins.result,
                              lambda: tf.pins["result"],
                              lambda: tf.pins.__setitem__("result", 1),
                              lambda: tf.pins.__delitem__("result")):
                with pytest.raises(AttributeError):
                    operation()
        # On a bound transformer, tf.result is the result handle, not a pin.
        assert not isinstance(ctx.tf.result, type(ctx.tf.pins.value))


def test_bound_item_form_is_not_an_escape_hatch():
    with Context() as ctx:
        ctx.tf = identity
        with pytest.raises(AttributeError):
            ctx.tf.pins["typo"]
        with pytest.raises(AttributeError, match=r"reached as \.pins\['value'\]"):
            ctx.tf.value = 1


# --- Which pins exist ------------------------------------------------------

def test_bound_del_celltype_removes_signatureless_declaration():
    with Context() as ctx:
        ctx.loose = delayed("result = x")
        ctx.loose.celltypes.x = "int"
        assert ctx.loose.pins.x.state == "unwired"
        assert ctx.loose.block_reason == {"x": "unwired"}
        ctx.loose.pins.x = 3
        del ctx.loose.celltypes.x
        with pytest.raises(AttributeError):
            ctx.loose.pins.x
        assert "x" not in ctx.loose.optional_pins
        node, = [n for n in ctx.get_graph()["nodes"] if n["path"] == ["loose"]]
        assert node["pins"] == {}


# --- Pins hold checksums ---------------------------------------------------

def test_bound_invalid_literal_raises_at_assignment():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = "int"
        # "exactly as Cell.set does": Cell("int").set("abc") raises ValueError (cells.md)
        with pytest.raises(ValueError):
            ctx.tf.pins.value = "not an integer"
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "unwired"
        assert ctx.tf.state == "unwired"


def test_bound_set_checksum_none_clears_and_keeps_declaration():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = "int"
        ctx.tf.pins.value = 3
        ctx.compute(timeout=10)
        ctx.tf.pins.value.set_checksum(None)
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "unwired"
        assert ctx.tf.celltypes.value == "int"
        assert ctx.tf.block_reason == {"value": "unwired"}


# --- Null, required pins, optional pins ------------------------------------

@pytest.mark.parametrize("celltype", ["float", "str", "text", "binary", "checksum",
                                      "deepcell", "folder", "module"])
def test_bound_required_null_from_upstream_is_reported_on_the_pin(celltype):
    with Context() as ctx:
        ctx.source = Cell(celltype)  # same celltype: no conversion involved
        ctx.source.set(None)
        ctx.tf = identity
        ctx.tf.celltypes.value = celltype
        with pytest.raises(TypeError, match=(
                f"Required pin 'value' with celltype '{celltype}' cannot accept null")):
            ctx.tf.pins.value = None
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        pin = ctx.tf.pins.value
        assert pin.state == "failed"
        assert pin.exception == (
            f"Required pin 'value' with celltype '{celltype}' cannot accept null")
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"value": "blocked-by-error"}
        # A pin with no valid checksum does not set the transformer's exception.
        assert ctx.tf.exception is None
        assert ("tf",) not in ctx._runtime.current_runs


@pytest.mark.parametrize("celltype", ["int", "text", "deepcell", "module"])
def test_bound_required_null_from_upstream_builds_no_snapshot_transformation(celltype):
    with Context() as ctx:
        ctx.source = Cell(celltype)
        ctx.source.set(None)
        ctx.tf = identity
        ctx.tf.celltypes.value = celltype
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "failed"
        assert ctx.tf().construct() is None


@pytest.mark.parametrize("celltype", ["plain", "mixed", "bytes"])
def test_bound_required_nullable_pin_accepts_null(celltype):
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = celltype
        ctx.tf.pins.value = None
        ctx.compute(timeout=10)
        assert ctx.tf.state == "complete", ctx.tf.exception
        assert ctx.tf.pins.value.checksum == NULL


@pytest.mark.parametrize("celltype", [
    "plain", "mixed", "bytes", "binary", "int", "float", "bool", "str", "text",
    "ipython", "checksum", "deepcell", "deepfolder", "folder", "module"])
def test_bound_optional_null_has_absent_identity_reactive_and_snapshot(celltype):
    with Context() as ctx:
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = celltype
        ctx.compute(timeout=10)
        assert ctx.tf.state == "complete", ctx.tf.exception
        absent = ctx.tf().construct()
        absent_result = ctx.tf.result.checksum
        ctx.source = Cell(celltype)  # same celltype: no conversion involved
        ctx.source.set(None)
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "complete"
        assert ctx.tf.state == "complete", ctx.tf.exception
        assert ctx.tf().construct() == absent
        assert ctx.tf.result.checksum == absent_result
        runtime, = [n["runtime"] for n in ctx.get_graph(runtime=True)["nodes"]
                    if n["path"] == ["tf"]]
        assert runtime["run"]["current"]["identity"] == absent.hex()


def test_bound_optional_failing_upstream_is_a_failure_not_absence():
    with Context() as ctx:
        ctx.up = boom
        ctx.tf = optional_identity
        ctx.tf.pins.value = ctx.up
        ctx.compute(timeout=10)
        assert ctx.up.state == "failed"
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"value": "blocked-by-error"}
        assert ctx.tf.exception is None
        assert ctx.tf.result.checksum is None


def test_bound_optional_pin_is_not_lazy():
    def produce():
        return 5

    with Context() as ctx:
        ctx.up = produce
        ctx.tf = optional_identity
        ctx.tf.pins.value = ctx.up
        ctx.compute(timeout=10)
        assert ctx.up.state == "complete"
        assert ctx.tf.result.value == 5
        assert "value" in ctx.tf().construct().resolve("plain")


def test_bound_null_result_rejected_for_non_nullable_celltype():
    def returns_none():
        return None

    with Context() as ctx:
        ctx.tf = returns_none
        ctx.tf.celltypes.result = "int"
        ctx.compute(timeout=10)
        assert ctx.tf.state == "failed"
        assert "Null result is not allowed for celltype 'int'" in ctx.tf.exception


# --- Conversion / wiring ---------------------------------------------------

@pytest.mark.parametrize("spelling", ["project-then-convert", "convert-then-project"])
def test_bound_wiring_rule_explicit_spellings_are_accepted(spelling):
    with Context() as ctx:
        ctx.b = Cell("text")
        ctx.b.set("[10, 20, 30, 40]")
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        with pytest.raises(TypeError):
            ctx.tf.pins.value = ctx.b[3]
        if spelling == "project-then-convert":
            ctx.tf.pins.value = ctx.b[3].as_celltype("plain")
        else:
            ctx.tf.pins.value = ctx.b.as_celltype("plain")[3]
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.input_celltype == ctx.tf.pins.value.celltype == "plain"
        # The explicit operations keep their order: character projection or list item.
        expected = "," if spelling == "project-then-convert" else 40
        assert ctx.tf.result.value == expected


# --- Reads -----------------------------------------------------------------

def test_bound_build_and_compute_refuse_a_replacement_input():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.pins.value = 3
        ctx.compute(timeout=10)
        pin = ctx.tf.pins.value
        other = Buffer(4, "mixed").get_checksum()
        with pytest.raises(TypeError, match="Pin.build does not accept a replacement input"):
            pin.build(other)
        with pytest.raises(TypeError, match="Pin.compute does not accept a replacement input"):
            pin.compute(other)


def test_bound_pin_compute_returns_the_pin_checksum():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = "str"
        ctx.source = Cell("int")
        ctx.source.set(42)
        ctx.tf.pins.value = ctx.source
        checksum = ctx.tf.pins.value.compute(timeout=10)
        assert checksum == ctx.tf.pins.value.checksum
        assert ctx.tf.pins.value.value == "42"


def test_bound_pin_exception_is_a_string_or_none():
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = "int"
        ctx.tf.pins.value = 1
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.exception is None
        ctx.source = Cell("str")
        ctx.source.set("abc")
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        assert isinstance(ctx.tf.pins.value.exception, str)


def test_bound_pin_fingertip_and_bytes_run():
    with Context() as ctx:
        ctx.tf = identity
        assert ctx.tf.pins.value.fingertip() is None
        assert ctx.tf.pins.value.exception is None
        ctx.tf.celltypes.value = "bytes"
        ctx.tf.pins.value = b"xyz"
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.run() == b"xyz"
        buffer = ctx.tf.pins.value.fingertip()
        assert isinstance(buffer, Buffer)
        assert buffer.get_checksum() == ctx.tf.pins.value.checksum


def test_bound_pin_surface_has_no_cell_only_members():
    with Context() as ctx:
        ctx.tf = identity
        pin = ctx.tf.pins.value
        for name in ("path", "prune", "mount", "validator"):
            assert not hasattr(pin, name), name


# --- Rulings of 2026-09-26 (contract-clarity-rulings.md) -------------------

def test_bound_wiring_refusal_message_names_both_spellings():
    with Context() as ctx:
        ctx.b = Cell("text")
        ctx.b.set("[10, 20, 30, 40]")
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        with pytest.raises(TypeError) as excinfo:
            ctx.tf.pins.value = ctx.b[3]
        lines = str(excinfo.value).splitlines()
        assert lines[0] == "would convert text -> plain behind a projection."
        assert len(lines) == 3
        # Projection first, then conversion, each glossed (pins.md §Wiring example).
        assert re.match(
            r'^\s*ctx\.tf\.pins\.value = ctx\.b\[3\]\.as_celltype\("plain"\)\s+'
            r'# item 3 of the text \(a character\), as plain$', lines[1])
        assert re.match(
            r'^\s*ctx\.tf\.pins\.value = ctx\.b\.as_celltype\("plain"\)\[3\]\s+'
            r'# item 3 of the parsed list$', lines[2])


def test_bound_symbol_handle_with_a_path_counts_as_a_path():
    with Context() as ctx:
        ctx.b = Cell("text")
        ctx.b.set("[10, 20, 30, 40]")
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        handle = ctx.b[3]  # an anonymous cell (Ruling 1, sub-ruling c)
        with pytest.raises(TypeError):
            ctx.tf.pins.value = handle
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "unwired"


def test_bound_retyping_an_upstream_source_leaves_the_pin_miswired():
    with Context() as ctx:
        ctx.b = Cell("plain")
        ctx.b.set([10, 20, 30, 40])
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        ctx.tf.pins.value = ctx.b[3]
        ctx.compute(timeout=10)
        assert ctx.tf.result.value == 40
        ctx.b.celltype = "text"  # a valid request: never refused
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "miswired"
        assert ctx.tf.state == "miswired"
        assert ctx.tf.block_reason == {"value": "miswired"}
        assert ctx.tf.exception is None


def test_bound_wiring_rule_does_not_apply_to_call_time_arguments():
    # An Expression, not a Cell: whether Cells may be call-time arguments at all
    # is undecided (pins.md §Call-time arguments).
    source = Cell("text")
    source.set("[10, 20, 30, 40]")
    projected = source[3].build()
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        assert ctx.tf(projected).run() == ","
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "unwired"


def test_bound_pin_fed_through_a_symbol_path_cannot_be_retyped():
    with Context() as ctx:
        ctx.b = Cell("text")
        ctx.b.set("[10, 20, 30, 40]")
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        ctx.tf.pins.value = ctx.b.as_celltype("plain")[3]
        with pytest.raises(TypeError):
            ctx.tf.celltypes.value = "int"
        with pytest.raises(TypeError):
            ctx.tf.pins.value.celltype = "int"
        ctx.compute(timeout=10)
        assert ctx.tf.result.value == 40


@pytest.mark.parametrize("spelling", ["projection", "as_celltype"])
def test_anonymous_bound_handle_cannot_feed_a_standalone_pin(spelling):
    from seamless_workflow.errors import DependencyError
    with Context() as ctx:
        ctx.c = Cell("plain")
        ctx.c.set([1, 2, 3])
        ctx.t = Cell("text")
        ctx.t.set("[1, 2, 3]")
        tf = delayed(identity)
        tf.celltypes.value = "plain"
        handle = ctx.c[0] if spelling == "projection" else ctx.t.as_celltype("plain")
        # pins.md: the exception class is not separately ruled for pins; cells.md
        # rules DependencyError for the Cell counterpart, Cell(source=<handle>).
        # Accept either until a pin-specific ruling exists.
        with pytest.raises((TypeError, DependencyError)):
            tf.pins.value = handle
        assert tf.pins.value.state == "unwired"


def test_anonymous_bound_handle_into_another_context_raises_dependency_error():
    from seamless_workflow.errors import DependencyError
    with Context() as ctx, Context() as other:
        ctx.c = Cell("plain")
        ctx.c.set([1, 2, 3])
        other.tf = identity
        with pytest.raises(DependencyError):
            other.tf.pins.value = ctx.c[0]
        assert other.tf.pins.value.state == "unwired"


@pytest.mark.parametrize("celltype", ["deepcell", "deepfolder", "folder"])
def test_bound_optional_null_through_an_illegal_conversion_is_not_absence(celltype):
    with Context() as ctx:
        ctx.source = Cell("plain")
        ctx.source.set(None)
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = celltype
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.checksum is None
        assert ctx.tf.result.checksum is None
        assert ("tf",) not in ctx._runtime.current_runs


def test_bound_failed_conversion_is_blocked_by_error_without_tf_exception():
    with Context() as ctx:
        ctx.source = Cell("str")
        ctx.source.set("abc")
        ctx.tf = identity
        ctx.tf.celltypes.value = "int"
        ctx.tf.pins.value = ctx.source
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.state == "failed"
        assert isinstance(ctx.tf.pins.value.exception, str)
        assert ctx.tf.state == "blocked"
        assert ctx.tf.block_reason == {"value": "blocked-by-error"}
        assert ctx.tf.exception is None
        assert ("tf",) not in ctx._runtime.current_runs


def _derive(incoming, pins=("x", "y")):
    from types import SimpleNamespace
    from seamless_workflow.builder_state import BoundTransformerBackend
    from seamless_workflow.graph import Node, TransformerConfig
    from seamless_workflow.reactive import Reactive

    node = Node("transformer", transformer_config=TransformerConfig(pins=set(pins)))
    context = SimpleNamespace(
        _incoming_for=lambda path: incoming,
        _source_state=lambda edge: (edge, None),
        _suspend=lambda path: None,
        _replace_current_checksum=lambda path, checksum: None,
    )
    Reactive._derive_transformer(context, ("tf",), node)
    backend = SimpleNamespace(_node=lambda: node)
    return node, BoundTransformerBackend.block_reason.fget(backend)


@pytest.mark.parametrize("incoming,state,expected", [
    pytest.param({("code",): "computing", ("x",): "unwired", ("y",): "computing"},
                 "blocked", {"x": "blocked-by-unwired"},
                 id="blocked-with-waiting-inputs"),
    pytest.param({("code",): "computing", ("x",): "failed"},
                 "unwired", {"x": "blocked-by-error", "y": "unwired"},
                 id="unwired-with-waiting-code"),
    pytest.param({("code",): "computing", ("x",): "computing", ("y",): "computing"},
                 "waiting", None, id="waiting-is-none"),
    pytest.param({("code",): "failed", ("x",): "unwired", ("y",): "failed"},
                 "blocked", {"code": "blocked-by-error", "x": "blocked-by-unwired",
                             "y": "blocked-by-error"}, id="all-blocking"),
])
def test_block_reason_lists_only_inputs_that_are_not_complete_or_waiting(
        incoming, state, expected):
    node, block_reason = _derive(incoming)
    assert node.state == state
    assert block_reason == expected
    allowed = {"miswired", "unwired", "blocked-by-miswiring", "blocked-by-unwired",
               "blocked-by-error"}
    if block_reason is not None:
        assert set(block_reason.values()) <= allowed
