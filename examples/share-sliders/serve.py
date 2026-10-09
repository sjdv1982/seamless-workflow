"""Serve the browser slider example with a local Seamless share server.

Install ``seamless-workflow[share]`` and run ``python serve.py`` from this
directory. It uses the configured share server, which defaults to port 5813,
and uses loopback when no host was configured explicitly.
"""

import os
from pathlib import Path

import seamless
from seamless import Cell
from seamless_workflow import Context, shareserver


def build(page_path=None):
    """Build and return the live Context used by the browser page."""
    if shareserver.url is None:
        configured_host = shareserver.host
        host = os.environ.get("SEAMLESS_SHARE_HOST")
        if host is None:
            host = "127.0.0.1" if configured_host == "0.0.0.0" else configured_host
        port = int(os.environ.get("SEAMLESS_SHARE_PORT", shareserver.port))
        shareserver.configure(host=host, port=port)
    ctx = Context()
    try:
        def add(a, b):
            return a + b

        ctx.a = Cell(celltype="plain")
        ctx.a.set(10)
        ctx.b = Cell(celltype="plain")
        ctx.b.set(20)
        ctx.add = add
        ctx.add.pins.a = ctx.a
        ctx.add.pins.b = ctx.b
        ctx.c = Cell(celltype="plain")
        ctx.c = ctx.add.result

        if page_path is None:
            page_path = Path(__file__).with_name("index.html")
        ctx.page = Cell(celltype="text")
        ctx.page.mount(page_path, mode="r")

        ctx.a.share(readonly=False)
        ctx.b.share(readonly=False)
        ctx.c.share()
        ctx.page.share(path="index.html", toplevel=True)
        return ctx
    except Exception:
        try:
            ctx.close()
        finally:
            seamless.close()
        raise


def main():
    ctx = build()
    try:
        print(f"Open the slider page: {ctx.page.share.url}")
        input("Press Enter to stop the server... ")
    except EOFError:
        pass
    finally:
        try:
            ctx.close()
        finally:
            seamless.close()


if __name__ == "__main__":
    main()
