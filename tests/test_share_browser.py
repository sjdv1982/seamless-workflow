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


@pytest.mark.skipif(CHROME is None, reason="Chrome is unavailable")
def test_mixlangstatus_state_graph_browser(tmp_path, monkeypatch):
    """Live states, error controls, in-place recolouring and endpoint identity."""
    monkeypatch.setenv("SEAMLESS_SHARE_HOST", "127.0.0.1")
    monkeypatch.setenv("SEAMLESS_SHARE_PORT", "0")
    from seamless_workflow import shareserver
    example_dir = EXAMPLE.parent / "mixlangstatus"
    spec = importlib.util.spec_from_file_location("mixlangstatus_status_example", example_dir / "workflow-status.py")
    example = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = example
    spec.loader.exec_module(example)
    html = tmp_path / "status.html"
    html.write_bytes((example_dir / "status.html").read_bytes())
    context = None
    try:
        with Chrome() as chrome:
            context = example.build(page_path=html)
            # A unique input prevents an earlier test's cached result from
            # hiding the five-second computing/waiting states.
            context.seed.set(time.time_ns() % 2_000_000_000)
            wait_python(lambda: context.random_text.state == "computing")
            tab = chrome.tab(context.page.share.url)
            tab.wait("document.querySelector('#status-graph g.node[data-path=\"random_text\"]')?.dataset.state === 'computing' && document.querySelector('#status-graph g.node[data-path=\"modify_text\"]')?.dataset.state === 'waiting'", timeout=4)
            tab.wait("document.querySelector('g.node[data-path=\"random_text\"]')?.dataset.state === 'complete' && document.querySelector('g.node[data-path=\"modify_text\"]')?.dataset.state === 'complete'", timeout=20)
            assert tab.evaluate("(() => {const edges=[...document.querySelectorAll('#status-graph g.edge')];return edges.length>0 && edges.every(edge => edge.dataset.state === [...document.querySelectorAll('#status-graph g.node')].find(node => node.dataset.path === edge.dataset.source)?.dataset.state);})()")
            tab.evaluate("window.savedStatusNode=document.querySelector('g.node[data-path=\"modify_text\"]'); window.savedStatusEdge=document.querySelector('g.edge[data-source=\"modify_text\"]');")
            # Exercise the actual writable text control, including its blur
            # submission, so the browser exposes a failure despite stale output.
            tab.evaluate("(() => {const code=document.querySelector('#modify_text_code');code.value='echo browser-status-failure >&2\\nexit 1\\n';code.dispatchEvent(new Event('input',{bubbles:true}));code.dispatchEvent(new Event('blur'));})()")
            wait_python(lambda: context.modify_text.state == "failed")
            tab.wait("document.querySelector('g.node[data-path=\"modify_text\"]')?.dataset.state === 'failed' && document.querySelector('g.node[data-path=\"text2\"]')?.dataset.state === 'blocked'")
            assert tab.evaluate("savedStatusNode === document.querySelector('g.node[data-path=\"modify_text\"]') && savedStatusEdge === document.querySelector('g.edge[data-source=\"modify_text\"]')")
            assert tab.evaluate("savedStatusEdge.dataset.state === savedStatusNode.dataset.state")
            tab.evaluate("savedStatusNode.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
            tab.wait("document.querySelector('#status-detail')?.textContent.includes('failed') && document.querySelector('#status-detail')?.textContent.includes('browser-status-failure')")

            # Drive the same renderer through its client handler with graph-format
            # endpoints: nested paths, a direct node, and an anonymous chain.
            assert tab.evaluate(r'''(() => {
              const render=window.ctx.self.onstategraph;
              window.ctx.self.onstategraph=null;
              const graph={nodes:[
                {type:'cell',path:['group','input'],celltype:'plain',state:'complete',block_reason:null,exception:null,checksum:null},
                {type:'transformer',path:['group','run'],language:'python',state:'waiting',block_reason:null,exception:null,checksum:null},
                {type:'cell',path:['result'],celltype:'plain',state:'waiting',block_reason:null,exception:null,checksum:null}],
                anonymous_nodes:{one:{source:{node:['group','input']},path:'[0]',celltype:'plain'},two:{source:{symbol:'one'},path:'[1]',celltype:'plain'}},
                connections:[{type:'connection',source:{symbol:'two'},target:['group','run','x']},{type:'connection',source:{node:['group','run']},target:['result']}]};
              render(graph);
              const nodes=[...document.querySelectorAll('#status-graph g.node')];
              const edges=[...document.querySelectorAll('#status-graph g.edge')];
              if(nodes.length!==3 || edges.length!==2) return false;
              if(!edges.some(e=>e.dataset.source==='group.input' && e.dataset.target==='group.run') || !edges.some(e=>e.dataset.source==='group.run' && e.dataset.target==='result')) return false;
              const input=nodes.find(n=>n.dataset.path==='group.input'),run=nodes.find(n=>n.dataset.path==='group.run'),result=nodes.find(n=>n.dataset.path==='result');
              if(!(input.getBoundingClientRect().left < run.getBoundingClientRect().left && run.getBoundingClientRect().left < result.getBoundingClientRect().left)) return false;
              graph.nodes[0].state='failed';render(graph);
              if(document.querySelector('g.node[data-path="group.input"]')!==input || input.dataset.state!=='failed') return false;
              if(!edges.every(e=>e.isConnected && e.dataset.state===nodes.find(n=>n.dataset.path===e.dataset.source).dataset.state)) return false;
              graph.nodes.push({type:'cell',path:['extra'],celltype:'plain',state:'unwired',block_reason:null,exception:null,checksum:null});
              render(graph);
              if(document.querySelectorAll('#status-graph g.node').length!==4) return false;
              // Dotted labels are ambiguous; graph identity remains the path array.
              const collision={nodes:[
                {type:'cell',path:['a.b'],celltype:'plain',state:'complete',block_reason:null,exception:null,checksum:null},
                {type:'cell',path:['a','b'],celltype:'plain',state:'failed',block_reason:null,exception:'nested failure',checksum:null},
                {type:'transformer',path:['target'],language:'python',state:'blocked',block_reason:null,exception:null,checksum:null}],
                anonymous_nodes:{},connections:[
                  {type:'connection',source:['a.b','item'],target:['target','first']},
                  {type:'connection',source:['a','b','item'],target:['target','second']}]};
              render(collision);
              const exactNodes=[...document.querySelectorAll('#status-graph g.node')];
              const exactEdges=[...document.querySelectorAll('#status-graph g.edge')];
              const flat=exactNodes.find(n=>n.dataset.pathJson===JSON.stringify(['a.b']));
              const nested=exactNodes.find(n=>n.dataset.pathJson===JSON.stringify(['a','b']));
              if(exactNodes.length!==3 || !flat || !nested || flat===nested || flat.dataset.path!=='a.b' || nested.dataset.path!=='a.b') return false;
              if(flat.dataset.state!=='complete' || nested.dataset.state!=='failed') return false;
              if(exactEdges.length!==2 || !exactEdges.some(e=>e.dataset.sourceJson===JSON.stringify(['a.b']) && e.dataset.targetJson===JSON.stringify(['target']) && e.dataset.state==='complete') || !exactEdges.some(e=>e.dataset.sourceJson===JSON.stringify(['a','b']) && e.dataset.targetJson===JSON.stringify(['target']) && e.dataset.state==='failed')) return false;
              collision.nodes[0].state='computing';render(collision);
              return flat.isConnected && nested.isConnected && flat.dataset.state==='computing' && nested.dataset.state==='failed' && exactEdges.every(e=>e.isConnected && e.dataset.state===exactNodes.find(n=>n.dataset.pathJson===e.dataset.sourceJson).dataset.state);
            })()''')
    finally:
        if context is not None:
            context.close()
        shareserver.close()


@pytest.mark.parametrize("failures,error_number,expected_calls", [(2, "ENOTEMPTY", 3), (25, "ENOTEMPTY", 21), (1, "EACCES", 1)])
def test_chrome_profile_cleanup_retries_only_transient_nonempty(monkeypatch, failures, error_number, expected_calls):
    """A child write race is retried, while persistent and other errors surface."""
    import errno
    from helpers import chrome_cdp
    # Use symbolic errno values so the fake behaves across operating systems.
    error_number = getattr(errno, error_number)
    calls = []
    sleeps = []
    failure = OSError(error_number, "instrumented profile cleanup failure")
    class Profile:
        def cleanup(self):
            calls.append(None)
            if len(calls) <= failures:
                raise failure
    chrome = Chrome.__new__(Chrome)
    chrome.profile = Profile()
    monkeypatch.setattr(chrome_cdp.time, "sleep", sleeps.append)
    if failures == 2:
        chrome._cleanup_profile()
    else:
        with pytest.raises(OSError) as caught:
            chrome._cleanup_profile()
        assert caught.value is failure
    assert len(calls) == expected_calls
    assert sleeps == [0.1] * (expected_calls - 1)
