"""Serve the mixed-language workflow and its live state graph.

This follows ``workflow-share.py`` and adds the node-state graph from
``status.html``. Integer cells are sliders and text cells are text fields.

Install ``seamless-workflow[share]`` and run ``python workflow-status.py`` from
this directory. ``build(page_path=...)`` is also available to launch the
workflow with a temporary page during browser checks. The share server uses
the configured port (5813 by default) and loopback when no host is configured.
"""

import os
from pathlib import Path

import seamless
from seamless import Cell
from seamless_workflow import Context, Transformer, shareserver


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
        def random_text(length:int, seed:int):
            import time
            import random
            time.sleep(5)
            random.seed(seed)
            alphabet = [chr(n) for n in range(ord('A'), ord('Z')+1)]
            letters = random.choices(alphabet, k=length)
            return "".join(letters)

        ctx.random_text = random_text
        ctx.random_text_code = Cell("python")
        ctx.random_text_code.set_buffer(ctx.random_text.code)
        ctx.random_text.code = ctx.random_text_code
        ctx.length = Cell("int")
        ctx.length.set(12)
        ctx.seed = Cell("int")
        ctx.seed.set(128)
        ctx.random_text.pins.length = ctx.length
        ctx.random_text.pins.seed = ctx.seed

        ctx.text1 = Cell("text")
        ctx.text1 = ctx.random_text.result

        bash_code = """
sleep 5
sed 's/C/Y/g' text1 > RESULT
"""
        ctx.modify_text_code = Cell("text")
        ctx.modify_text_code.set(bash_code)

        ctx.modify_text = Transformer("bash")
        ctx.modify_text.code = ctx.modify_text_code
        ctx.modify_text.pins.text1 = ctx.text1

        ctx.text2 = Cell("text")
        ctx.text2 = ctx.modify_text.result

        if page_path is None:
            page_path = Path(__file__).with_name("status.html")
        ctx.page = Cell(celltype="text")
        ctx.page.mount(page_path, mode="r")

        # Inputs have a value before they are shared: a writable share on a
        # cell without a value would install null.
        ctx.length.share(readonly=False)
        ctx.seed.share(readonly=False)
        ctx.random_text_code.share(readonly=False)
        ctx.modify_text_code.share(readonly=False)
        ctx.text1.share()
        ctx.text2.share()
        ctx.page.share(path="status.html", toplevel=True)
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
        print(f"Open the mixed-language status graph: {ctx.page.share.url}")
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
