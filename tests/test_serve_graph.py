import json
import subprocess
import sys

import pytest

import seamless_workflow.serve_graph as serve_graph


def test_main_requires_graph_file():
    with pytest.raises(SystemExit) as exc_info:
        serve_graph.main([])

    assert exc_info.value.code == 2


def test_main_loads_graph_and_closes_context(tmp_path, monkeypatch):
    graph = {"nodes": [], "connections": []}
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    seen = {"events": []}

    class FakeContext:
        def __init__(self):
            seen["events"].append("context-created")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            seen["context_closed"] = True

        def set_graph(self, value, *, mounts, shares):
            seen["graph"] = value
            seen["mounts"] = mounts
            seen["shares"] = shares

    monkeypatch.setattr(serve_graph, "Context", FakeContext)
    monkeypatch.setattr(serve_graph, "_wait_for_shutdown", lambda: None)

    def fake_config_init(*, workdir):
        seen["events"].append("config-init")
        seen["config_workdir"] = workdir

    monkeypatch.setattr(serve_graph.os, "getcwd", lambda: "/launch-directory")
    monkeypatch.setattr(
        serve_graph.seamless.config,
        "init",
        fake_config_init,
    )
    monkeypatch.setattr(
        serve_graph.seamless, "close", lambda: seen.update(closed=True)
    )

    serve_graph.main([str(graph_path)])

    assert seen == {
        "events": ["config-init", "context-created"],
        "config_workdir": "/launch-directory",
        "graph": graph,
        "mounts": False,
        "shares": True,
        "context_closed": True,
        "closed": True,
    }


def test_context_set_graph_loads_serialized_graph():
    script = """
import json
import os
import seamless
import seamless.config
from seamless_workflow import Context

ctx = None
try:
    seamless.config.init(workdir=os.getcwd())
    ctx = Context()
    graph = json.loads(json.dumps(ctx.get_graph()))
    ctx.set_graph(graph, mounts=False, shares=True)
    assert ctx.get_graph() == graph
finally:
    if ctx is not None:
        ctx.close()
    seamless.close()
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_graph_load_failure_closes_context_and_seamless(tmp_path, monkeypatch):
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text('{"nodes": [], "connections": []}', encoding="utf-8")
    events = []

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            events.append("context-closed")

        def set_graph(self, *_args, **_kwargs):
            raise ValueError("invalid graph")

    monkeypatch.setattr(serve_graph, "Context", FakeContext)
    monkeypatch.setattr(serve_graph.seamless.config, "init", lambda **_kwargs: None)
    monkeypatch.setattr(serve_graph.seamless, "close", lambda: events.append("seamless-closed"))
    monkeypatch.setattr(serve_graph, "_wait_for_shutdown", lambda: events.append("waited"))

    with pytest.raises(ValueError, match="invalid graph"):
        serve_graph.main([str(graph_path)])

    assert events == ["context-closed", "seamless-closed"]


def test_shutdown_wait_restores_signal_handlers(monkeypatch):
    installed = {}
    original = {
        serve_graph.signal.SIGINT: object(),
        serve_graph.signal.SIGTERM: object(),
    }

    class FakeEvent:
        was_set = False

        def set(self):
            self.was_set = True

        def wait(self):
            installed[serve_graph.signal.SIGTERM](serve_graph.signal.SIGTERM, None)

    event = FakeEvent()

    def fake_signal(signum, handler):
        old_handler = installed.get(signum, original[signum])
        installed[signum] = handler
        return old_handler

    monkeypatch.setattr(serve_graph.threading, "Event", lambda: event)
    monkeypatch.setattr(serve_graph.signal, "signal", fake_signal)

    serve_graph._wait_for_shutdown()

    assert event.was_set
    assert installed == original
