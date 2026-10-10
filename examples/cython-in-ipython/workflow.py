"""Mounted Cython annotation demo, ported from legacy injection4.py."""

from pathlib import Path

import seamless
from seamless.workflow import Cell, Context


def run_ipython(ipy: str, i: int):
    """Compile the IPython cells, retain their HTML, and time func(i)."""
    from time import perf_counter

    from IPython.core.interactiveshell import InteractiveShell

    # A separate shell keeps edited definitions and output history isolated.
    # In this .ipy example, blank lines delimit the notebook code cells.
    shell = InteractiveShell(user_ns={})
    try:
        for block in ipy.split("\n\n"):
            if not block.strip():
                continue
            execution = shell.run_cell(block, store_history=False)
            execution.raise_error()
            if execution.result is not None:
                shell.user_ns["_"] = execution.result
        func = shell.user_ns["func"]
        html = shell.user_ns["func_html"]
        start = perf_counter()
        value = func(i)
        elapsed = perf_counter() - start
        return {"value": value, "html": html, "seconds": elapsed}
    finally:
        shell.reset(new_session=False)


def build(directory=None, i=100):
    """Return a live Context; mount paths are relative to directory."""
    directory = Path(directory or Path(__file__).parent).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "cell-ipython.ipy"
    if not source.exists():
        source.write_text(
            Path(__file__).with_name("cell-ipython-ORIGINAL.ipy").read_text()
        )
    ctx = Context()
    try:
        ctx.i = Cell("int")
        ctx.i.set(i)
        ctx.ipy = Cell("ipython")
        ctx.ipy.mount(source, mode="rw", authority="file")
        ctx.tf = run_ipython
        ctx.tf.pins.ipy = ctx.ipy
        ctx.tf.pins.i = ctx.i
        ctx.result = Cell("float")
        ctx.result = ctx.tf.result["value"].as_celltype("float")
        ctx.seconds = Cell("float")
        ctx.seconds = ctx.tf.result["seconds"].as_celltype("float")
        ctx.html = Cell("text")
        ctx.html = ctx.tf.result["html"].as_celltype("text")
        ctx.html.mount(directory / "cell-ipython.html", mode="w")
        return ctx
    except BaseException:
        ctx.close()
        raise


def main():
    ctx = build(i=6000)
    try:
        while True:
            ctx.mounts.sync(timeout=120)
            ctx.compute()
            ctx.mounts.sync(timeout=120)
            if ctx.tf.exception:
                print(ctx.tf.exception)
            else:
                print(f"result: {ctx.result.value}; func time: {ctx.seconds.value:.6f}s")
                print(f"HTML: {Path(__file__).with_name('cell-ipython.html')}")
            input("Edit cell-ipython.ipy, then press Enter to synchronize (Ctrl-C to stop): ")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        ctx.close()
        seamless.close()


if __name__ == "__main__":
    main()
