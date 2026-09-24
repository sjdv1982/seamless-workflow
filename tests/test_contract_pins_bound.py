"""Bound Pin contract coverage (seamless/docs/agent/contracts/pins.md).

Complements test_pin_handles.py, test_optional_pin_contract.py,
test_pin_celltype_changes.py, test_transformer_block_reason.py and
test_canonical_handles.py for the bound (workflow Context) mode.
"""
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

@pytest.mark.xfail(strict=False, reason=(
    "pins.md §Which pins exist: bound `del tf.celltypes.x` on signature-less code "
    "resets the celltype to mixed and keeps the pin and its input instead of "
    "removing the declaration (standalone removes it)"))
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
        with pytest.raises((TypeError, ValueError)):
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
                                      "deepcell", "folder"])
def test_bound_required_null_from_upstream_is_reported_on_the_pin(celltype):
    with Context() as ctx:
        ctx.source = Cell("plain")
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
        assert ("tf",) not in ctx._runtime.current_runs


@pytest.mark.parametrize("celltype", ["plain", "mixed", "bytes"])
def test_bound_required_nullable_pin_accepts_null(celltype):
    with Context() as ctx:
        ctx.tf = identity
        ctx.tf.celltypes.value = celltype
        ctx.tf.pins.value = None
        ctx.compute(timeout=10)
        assert ctx.tf.state == "complete", ctx.tf.exception
        assert ctx.tf.pins.value.checksum == NULL


_FORMAT = pytest.mark.xfail(strict=False, reason=(
    "pins.md §Null, required pins, optional pins (identity rule): the dropped "
    "folder/deepfolder pin leaves its __format__ entry in the transformation dict"))


@pytest.mark.parametrize("celltype", [
    pytest.param(ct, marks=_FORMAT) if ct in ("folder", "deepfolder") else ct
    for ct in ["plain", "mixed", "bytes", "binary", "int", "float", "bool", "str",
               "text", "ipython", "checksum", "deepcell", "deepfolder", "folder",
               "module"]])
def test_bound_optional_null_has_absent_identity_reactive_and_snapshot(celltype):
    with Context() as ctx:
        ctx.tf = optional_identity
        ctx.tf.celltypes.value = celltype
        ctx.compute(timeout=10)
        assert ctx.tf.state == "complete", ctx.tf.exception
        absent = ctx.tf().construct()
        absent_result = ctx.tf.result.checksum
        ctx.source = Cell("plain")
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
        assert ctx.tf.result.checksum is None


def test_bound_optional_pin_is_not_lazy():
    calls = []

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
@pytest.mark.xfail(strict=False, reason=(
    "pins.md §Conversion at the pin (wiring rule): the two explicit spellings "
    "ctx.b[3].as_celltype('plain') and ctx.b.as_celltype('plain')[3] are refused "
    "on a pin (the same spellings are also refused for a Cell target today)"))
def test_bound_wiring_rule_explicit_spellings_are_accepted(spelling):
    with Context() as ctx:
        ctx.b = Cell("text")
        ctx.b.set("[10, 20, 30, 40]")
        ctx.tf = identity
        ctx.tf.celltypes.value = "plain"
        with pytest.raises(TypeError, match="as_celltype"):
            ctx.tf.pins.value = ctx.b[3]
        if spelling == "project-then-convert":
            ctx.tf.pins.value = ctx.b[3].as_celltype("plain")
        else:
            ctx.tf.pins.value = ctx.b.as_celltype("plain")[3]
        ctx.compute(timeout=10)
        assert ctx.tf.pins.value.input_celltype == ctx.tf.pins.value.celltype == "plain"
        assert ctx.tf.result.value == 40


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
