"""[MOD-15] — non-Python transformers, starting with bash.

    A Context transformer today executes ``cfg.callable(**kwargs)``, so bash and
    compiled transformers have no execution path at all — and rather than being
    rejected, a node whose ``callable`` is ``None`` is set to ``complete`` with a
    null result.  Measured: a ``delayed(…, language="bash")`` transformer bound
    into a Context reports ``Status: OK`` and yields ``None``.  The suite is
    silent because it only smoke-tests state.

So these tests assert **results**, not states.  They are the acceptance criteria
for carrying the node's stored execution envelope into the submitted
transformation (A4), not a regression suite for it.

Ported from legacy ``tests/workflow/bash.py``, whose expected output
(``test-outputs/bash.out``) pins both shapes: a bash transformer whose ``RESULT``
is a file, and one whose ``RESULT`` is a directory and therefore arrives as a
dict keyed by relative path.

Sequencing note from [MOD-15]: bash before compiled, because bash needs only an
execution path while compiled is also a representation gap.
"""

from __future__ import annotations

import pytest

from contract_helpers import settle, state, states
from seamless_transformer import delayed, direct
from seamless_workflow import Context


TESTDATA = "a \nb \nc \nd \ne \nf \n"

HEAD_CODE = "head -$lines testdata > RESULT"

DIRECTORY_CODE = (
    "head -$lines testdata > firstdata; "
    "mkdir -p RESULT/input; "
    "cp firstdata RESULT; "
    "cp testdata RESULT/input"
)


def _bash_context(code):
    ctx = Context()
    ctx.tf = delayed(code, "bash")
    ctx.tf.celltypes["lines"] = "int"
    ctx.tf.pins.testdata = TESTDATA
    ctx.tf.pins.lines = 3
    return ctx


@pytest.mark.a1
def test_a_bash_transformer_is_never_complete_with_a_null_result():
    """The one-branch fix of [MOD-15], stated for the language it was found with.

    This is the half that does not wait for A4: whatever the Context can or
    cannot execute, reporting ``complete`` for a node that produced nothing is a
    silently wrong answer where ``unwired``/``failed`` would be a visible gap.
    """

    ctx = _bash_context(HEAD_CODE)

    assert not (state(ctx, "tf") == "complete" and ctx.tf.result.checksum is None), (
        f"bash node reported {ctx.tf.status!r} with no result: {states(ctx)}"
    )


@pytest.mark.a4
def test_a_bash_transformer_writes_its_result_file():
    ctx = _bash_context(HEAD_CODE)

    assert settle(ctx, timeout=120), states(ctx)

    assert state(ctx, "tf") == "complete", states(ctx)
    assert ctx.tf.result.value == "a \nb \nc \n"


@pytest.mark.a4
def test_a_bash_result_directory_arrives_as_a_dict():
    ctx = _bash_context(DIRECTORY_CODE)

    assert settle(ctx, timeout=120), states(ctx)

    assert ctx.tf.result.value == {
        "firstdata": "a \nb \nc \n",
        "input/testdata": TESTDATA,
    }


@pytest.mark.a4
def test_a_bash_transformer_reacts_to_a_pin_edit():
    ctx = _bash_context(HEAD_CODE)
    assert settle(ctx, timeout=120), states(ctx)
    assert ctx.tf.result.value == "a \nb \nc \n"

    ctx.tf.pins.lines = 4

    assert state(ctx, "tf") != "complete", states(ctx)
    assert settle(ctx, timeout=120), states(ctx)
    assert ctx.tf.result.value == "a \nb \nc \nd \n"


@pytest.mark.a4
def test_a_bound_bash_transformer_agrees_with_the_standalone_one():
    """Weaker than A4's ``tf_checksum`` exit evidence, and available earlier.

    If the Context and ``direct``/``delayed`` disagree on the *result* of the
    same computation, they cannot possibly agree on the cache key.
    """

    standalone = direct(HEAD_CODE, "bash")
    standalone.args.testdata = TESTDATA
    standalone.celltypes.lines = int
    expected = standalone(lines=3)

    ctx = _bash_context(HEAD_CODE)
    assert settle(ctx, timeout=120), states(ctx)

    assert ctx.tf.result.value == expected
