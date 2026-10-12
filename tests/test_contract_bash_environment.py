"""Phase 2: conda environment survives binding and graph serialization."""
import json
from pathlib import Path
import sys
import uuid

from seamless_transformer import Transformer


def _builder():
    tf = Transformer('bash')
    tf.code = '# nonce: ' + uuid.uuid4().hex + '\nprintf "%s" "$CONDA_PREFIX" > RESULT'
    tf.environment.set_conda_env('seamless1')
    tf.celltypes.result = 'text'
    return tf


def test_bound_bash_transformer_runs_in_conda_environment(make_context):
    ctx = make_context()
    tf = _builder()
    ctx.tf = tf
    assert ctx.tf.environment['conda_environment'] == 'seamless1'
    ctx.compute(timeout=30)
    assert Path(ctx.tf.result.value).resolve() == Path(sys.prefix).resolve()


def test_conda_environment_survives_graph_save_and_load(make_context):
    original = make_context()
    original.tf = _builder()
    # Save before execution: the restored node must really enter the environment.
    graph = json.loads(json.dumps(original.get_graph()))
    restored = make_context()
    restored.set_graph(graph)
    assert restored.tf.environment['conda_environment'] == 'seamless1'
    restored.compute(timeout=30)
    assert Path(restored.tf.result.value).resolve() == Path(sys.prefix).resolve()
