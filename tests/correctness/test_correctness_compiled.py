"""[MOD-15] — compiled transformers are a representation gap, not only an execution gap.

    ``CompiledTransformer(CompiledMixin, TransformerCore)`` inherits
    ``_snapshot_for_call``, and ``TransformerBuilderSnapshot`` has no schema
    field; neither does ``TransformerConfig``, nor ``get_graph()``.  So
    ``ctx.tf = compiled_tf`` silently discards the schema, and the durable graph
    format cannot express a compiled transformer at all.  The format change must
    therefore **precede** its correctness tests.

Which is why the first test here is about ``get_graph()`` and not about a
result: a compiled transformer that cannot be written down cannot be reloaded,
shared, or submitted, no matter how execution is routed.

Measured against the current implementation, binding a ``CompiledTransformer``
also loses its *language*: the node reports ``language="python"`` and stores the
builder object itself as ``callable``, so the very next line —
``ctx.tf.pins.a = 2`` — raises ``AttributeError: Unknown transformer pin 'a'``,
because the signature it checks against is the builder's ``(*args, **kwargs)``.
Configuration fails before execution is ever reached.
"""

from __future__ import annotations

import importlib.util
import shutil

import pytest

from contract_helpers import compute_or_settle, states
from seamless_transformer import CompiledTransformer
from seamless_workflow import Context


def _missing_prerequisite() -> str | None:
    """Both prerequisites, named separately.

    ``gcc`` alone is not enough: the schema half of these tests goes through
    ``seamless_signature``, which is a sibling repository rather than an
    installed dependency, so a checkout without it on ``PYTHONPATH`` fails these
    tests for an environmental reason and reports it as a contract violation.
    """

    if shutil.which("gcc") is None:
        return "gcc is not on PATH"
    if importlib.util.find_spec("seamless_signature") is None:
        return "seamless_signature is not importable (sibling repo not on PYTHONPATH)"
    return None


pytestmark = pytest.mark.skipif(
    _missing_prerequisite() is not None, reason=_missing_prerequisite() or ""
)


ADD_SCHEMA = """\
inputs:
  - {name: a, dtype: int32}
  - {name: b, dtype: int32}
outputs:
  - {name: result, dtype: int32}
"""

ADD_C = """\
#include <stdint.h>
int transform(int32_t a, int32_t b, int32_t *result) {
    *result = a + b;
    return 0;
}
"""


def _builder():
    tf = CompiledTransformer("c")
    tf.schema = ADD_SCHEMA
    tf.code = ADD_C
    return tf


@pytest.mark.a4
def test_the_durable_graph_can_express_a_compiled_transformer():
    ctx = Context()
    ctx.tf = _builder()

    entry = next(node for node in ctx.get_graph()["nodes"] if node["path"] == ["tf"])

    assert entry["language"] == "c", entry
    assert entry.get("schema") == ADD_SCHEMA, sorted(entry)


@pytest.mark.a4
def test_a_compiled_transformer_survives_a_graph_round_trip():
    """Equality of the two graphs is not enough: today they agree on nothing.

    ``get_graph()`` has no schema field, so a round trip of a compiled
    transformer is trivially faithful to a representation that has already lost
    the transformer.  The language assertion is what makes the round trip mean
    something.
    """

    ctx = Context()
    ctx.tf = _builder()

    clone = Context()
    clone.set_graph(ctx.get_graph())

    assert clone.get_graph() == ctx.get_graph()
    assert clone.tf.language == "c"


@pytest.mark.a4
def test_a_compiled_transformer_accepts_its_schema_pins():
    ctx = Context()
    ctx.tf = _builder()

    ctx.tf.pins.a = 2
    ctx.tf.pins.b = 3

    assert ctx.tf.state != "unwired", states(ctx)


@pytest.mark.a4
def test_a_compiled_transformer_produces_its_result():
    ctx = Context()
    ctx.tf = _builder()
    ctx.tf.pins.a = 2
    ctx.tf.pins.b = 3

    compute_or_settle(ctx)

    assert ctx.tf.exception is None, str(ctx.tf.exception)
    assert ctx.tf.result.value == 5


@pytest.mark.now
def test_a_compiled_transformer_is_never_complete_with_a_null_result():
    """The [MOD-15] one-branch fix again: no executable code, no ``complete``.

    Green today, for an incidental reason: binding stores the builder object as
    ``callable``, so the node is ``unwired`` rather than falsely ``complete``.
    It is in the regression net because it must stay true once binding is fixed
    and the node acquires real pins.
    """

    ctx = Context()
    ctx.tf = _builder()

    assert not (ctx.tf.state == "complete" and ctx.tf.result.checksum is None), (
        f"compiled node reported {ctx.tf.state!r} with no result: {states(ctx)}"
    )
