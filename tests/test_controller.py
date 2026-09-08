from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from seamless_workflow import Context
from seamless_workflow.errors import ReentrantContextError


def test_concurrent_ingress_has_one_total_order_and_owner():
    ctx = Context()
    try:
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda n: setattr(ctx, f'a{n}', n), range(40)))
        graph = ctx.get_graph()
        assert len(graph['nodes']) == 40
        trace = list(ctx._controller.trace)
        sequences = [r[0] for r in trace]
        assert sequences == sorted(set(sequences))
        assert {r[3] for r in trace} == {ctx._controller.ident}
        assert ctx._controller.ident != threading.get_ident()
    finally:
        ctx.close()


def test_configuration_is_snapshot_and_mapping_updates_are_validated():
    ctx = Context()
    try:
        from seamless_transformer import delayed
        ctx.tf = delayed('result = a', 'python')
        ctx.tf.celltypes.a = int
        assert ctx.tf.celltypes.a == 'int'
        with pytest.raises(TypeError):
            ctx.tf.celltypes.a = 'not-a-type'
        cfg = ctx.tf._workflow_backend.cfg
        cfg.language = 'bad'
        assert ctx.tf.language == 'python'
        ctx.tf.driver = True
        ctx.tf.allow_input_fingertip = True
        assert ctx.tf.driver and ctx.tf.allow_input_fingertip
        ctx.a = 12
        ctx.a.target_celltype = 'text'
        assert ctx.a.target_celltype == 'text'
        with pytest.raises(TypeError):
            ctx.a.target_celltype = 'invalid-celltype'
        assert ctx.a.target_celltype == 'text'
    finally:
        ctx.close()


def test_turns_do_not_materialize_and_checksum_write_preserves_identity():
    from seamless import Buffer, Checksum, diagnostics
    ctx = Context()
    try:
        ctx.a = {'x': 1}
        with diagnostics.record_materialisation() as log:
            ctx.unrelated = 12345
            ctx.b = ctx.a.x
            ctx.compute(timeout=5)
            assert ctx.b.value == 1
        assert ctx._controller.ident not in log.threads, log.ops
        absent = Checksum('a1' * 32)
        with diagnostics.record_materialisation() as log:
            ctx.a.set_checksum(absent)
        assert ctx.a.checksum == absent
        assert log.ops == []
    finally:
        ctx.close()


def test_optimistic_subpath_edit_retries_after_interleaved_write(monkeypatch):
    import seamless_workflow.context as module
    ctx = Context()
    try:
        ctx.a = {'x': 1, 'other': 1}
        assign = module._assign_path
        attempts = []
        def interleave(root, path, value):
            attempts.append(1)
            if len(attempts) == 1:
                ctx.a = {'x': 2, 'other': 99}
            return assign(root, path, value)
        monkeypatch.setattr(module, '_assign_path', interleave)
        ctx.a.x = 7
        assert len(attempts) == 2
        assert ctx.a.value == {'x': 7, 'other': 99}
    finally:
        ctx.close()


def test_timeout_withdraws_predicate_without_cancelling_work():
    ctx = Context()
    try:
        def identity(x):
            import time
            time.sleep(1)
            return x
        ctx.tf = identity
        ctx.tf.pins.x = 1
        for _ in range(3):
            with pytest.raises(TimeoutError):
                ctx.compute(timeout=0.001)
        assert not ctx._barriers
    finally:
        ctx.close()


def test_public_api_reentry_raises_without_mutating_graph(monkeypatch):
    ctx = Context()
    ctx.a = 1
    handle = ctx.a
    observed = []
    after_turn = Context._after_turn
    def check(self):
        if self is ctx and not observed:
            for operation in (lambda: setattr(ctx, 'a', 2), ctx.get_graph,
                              lambda: handle.value, ctx.compute):
                with pytest.raises(ReentrantContextError):
                    operation()
            observed.append(True)
        return after_turn(self)
    monkeypatch.setattr(Context, '_after_turn', check)
    assert ctx.a.value == 1
    assert observed
    ctx.close()


def test_async_cancellation_during_registration_withdraws_predicate(monkeypatch):
    import asyncio
    ctx = Context()
    original = ctx._controller.submit
    async def exercise():
        task = asyncio.current_task()
        def submit(operation, *args, **kwargs):
            reply = original(operation, *args, **kwargs)
            if operation == '_install_wait':
                task.cancel()
            return reply
        monkeypatch.setattr(ctx._controller, 'submit', submit)
        with pytest.raises(asyncio.CancelledError):
            await ctx.computation()
        await asyncio.sleep(.05)
    asyncio.run(exercise())
    assert not ctx._barriers
    ctx.close()


def test_async_subpath_compute_returns_projected_checksum():
    import asyncio
    ctx = Context()
    ctx.a = {'x': 7}
    checksum = asyncio.run(ctx.a.x.compute_async())
    assert checksum.resolve('mixed') == 7
    ctx.close()


def test_turn_diagnostics_are_bounded_immutable_and_scoped():
    from dataclasses import FrozenInstanceError
    from seamless_workflow.diagnostics import record_turns
    ctx = Context()
    with record_turns(ctx, limit=3) as outer:
        ctx.a = 1
        with record_turns(ctx) as inner:
            ctx.a = 2
        recorded = inner.entries()
        ctx.a = 3
        assert inner.entries() == recorded
    entries = outer.entries()
    assert len(entries) == 3
    assert [entry.sequence for entry in entries] == sorted(entry.sequence for entry in entries)
    with pytest.raises(FrozenInstanceError):
        entries[-1].nodes[0].state = 'corrupted'
    ctx.a = 4
    assert outer.entries() == entries
    ctx.close()
