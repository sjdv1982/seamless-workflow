"""Phase D bound logs follow the current run without demanding work."""
import pytest
from seamless_transformer import delayed
from seamless_transformer.transformation_class import Transformation
from seamless_workflow import Cell


def add(a, b):
    return a + b


def add_required(a, b, c):
    return a + b + c


def fail(value):
    raise ValueError('bound-logs-failure-' + str(value))


def test_standalone_transformer_logs_is_live_node_only():
    transformer = delayed(add)
    with pytest.raises(AttributeError):
        _ = transformer.logs
    assert not hasattr(transformer, 'logs')
    transformer.pins.a = 1
    transformer.pins.b = 2
    assert transformer.build().logs is None


def test_bound_unwired_logs_are_passive(make_context, monkeypatch):
    ctx = make_context()
    ctx.tf = add
    def forbidden(*args, **kwargs):
        raise AssertionError('reading logs demanded a Transformation')
    monkeypatch.setattr(Transformation, 'construct', forbidden)
    monkeypatch.setattr(Transformation, 'construction', forbidden)
    monkeypatch.setattr(Transformation, 'computation', forbidden)
    assert ctx.tf.logs is None
    assert ('tf',) not in ctx._runtime.current_runs


def test_bound_success_follows_identity_changes(make_context):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.a = 901
    ctx.tf.pins.b = 1
    assert ctx.tf.run() == 902
    first_identity = ctx._runtime.current_runs[('tf',)].identity_checksum
    original = ctx.tf.logs
    assert isinstance(original, str) and 'Execution time:' in original
    assert ctx.tf.logs == original  # fresh views share one current run
    ctx.tf.pins.a = 902
    # compute the new run: its stored entry must replace the old identity.
    assert ctx.tf.run() == 903
    assert isinstance(ctx.tf.logs, str) and 'Execution time:' in ctx.tf.logs
    assert ctx._runtime.current_runs[('tf',)].identity_checksum != first_identity
    # A new required input settles without a run and hides the old log.
    ctx.tf.code = add_required
    ctx.compute(timeout=10)
    assert ctx.tf.logs is None


def test_bound_failure_returns_node_exception(make_context):
    ctx = make_context()
    ctx.tf = fail
    ctx.tf.pins.value = 904
    assert ctx.tf.compute() is None
    assert ctx.tf.state == 'failed'
    assert ctx.tf.logs == ctx.tf.exception
    assert 'bound-logs-failure-904' in ctx.tf.logs
    assert 'Execution time:' not in ctx.tf.logs


def test_bound_blocked_pin_has_no_node_log(make_context):
    ctx = make_context()
    ctx.source = Cell('float')
    ctx.source.set(None)
    ctx.tf = fail
    ctx.tf.celltypes.value = 'float'
    ctx.tf.pins.value = ctx.source
    ctx.compute(timeout=10)
    assert ctx.tf.state == 'blocked'
    assert ctx.tf.exception is None
    assert ctx.tf.logs is None
    assert ('tf',) not in ctx._runtime.current_runs
