"""node-state-lifecycle.md, *Speculation: launch immediately, delay cancellation*.

A run superseded because an upstream is recomputing (case a), and a completed
downstream's retained result (case c), are held until that upstream resolves,
under a five-minute backstop; only the self-edit hold (case b) is a fixed
window.  The upstream resolving to the same output reinstates the held run;
a different output cancels it.
"""

import time

from time import time as wall_time

from seamless_workflow.scheduler import Scheduler


def slow_same_one(x):
    import time
    time.sleep(1.5)
    return 42


def slow_same_two(x):
    import time
    time.sleep(1.5)
    return 6 * 7


def slow_changed(x):
    import time
    time.sleep(1.5)
    return 43


def slow_tail(x):
    import time
    time.sleep(3.0)
    return x * 2


def fast_tail(x):
    return x * 2


def wait_started(ctx, path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        node = next(n for n in ctx.get_graph(runtime=True)['nodes'] if n['path'] == [path])
        if node['runtime']['run']['current'] and node['runtime']['run']['current']['identity']:
            return
        time.sleep(0.01)
    raise AssertionError('Submission did not construct')


def _slow_graph(ctx):
    ctx.source = slow_same_one
    ctx.source.pins.x = 1
    ctx.tail = slow_tail
    ctx.tail.pins.x = ctx.source


def _live(ctx, path):
    return [
        record
        for record in ctx._runtime.superseded_runs.get(path, ())
        if record.phase != "cancelled"
    ]


def _upstream_records(ctx, path):
    return [record for record in _live(ctx, path) if record.hold_kind == "upstream"]


def _superseded_roles(ctx, checksum):
    return [
        role
        for cs, role in ctx._refheld_checksums()
        if cs == checksum and ":superseded:" in role
    ]


def test_the_upstream_backstop_is_five_minutes_and_its_own_knob():
    scheduler = Scheduler()
    assert scheduler.upstream_hold_max_seconds == 300
    before = wall_time()
    deadline = scheduler.hold_deadline("upstream")
    assert 299.5 <= deadline - before <= 301

    scheduler = Scheduler(self_edit_hold_seconds=99.0)
    before = wall_time()
    assert 299.5 <= scheduler.hold_deadline("upstream") - before <= 301

    scheduler = Scheduler(upstream_hold_max_seconds=77.0)
    before = wall_time()
    assert 76.5 <= scheduler.hold_deadline("upstream") - before <= 78
    assert 29.5 <= scheduler.hold_deadline("self-edit") - before <= 31


def test_inflight_run_survives_an_upstream_recompute_longer_than_the_self_edit_window(
    make_context, transformation_observations
):
    ctx = make_context()
    _slow_graph(ctx)
    ctx._runtime.scheduler.self_edit_hold_seconds = 0.2
    wait_started(ctx, 'tail')
    ctx.source.code = slow_same_two
    records = list(ctx._runtime.superseded_runs[("tail",)])
    assert len(records) == 1
    record = records[0]
    assert record.hold_kind == "upstream"
    assert 290 <= record.hold_deadline - wall_time() <= 301
    time.sleep(0.6)
    assert record.phase == "superseded"
    ctx.compute(timeout=30)
    assert ctx.tail.result.value == 84
    assert len(transformation_observations.misses('tail')) == 1, transformation_observations.entries()


def test_upstream_resolving_differently_cancels_the_held_run_at_the_event(
    make_context, transformation_observations
):
    ctx = make_context()
    _slow_graph(ctx)
    wait_started(ctx, 'tail')
    ctx.source.code = slow_changed
    assert len(_upstream_records(ctx, ("tail",))) == 1
    ctx.compute(timeout=30)
    assert ctx.tail.result.value == 86
    assert len(transformation_observations.misses('tail')) == 2, transformation_observations.entries()
    assert _upstream_records(ctx, ("tail",)) == []
    assert ctx.tail.prune() == {"cancelled": 0}


def test_completed_downstream_result_is_retained_until_the_upstream_event(
    make_context, transformation_observations
):
    ctx = make_context()
    ctx.source = slow_same_one
    ctx.source.pins.x = 1
    ctx.tail = fast_tail
    ctx.tail.pins.x = ctx.source
    ctx.compute(timeout=30)
    assert ctx.tail.result.value == 84
    ctx._runtime.scheduler.self_edit_hold_seconds = 0.2
    old = ctx._graph.nodes[("tail",)].current_checksum
    assert old is not None
    ctx.source.code = slow_same_two
    time.sleep(0.6)
    assert _superseded_roles(ctx, old)
    records = _live(ctx, ("tail",))
    assert [record.hold_kind for record in records] == ["upstream"]
    ctx.compute(timeout=30)
    assert ctx.tail.result.value == 84
    assert len(transformation_observations.misses('tail')) == 1, transformation_observations.entries()


def test_completed_downstream_cell_result_is_retained_until_the_upstream_event(make_context):
    ctx = make_context()
    ctx.source = slow_same_one
    ctx.source.pins.x = 1
    ctx.out = ctx.source.result
    ctx.compute(timeout=30)
    assert ctx.out.value == 42
    ctx._runtime.scheduler.self_edit_hold_seconds = 0.2
    old = ctx._graph.nodes[("out",)].current_checksum
    assert old is not None
    ctx.source.code = slow_same_two
    time.sleep(0.6)
    assert _superseded_roles(ctx, old)
    assert len(_upstream_records(ctx, ("out",))) == 1
    ctx.compute(timeout=30)
    assert ctx.out.value == 42
    assert _upstream_records(ctx, ("out",)) == []


def test_a_stuck_upstream_releases_the_hold_at_the_backstop(
    make_context, transformation_observations
):
    ctx = make_context()
    _slow_graph(ctx)
    ctx._runtime.scheduler.upstream_hold_max_seconds = 0.3
    wait_started(ctx, 'tail')
    ctx.source.code = slow_same_two
    assert len(_upstream_records(ctx, ("tail",))) == 1
    time.sleep(0.8)
    assert _live(ctx, ("tail",)) == []
    ctx.compute(timeout=30)
    assert ctx.tail.result.value == 84
    assert len(transformation_observations.misses('tail')) == 2, transformation_observations.entries()


def test_a_self_edit_keeps_the_fixed_window(make_context):
    ctx = make_context()
    ctx.tf = slow_tail
    ctx.tf.pins.x = 7
    ctx._runtime.scheduler.self_edit_hold_seconds = 50.0
    wait_started(ctx, 'tf')
    ctx.tf.pins.x = 8
    records = list(ctx._runtime.superseded_runs[("tf",)])
    assert len(records) == 1
    assert records[0].hold_kind == "self-edit"
    assert 49 <= records[0].hold_deadline - wall_time() <= 51
    ctx.compute(timeout=30)
    assert ctx.tf.result.value == 16
