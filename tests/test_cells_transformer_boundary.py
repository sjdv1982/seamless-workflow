"""Cell/Pin boundary; Transformer-dependent cases intentionally added last.

Contracts: cells.md (CellBase and input rejection), pins.md (A Pin is not a
source), direct-delayed-and-transformation.md (decorators return builders).
The task explicitly requires Transformer-building tests to be ahead-of-code
xfails. Keep these separate from ordinary Cell regressions.
"""
import pytest
from seamless import Cell
from seamless.cell_errors import ProjectionError
from seamless_transformer import delayed

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="contract ahead of code: Transformer-building Cell/Pin boundary tests (explicit feature 5 test-alignment instruction)",
)


def identity(x):
    return x


@pytest.fixture
def endpoints(make_context):
    ctx = make_context()
    ctx.tf = delayed(identity)
    ctx.cell = Cell("plain")
    # An unwired required pin prevents the eager Context from running the body.
    yield ctx.cell, ctx.tf.pins.x


@pytest.mark.parametrize("operation", ["constructor", "with_input", "set", "value", "build_override"])
def test_cell_rejects_pin_with_source_guidance(endpoints, operation):
    cell, pin = endpoints
    with pytest.raises(TypeError, match=r"pin\.source"):
        if operation == "constructor":
            Cell(source=pin)
        elif operation == "with_input":
            cell.with_input(pin)
        elif operation == "set":
            cell.set(pin)
        elif operation == "value":
            cell.value = pin
        else:
            cell.build(pin)
    assert cell.source is None
    assert cell.checksum is None


@pytest.mark.parametrize("operation", [
    lambda p: p == 1, lambda p: p != 1,
    lambda p: p < 1, lambda p: p <= 1,
    lambda p: p > 1, lambda p: p >= 1,
    bool, len, iter,
], ids=["eq", "ne", "lt", "le", "gt", "ge", "bool", "len", "iter"])
def test_cellbase_handle_guards_also_cover_pins(endpoints, operation):
    _, pin = endpoints
    with pytest.raises(ProjectionError):
        operation(pin)
