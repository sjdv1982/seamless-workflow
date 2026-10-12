"""Bash environments survive binding and graph serialization."""
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


UBUNTU = 'ubuntu@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401'


def _container_builder():
    tf = Transformer('bash')
    tf.code = '# nonce: ' + uuid.uuid4().hex + '\nprintf "%s" "$PWD" > RESULT'
    tf.environment.set_docker({'name': UBUNTU})
    tf.celltypes.result = 'text'
    return tf


import pytest
import subprocess


@pytest.fixture(params=['docker', 'podman'])
def container_runtime(request, monkeypatch):
    runtime = request.param
    result = subprocess.run([runtime, 'info'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([runtime, 'image', 'inspect', UBUNTU], capture_output=True)
    if result.returncode:
        result = subprocess.run([runtime, 'pull', UBUNTU], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setenv('SEAMLESS_CONTAINER_RUNTIME', runtime)
    monkeypatch.setenv('SEAMLESS_CONTAINER_PULL', 'never')
    return runtime


def test_bound_bash_transformer_runs_in_container(make_context, container_runtime):
    ctx = make_context()
    ctx.tf = _container_builder()
    assert ctx.tf.environment['docker']['name'] == UBUNTU
    ctx.compute(timeout=60)
    assert ctx.tf.result.value == '/seamless-job'


def test_container_environment_survives_graph_save_and_load(make_context, container_runtime):
    original = make_context()
    original.tf = _container_builder()
    graph = json.loads(json.dumps(original.get_graph()))
    restored = make_context()
    restored.set_graph(graph)
    assert restored.tf.environment['docker']['name'] == UBUNTU
    restored.compute(timeout=60)
    assert restored.tf.result.value == '/seamless-job'
