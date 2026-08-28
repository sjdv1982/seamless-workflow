"""[MOD-15], action 1 — stop reporting ``complete`` for a node with no executable code.

    Immediately, independent of everything else: stop reporting ``complete`` for
    a node with no executable code.  ``unwired`` (or ``failed``, with a reason)
    turns a silently wrong answer into a visible gap, and costs one branch.

The branch is ``if cfg.callable is None`` in ``_derive_transformer``, which sets
``complete`` and a null result.  Every non-Python language reaches it, because
``callable`` is only ever a Python function — but so does plain Python, as the
first test below shows: clearing the code of a fully wired Python transformer
turns ``Status: OK`` with the value 3 into ``Status: OK`` with ``None``, and
nothing anywhere reports that the transformer lost its code.

The file states the rule once, in the form that survives A4: **no node is ever
``complete`` while holding no result checksum**.  After A4 a bash node computes
and the rule holds because the node has a real result; before A4 it holds
because the node is honestly reported as not having one.  Only a node that
claims a result it does not have violates it.
"""

from __future__ import annotations

import pytest

from contract_helpers import result_checksum, runtime, state, states
from seamless_transformer import delayed
from seamless_workflow import Context


def add(x, y):
    return x + y


@pytest.mark.a1
def test_clearing_the_code_of_a_wired_transformer_does_not_leave_it_complete():
    ctx = Context()
    ctx.tf = add
    ctx.tf.pins.x = 1
    ctx.tf.pins.y = 2

    ctx.tf.code = None

    assert state(ctx, "tf") != "complete", (
        f"a transformer with no code reported {ctx.tf.status!r} and value "
        f"{ctx.tf.result.value!r}"
    )
    assert result_checksum(ctx, "tf") is None


@pytest.mark.a1
@pytest.mark.parametrize(
    "language,code",
    [
        ("bash", "echo hello > RESULT"),
        ("bash", "head -1 testdata > RESULT"),
    ],
)
def test_a_node_with_no_executable_path_is_not_reported_complete(language, code):
    ctx = Context()
    ctx.tf = delayed(code, language)

    assert not (state(ctx, "tf") == "complete" and result_checksum(ctx, "tf") is None), (
        f"{language} node with no execution path reported {ctx.tf.status!r} and a "
        f"null result"
    )


@pytest.mark.a1
def test_no_node_in_a_graph_is_complete_without_a_result_checksum():
    """The rule over a whole graph, including the cell that copies the claim on."""

    ctx = Context()
    ctx.python = add
    ctx.python.pins.x = 1
    ctx.python.pins.y = 2
    ctx.bash = delayed("echo hello > RESULT", "bash")
    ctx.out = ctx.bash

    claiming = {
        path: entry
        for path, entry in runtime(ctx).items()
        if entry["state"] == "complete" and entry["checksum"] is None
    }

    assert claiming == {}, (claiming, states(ctx))
