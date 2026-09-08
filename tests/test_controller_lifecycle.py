import asyncio
import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
import pytest
from seamless import Buffer
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims
from seamless_workflow import Context
from seamless_workflow.errors import ClosedContextError


def sleeping(x):
    import time
    time.sleep(2)
    return x


def test_close_fails_waiters_joins_threads_and_rejects_new_ingress():
    ctx = Context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 123
    controller,side = ctx._controller,ctx._side
    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(ctx.compute)
        ctx.close()
        with pytest.raises(ClosedContextError): waiting.result(timeout=3)
    assert not controller.thread.is_alive()
    assert not side.thread.is_alive()
    assert ctx._refheld_checksums() == ()
    with pytest.raises(ClosedContextError): ctx.a = 1
    with pytest.raises(ClosedContextError): ctx.get_graph()
    ctx.close()


def test_concurrent_close_is_idempotent():
    ctx = Context()
    ctx.a = {'payload':'unique lifecycle payload'}
    checksum = ctx.a.checksum
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: ctx.close(), range(8)))
    assert get_buffer_cache().reference_snapshot().get(checksum,(0,))[0] == 0


def test_repeated_context_construction_and_gc_leave_no_controller_threads():
    threads=[]
    for _ in range(20):
        ctx=Context()
        ctx.a=1
        ctx.a=2  # includes a superseded record
        threads.extend((ctx._controller.thread,ctx._side.thread))
        ref=weakref.ref(ctx)
        del ctx
        gc.collect()
        assert ref() is None
    deadline=time.monotonic()+3
    while any(t.is_alive() for t in threads) and time.monotonic()<deadline:
        time.sleep(.01)
    assert not any(t.is_alive() for t in threads)


def test_close_cancels_a_stalled_adapter(monkeypatch):
    from seamless_transformer.transformation_class import Transformation
    entered=threading.Event()
    async def stalled(self, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(Transformation,'computation',stalled)
    ctx=Context()
    ctx.tf=sleeping
    ctx.tf.pins.x=1
    assert entered.wait(3)
    start=time.monotonic()
    ctx.close()
    assert time.monotonic()-start < 2
    assert not ctx._side.thread.is_alive()


def test_process_shutdown_registry_closes_live_contexts(tmp_path):
    import subprocess,sys
    script=tmp_path/'shutdown.py'
    script.write_text('''from seamless_workflow import Context
import seamless
ctx = Context()
ctx.a = 3
seamless.close()
assert not ctx._controller.thread.is_alive()
assert not ctx._side.thread.is_alive()
assert ctx._refheld_checksums() == ()
''')
    result=subprocess.run([sys.executable,str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0,result.stdout+result.stderr


def test_internal_notification_failure_fails_waiters_and_poison_is_visible(monkeypatch):
    from seamless_workflow.errors import ControllerFailedError
    ctx = Context()
    ctx.tf = sleeping
    ctx.tf.pins.x = 98765
    waiter = ctx._controller.call('_install_wait', barrier=True, klass=1)
    def fail(self):
        raise AssertionError('injected continuation failure')
    monkeypatch.setattr(Context, '_injected_failure', fail, raising=False)
    notification = ctx._controller.submit('_injected_failure', klass=5)
    with pytest.raises(AssertionError): notification.result(timeout=3)
    with pytest.raises(ControllerFailedError): waiter.result(timeout=3)
    with pytest.raises(ControllerFailedError): ctx.compute()
    ctx.close()
    assert not ctx._controller.thread.is_alive()
    assert not ctx._side.thread.is_alive()


def test_escaped_cell_and_transformer_snapshots_survive_close():
    from seamless import Cell
    from seamless_transformer.transformer_class import Transformer
    from helpers.reference_lifecycle import force_expiry
    ctx = Context()
    ctx.a = {'token': 'escaped ownership'}
    captured = Cell()
    captured.set(ctx.a)
    ctx.tf = sleeping
    ctx.tf.pins.x = ctx.a
    snapshot = ctx.tf._snapshot_for_call()
    checksum = ctx.a.checksum
    ctx.close()
    force_expiry(checksum)
    assert captured.run() == {'token': 'escaped ownership'}
    tf = Transformer.__new__(Transformer)._build_from_snapshot(snapshot)
    assert tf.run() == {'token': 'escaped ownership'}
