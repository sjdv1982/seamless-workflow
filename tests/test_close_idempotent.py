"""Patch 04: a second Context.close() and a late, atexit-style seamless close must not
produce "Event loop is closed" or "cannot schedule new futures"."""

import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import concurrent.futures.thread as cft
    from seamless import Cell
    from seamless_transformer.worker import spawn
    from seamless_workflow import Context

    def double(x):
        return x * 2

    spawn(1)
    ctx = Context()
    ctx.x = Cell(celltype="plain"); ctx.x.set(21)
    ctx.tf = double
    ctx.tf.pins.x = ctx.x
    ctx.tf.celltypes.x = "plain"; ctx.tf.celltypes.result = "plain"
    ctx.y = ctx.tf.result
    ctx.compute(timeout=60)
    assert ctx.y.value == 42
    ctx.close()
    ctx.close()  # second close: must be a quiet no-op
    # late, atexit-style close: the executor machinery is already shut down
    cft._shutdown = True
    from seamless.shutdown import close
    close(from_atexit=True)
    """
)


def test_second_close_and_late_atexit_close_are_quiet():
    r = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Event loop is closed" not in out, out
    assert "cannot schedule new futures" not in out, out
