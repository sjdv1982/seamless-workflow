"""[MOD-15] — ``modules`` must stop being inert graph decoration.

    ``language``, ``modules``, ``globals``, ``environment``, ``scratch``,
    ``local`` and ``direct_print`` are serialized into ``get_graph()`` today and
    then dropped, so a Context transformer is Python-only and module-less
    whatever its configuration says.

Measured: a Context transformer whose body does ``import helper_module`` fails
with ``ModuleNotFoundError: No module named 'helper_module'`` even though the
module is declared on the node and round-trips through ``get_graph()``.  The
identical declaration on a standalone ``direct``/``delayed`` transformer works.

Ported from legacy ``tests/workflow/module-simplified.py``, which declared the
module as a plain dict (``{"type": "interpreted", "language": "python",
"code": ...}``) and asserted the transformer's result — the same two things
asserted here.
"""

from __future__ import annotations

import pytest

from contract_helpers import settle, state, states
from seamless_transformer import direct
from seamless_workflow import Context


HELPER_MODULE = {
    "type": "interpreted",
    "language": "python",
    "code": "def triple(x):\n    return 3 * x\n",
}


def use_helper(x):
    import helper_module

    return helper_module.triple(x)


def _context():
    ctx = Context()
    ctx.tf = use_helper
    ctx.tf.modules.helper_module = HELPER_MODULE
    ctx.tf.pins.x = 5
    return ctx


@pytest.mark.now
def test_a_declared_module_survives_a_graph_round_trip():
    """The durable half already works; it is the execution half that does not."""

    ctx = _context()
    entry = next(node for node in ctx.get_graph()["nodes"] if node["path"] == ["tf"])
    assert entry["modules"] == {"helper_module": HELPER_MODULE}

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert clone.get_graph() == ctx.get_graph()


@pytest.mark.a4
def test_a_transformer_using_a_module_produces_its_result():
    ctx = _context()

    assert settle(ctx, timeout=120), states(ctx)

    assert state(ctx, "tf") == "complete", (states(ctx), ctx.tf.exception)
    assert ctx.tf.result.value == 15


@pytest.mark.a4
def test_the_context_agrees_with_the_standalone_form():
    standalone = direct(use_helper)
    standalone.modules["helper_module"] = HELPER_MODULE
    expected = standalone(x=5)

    ctx = _context()
    assert settle(ctx, timeout=120), states(ctx)

    assert ctx.tf.result.value == expected


@pytest.mark.a4
def test_a_module_edit_is_an_ordinary_perturbation():
    """Changing a module changes the transformation identity, like any input."""

    ctx = _context()
    assert settle(ctx, timeout=120), states(ctx)
    assert ctx.tf.result.value == 15

    ctx.tf.modules.helper_module = {
        "type": "interpreted",
        "language": "python",
        "code": "def triple(x):\n    return 30 * x\n",
    }

    assert state(ctx, "tf") != "complete", states(ctx)
    assert settle(ctx, timeout=120), states(ctx)
    assert ctx.tf.result.value == 150
