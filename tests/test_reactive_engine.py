import time
from seamless_workflow import Context
from seamless_transformer import delayed


def add(x, y):
    return x + y


def same_one(x):
    return 42


def same_two(x):
    return 6 * 7


def changed(x):
    return 43


def slow_value(x):
    import time
    time.sleep(1.0)
    return x * 2


def other_value(x):
    import time
    time.sleep(1.0)
    return x * 3


def wait_started(ctx, path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        node = next(n for n in ctx.get_graph(runtime=True)['nodes'] if n['path'] == [path])
        if node['runtime']['run']['current'] and node['runtime']['run']['current']['identity']:
            return
        time.sleep(0.01)
    raise AssertionError('Submission did not construct')


def test_context_and_delayed_have_the_same_transformation_identity(make_context, transformation_observations):
    ctx = make_context()
    ctx.tf = add
    ctx.tf.pins.x = 12
    ctx.tf.pins.y = 30
    ctx.compute(timeout=10)
    standalone = delayed(add)(12, 30)
    standalone.compute()
    entry = ctx.get_graph(runtime=True)['nodes'][0]['runtime']['run']['current']
    assert entry['identity'] == standalone.transformation_checksum.hex()
    assert ctx.tf.result.value == standalone.value == 42
    entries = transformation_observations.entries()
    assert len(transformation_observations.misses()) == 1, entries
    assert transformation_observations.hits(), entries
    bound = ctx.tf()
    bound.compute()
    assert bound.transformation_checksum == standalone.transformation_checksum


def test_inconsequential_upstream_edit_relatches_inflight_run(make_context, transformation_observations):
    ctx = make_context()
    ctx.source = same_one
    ctx.source.pins.x = 1
    ctx.tail = slow_value
    ctx.tail.pins.x = ctx.source
    wait_started(ctx, 'tail')
    ctx.source.code = same_two
    ctx.compute(timeout=10)
    assert ctx.tail.result.value == 84
    assert len(transformation_observations.misses('tail')) == 1, transformation_observations.entries()


def test_consequential_edit_supersedes_and_late_result_is_not_applied(make_context, transformation_observations):
    ctx = make_context()
    ctx.source = same_one
    ctx.source.pins.x = 1
    ctx.tail = slow_value
    ctx.tail.pins.x = ctx.source
    wait_started(ctx, 'tail')
    ctx.source.code = changed
    ctx.compute(timeout=10)
    assert ctx.tail.result.value == 86
    assert len(transformation_observations.misses('tail')) == 2, transformation_observations.entries()


def test_self_edit_revert_joins_held_identity(make_context, transformation_observations):
    ctx = make_context()
    ctx.tf = slow_value
    ctx.tf.pins.x = 7
    wait_started(ctx, 'tf')
    first = ctx.get_graph(runtime=True)['nodes'][0]['runtime']['run']['current']['identity']
    ctx.tf.code = other_value
    ctx.tf.code = slow_value
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 14
    misses = [entry for entry in transformation_observations.misses('tf') if entry.checksum == first]
    assert len(misses) == 1, transformation_observations.entries()


def test_supersession_and_prune_have_no_asyncio_errors(make_context, capfd, caplog):
    import logging
    ctx = make_context()
    ctx.tf = slow_value
    ctx.tf.pins.x = 711
    wait_started(ctx, 'tf')
    ctx.tf.pins.x = 712
    ctx.prune()
    ctx.compute(timeout=10)
    assert ctx.tf.result.value == 1424
    ctx.close()  # drain cancellation callbacks before inspecting diagnostics
    assert [record for record in caplog.records
            if record.name == 'asyncio' and record.levelno >= logging.ERROR] == []
    assert capfd.readouterr().err == ''
