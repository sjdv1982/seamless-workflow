"""Plan step 9 acceptance: real Chrome, real one-port server, real slider page."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import time

import pytest

from helpers.chrome_cdp import CHROME, Chrome
from test_contract_shares import get


EXAMPLE = Path(__file__).resolve().parents[1] / "examples/share-sliders"


def wait_python(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError("Python workflow did not receive the browser value")


def page_values(a, b, c):
    return ("window.shareReady === true && document.querySelector('#a') && document.querySelector('#b') && document.querySelector('#c') && "
            f"Number(document.querySelector('#a').value)==={a} && "
            f"Number(document.querySelector('#b').value)==={b} && "
            f"Number(document.querySelector('#c').value || document.querySelector('#c').textContent)==={c}")


@pytest.mark.skipif(CHROME is None, reason="Chrome is unavailable")
def test_real_browser_slider_edits_python_edits_and_two_tab_coediting(tmp_path, monkeypatch):
    """Real web server acceptance: browser→cell, cell→browser and two co-editors."""
    monkeypatch.setenv("SEAMLESS_SHARE_HOST", "127.0.0.1")
    monkeypatch.setenv("SEAMLESS_SHARE_PORT", "0")
    from seamless_workflow import shareserver
    spec = importlib.util.spec_from_file_location("share_sliders_example", EXAMPLE / "serve.py")
    example = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = example
    spec.loader.exec_module(example)
    html = tmp_path / "index.html"
    html.write_bytes((EXAMPLE / "index.html").read_bytes())
    ctx = example.build(page_path=html)
    try:
        url = ctx.shares.url.rsplit("/", 1)[0]
        assert ctx.a.value == 10 and ctx.b.value == 20
        node = next(n for n in ctx.get_graph()["nodes"] if n["path"] == ["page"])
        assert node["mount"]["mode"] == "r"
        assert node["share"]["path"] == "index.html" and node["share"]["toplevel"] is True
        assert ctx.page.mount.spec is not None and ctx.page.share.spec is not None
        response = get(url + "/", allow_redirects=False)
        assert response.status_code == 302 and response.headers["Location"] == "/index.html"
        with Chrome() as chrome:
            first = chrome.tab(url + "/")
            first.wait(page_values(10, 20, 30))
            assert first.evaluate("document.querySelector('#a').disabled || document.querySelector('#b').disabled") is False
            assert first.evaluate("location.pathname") == "/index.html"
            first.evaluate("document.querySelector('#a').focus()")
            for _ in range(3):
                first.key("ArrowLeft", "ArrowLeft", 37)
            wait_python(lambda: ctx.a.value == 7)
            first.wait(page_values(7, 20, 27))
            assert get(ctx.c.share.url).json() == 27
            first.evaluate("document.querySelector('#b').focus()")
            ctx.b.set(8)
            first.wait(page_values(7, 8, 15))
            assert first.evaluate("document.activeElement.id") == "b"
            second = chrome.tab(url + "/")
            second.wait(page_values(7, 8, 15))
            first.evaluate("document.querySelector('#a').focus()")
            second.evaluate("document.querySelector('#a').value='6'; document.querySelector('#a').dispatchEvent(new Event('input',{bubbles:true}));")
            wait_python(lambda: ctx.a.value == 6)
            first.wait(page_values(6, 8, 14))
            assert first.evaluate("document.activeElement.id") == "a"
            second.wait(page_values(6, 8, 14))
            assert ctx.c.value == 14
            # The mounted HTML file stays attached while the share is live.
            html.write_text(html.read_text() + "\n<!-- live-mounted-page -->\n")
            wait_python(lambda: b"live-mounted-page" in get(ctx.page.share.url).content)
            assert ctx.page.mount.error is None and ctx.page.share.error is None
    finally:
        ctx.close()
        shareserver.close()


def test_example_blocks_in_input_and_never_calls_compute():
    """Server; Threads and blocking: example leaves the caller blocked while serving."""
    tree = ast.parse((EXAMPLE / "serve.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(isinstance(c.func, ast.Name) and c.func.id == "input" for c in calls)
    assert not any(isinstance(c.func, ast.Attribute) and c.func.attr == "compute" for c in calls)
    html = (EXAMPLE / "index.html").read_text()
    assert '/seamless-client.js' in html
    assert "connect_seamless" in html
    assert "compute(" not in html
    assert 'min="-10"' in html and 'max="30"' in html


def test_notebook_share_section_is_present_when_notebook_exists():
    """Real web server example: notebook exposes the same cells and top-level page."""
    notebook = EXAMPLE.parent / "basic-example.ipynb"
    if not notebook.exists():
        return
    cells = json.loads(notebook.read_text())["cells"]
    section = next(i for i, cell in enumerate(cells)
                   if cell["cell_type"] == "markdown" and "Share cells over HTTP" in "".join(cell["source"]))
    snippets = ["".join(c["source"]) for c in cells[section+1:] if c["cell_type"] == "code"]
    source = "\n".join(snippets)
    for name in ("a", "b", "c"):
        assert f"ctx.{name}.share(" in source
    assert "toplevel=True" in source
    assert "share-sliders/index.html" in source


def test_notebook_mounted_page_resolves_from_any_working_directory(tmp_path):
    """Example: notebook HTML path resolves independently of the current directory."""
    notebook = EXAMPLE.parent / "basic-example.ipynb"
    if not notebook.exists():
        return
    cells = json.loads(notebook.read_text())["cells"]
    section = next(i for i, cell in enumerate(cells)
                   if cell["cell_type"] == "markdown" and "Share cells over HTTP" in "".join(cell["source"]))
    source = "\n".join("".join(c["source"]) for c in cells[section+1:] if c["cell_type"] == "code")
    tree = ast.parse(source)
    mount = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "mount")
    setup = []
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            setup.append(statement)
        elif isinstance(statement, ast.Assign) and all(isinstance(t, ast.Name) for t in statement.targets):
            if not any(isinstance(n, ast.Name) and n.id == "ctx" for n in ast.walk(statement.value)):
                setup.append(statement)
    script = ast.unparse(ast.Module(body=setup, type_ignores=[]))
    script += "\nfrom pathlib import Path\npage = Path(" + ast.unparse(mount.args[0]) + ").resolve()\n"
    script += "assert page.is_file(), str(page)\nassert page == Path(" + repr(str(EXAMPLE / "index.html")) + ")\n"
    for cwd in (EXAMPLE.parents[1], EXAMPLE.parent, EXAMPLE.parents[2], tmp_path):
        result = subprocess.run([sys.executable, "-c", script], cwd=cwd,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, f"cwd={cwd}\n{result.stdout}{result.stderr}"


def test_chrome_cleanup_runs_even_when_disconnect_raises():
    """Browser test helper: disconnect failure still releases loop, process and profile."""
    events = []
    class Process:
        def poll(self): return None
        def terminate(self): events.append("terminate")
        def wait(self, timeout): events.append("wait")
    class Loop:
        def stop(self): pass
        def call_soon_threadsafe(self, callback): events.append("stop")
        def close(self): events.append("loop.close")
    class Thread:
        def is_alive(self): return True
        def join(self, timeout): events.append("join")
    class Log:
        def close(self): events.append("log.close")
    class Profile:
        def cleanup(self): events.append("profile.cleanup")
    chrome = Chrome.__new__(Chrome)
    chrome.process, chrome.loop, chrome.thread = Process(), Loop(), Thread()
    chrome.log, chrome.profile = Log(), Profile()
    def disconnect_failure(coroutine):
        coroutine.close()
        raise RuntimeError("instrumented disconnect failure")
    chrome._await = disconnect_failure
    with pytest.raises(RuntimeError, match="instrumented disconnect failure"):
        chrome.close()
    assert events == ["stop", "join", "loop.close", "terminate", "wait", "log.close", "profile.cleanup"]
