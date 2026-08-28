from __future__ import annotations

import shutil

import pytest

from seamless_transformer import CompiledTransformer, delayed


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


def use_helper_module(value):
    import workflow_test_helper

    return workflow_test_helper.triple(value)


def test_bash_transformer_writes_result_instead_of_completing_with_none(make_context):
    """Port legacy ``workflow/bash.py`` and assert the value, not status text."""

    transformer = delayed('printf "%s" "$word" > RESULT', "bash")
    transformer.celltypes.word = "text"
    transformer.celltypes.result = "text"
    context = make_context()
    context.bash = transformer
    context.bash.pins.word = "seamless-bash"

    assert context.bash.run() == "seamless-bash"
    assert context.bash.result.value == "seamless-bash"


def test_non_executable_context_node_never_reports_false_completion(make_context):
    """Make the MOD-15 one-branch safety fix independently observable."""

    transformer = delayed('printf "done" > RESULT', "bash")
    transformer.celltypes.result = "text"
    context = make_context()
    context.bash = transformer

    node = context._graph.nodes[("bash",)]
    assert node.state != "complete"
    assert node.current_checksum is None


@pytest.mark.skipif(not shutil.which("gcc"), reason="gcc is required")
def test_compiled_transformer_preserves_schema_and_executes_in_context(make_context):
    transformer = CompiledTransformer("c")
    transformer.schema = ADD_SCHEMA
    transformer.code = ADD_C
    context = make_context()
    context.compiled = transformer
    context.compiled.pins.a = 12
    context.compiled.pins.b = 30

    graph_entry = next(
        entry for entry in context.get_graph()["nodes"] if entry["path"] == ["compiled"]
    )
    assert graph_entry["schema"] == ADD_SCHEMA
    assert context.compiled.run() == 42
    assert context.compiled.result.value == 42


def test_python_module_execution_envelope_is_live_in_context(make_context):
    transformer = delayed(use_helper_module)
    transformer.modules.workflow_test_helper = {
        "code": "def triple(value):\n    return value * 3\n",
        "language": "python",
        "type": "interpreted",
    }
    context = make_context()
    context.with_module = transformer
    context.with_module.pins.value = 14

    assert context.with_module.run() == 42
    assert context.with_module.result.value == 42


def test_nondefault_environment_is_carried_by_context_execution(
    make_context, monkeypatch
):
    # Remove the inherited value so the result can only be produced when the
    # submitted execution envelope activates the requested environment.
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    transformer = delayed(
        'test "$CONDA_DEFAULT_ENV" = seamless1 && printf environment-ok > RESULT',
        "bash",
    )
    transformer.celltypes.result = "text"
    transformer.environment.set_conda_env("seamless1")
    context = make_context()
    context.with_environment = transformer

    assert context.with_environment.run() == "environment-ok"
    assert context.with_environment.result.value == "environment-ok"
