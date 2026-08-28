"""[MOD-15] — a transformer carrying a non-default ``environment``.

The environment is the envelope field whose failure mode is the quietest: the
node keeps its declaration through binding and through ``get_graph()``, and then
the Context computes as if it were not there.  A transformer that declares
requirements it does not have therefore *succeeds* today, which is the one
outcome an environment declaration is supposed to make impossible.

Note the shape of these tests.  The positive test passes today **for the wrong
reason** — the environment is ignored rather than satisfied — so on its own it
proves nothing; it is the control that keeps the negative test honest by showing
the same graph computing correctly when its requirements *are* met.  The
discriminating assertion is the negative one.

The exact failure a missing requirement should produce is deliberately not
asserted.  Measured today, the standalone stack answers an unsatisfiable
``which`` with ``CacheMissError``, which is a poor message for "your environment
is not available"; pinning it here would freeze that.  What is asserted is the
part that cannot be argued: the node must not report ``complete``.
"""

from __future__ import annotations

import pytest

from contract_helpers import settle, state, states
from seamless_transformer import delayed
from seamless_workflow import Context


def add_one(x):
    return x + 1


def _context(which):
    builder = delayed(add_one)
    builder.environment.set_which(which)
    ctx = Context()
    ctx.tf = builder
    ctx.tf.pins.x = 3
    return ctx


@pytest.mark.now
def test_the_environment_survives_binding_and_a_graph_round_trip():
    ctx = _context(["head"])

    entry = next(node for node in ctx.get_graph()["nodes"] if node["path"] == ["tf"])
    assert entry["environment"] == {"which": ["head"]}
    assert ctx.tf.environment == {"which": ["head"]}

    clone = Context()
    clone.set_graph(ctx.get_graph())
    assert clone.tf.environment == {"which": ["head"]}


@pytest.mark.now
def test_a_satisfiable_environment_does_not_change_the_result():
    """Control case; see the module docstring for why it is not discriminating."""

    ctx = _context(["head"])

    assert settle(ctx, timeout=120), states(ctx)
    assert ctx.tf.result.value == 4


@pytest.mark.a4
def test_an_unsatisfiable_environment_must_not_silently_succeed():
    ctx = _context(["definitely-not-a-real-binary-xyz"])

    settle(ctx, timeout=120)

    assert state(ctx, "tf") != "complete", (
        "a transformer declaring an unavailable requirement computed anyway: "
        f"{states(ctx)}, value={ctx.tf.result.value!r}"
    )
    assert ctx.tf.result.value is None
