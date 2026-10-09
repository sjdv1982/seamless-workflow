"""State graph contract: transport, handle parity, pacing and build budget."""
import hashlib
import json
import queue
import time

import pytest
import requests

from seamless import Cell
from seamless_workflow.diagnostics import record_turns
from test_contract_shares import ctx, server, get, updates, fail_negative
from test_share_transport import Spec, Sink, server as transport_server, attach, deliver


def publication(marker=1, value='complete'):
    body = json.dumps({'nodes': [{'path': ['a'], 'state': value}],
                       'anonymous_nodes': {}, 'connections': []},
                      sort_keys=True, separators=(',', ':')).encode()
    return marker, hashlib.sha256(body).hexdigest(), body


def reserve_graph(server, provider):
    reg = server.reserve('ctx', Spec(), 'int', 's', Sink(), state_graph=provider)
    server.activate(reg).result(10)
    return server.url + '/ctx/state-graph'


def test_state_graph_http(transport_server):
    pub = publication()
    url = reserve_graph(transport_server, lambda timeout, fresh: pub)
    result = get(url)
    assert result.status_code == 200
    assert result.content == pub[2]
    assert result.headers['Content-Type'].startswith('application/json')
    assert result.headers['ETag'] == '"' + pub[1] + '"'
    assert result.headers['X-Seamless-Marker'] == '1'
    assert result.headers['Cache-Control'] == 'no-cache'
    head = requests.head(url, timeout=10)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['ETag'] == result.headers['ETag']
    cached = get(url, headers={'If-None-Match': result.headers['ETag']})
    assert cached.status_code == 304 and cached.content == b''
    assert requests.put(url, data=b'{}', timeout=10).status_code == 405


def test_state_graph_missing_provider(transport_server):
    attach(transport_server)
    assert get(transport_server.url + '/ctx/state-graph').status_code == 404


def test_state_graph_unavailable(transport_server):
    url = reserve_graph(transport_server, lambda timeout, fresh: None)
    assert get(url).status_code == 404


def test_state_graph_reserved_key(transport_server):
    from seamless_workflow.attachments.share.spec import ShareSpec
    with pytest.raises(ValueError):
        ShareSpec(path='state-graph')
    with pytest.raises(ValueError):
        attach(transport_server, Spec(path='state-graph'))
    reg, sink, url = attach(transport_server, ShareSpec(path='state-graph', toplevel=True))
    deliver(transport_server, reg, sink)
    assert get(url).status_code == 200


def test_state_graph_notifications_coalesce(transport_server):
    current = [publication()]
    reserve_graph(transport_server, lambda timeout, fresh: current[0])
    with updates(transport_server.url + '/ctx') as receive:
        receive('shares')
        assert receive('state-graph', timeout=1.2) == ['state-graph', [current[0][1], 1]]
        with pytest.raises(queue.Empty):
            receive('state-graph', timeout=0.65)
        # All intermediate states fit inside one ticker interval.
        for marker in range(2, 10):
            current[0] = publication(marker, str(marker))
        assert receive('state-graph', timeout=1.2) == ['state-graph', [current[0][1], 9]]
        with pytest.raises(queue.Empty):
            receive('state-graph', timeout=0.65)


def graph_url(ctx):
    ctx.a.share()
    return ctx.a.share.url.rsplit('/', 1)[0] + '/state-graph'


def test_state_graph_matches_handles_and_graph(ctx):
    ctx.a = Cell('int'); ctx.a.set(-1)
    ctx.tf = fail_negative
    ctx.tf.pins.x = ctx.a
    ctx.out = ctx.tf.result
    ctx.loose = Cell('plain')
    ctx.compute()
    payload = get(graph_url(ctx)).json()
    nodes = {tuple(node['path']): node for node in payload['nodes']}
    assert set(nodes) == {('a',), ('tf',), ('out',), ('loose',)}
    for path, node in nodes.items():
        handle = getattr(ctx, path[0])
        for field in ('state', 'block_reason', 'exception'):
            assert node[field] == getattr(handle, field), (path, field)
        assert 'checksum' in node
        assert 'code' not in node and 'generation' not in node
    assert nodes['tf',]['state'] == 'failed'
    assert nodes['out',]['state'] == 'blocked'
    graph = ctx.get_graph()
    assert payload['connections'] == graph['connections']
    assert payload['anonymous_nodes'] == graph.get('anonymous_nodes', {})
    response = get(ctx.a.share.url.rsplit('/', 1)[0] + '/state-graph')
    assert hashlib.sha256(response.content).hexdigest() == response.headers['ETag'].strip('"')


def test_state_graph_idle_context_has_no_turns(ctx):
    ctx.a = Cell('int'); ctx.a.set(1)
    url = graph_url(ctx)
    with updates(url.rsplit('/', 1)[0]) as receive:
        receive('state-graph', timeout=1.2)
        with record_turns(ctx) as turns:
            # Registering the recorder is itself a turn and dirties the graph.
            # Let its resulting refresh settle before measuring idle activity.
            time.sleep(0.65)
            baseline = turns.entries()
            time.sleep(2)
            assert get(url).status_code == 200
            assert turns.entries() == baseline


def test_state_graph_burst_builds_at_most_once_per_interval(ctx, monkeypatch):
    ctx.a = Cell('int'); ctx.a.set(0)
    url = graph_url(ctx)
    api = type(ctx)
    builds = []
    original = api._publish_state_graph
    def publish(self):
        builds.append(time.monotonic())
        return original(self)
    monkeypatch.setattr(api, '_publish_state_graph', publish)
    get(url)
    with updates(url.rsplit('/', 1)[0]) as receive:
        receive('state-graph', timeout=1.2)
        for value in range(100):
            ctx.a.set(value)
            get(url)
        time.sleep(0.6)
    assert all(b - a >= 0.49 for a, b in zip(builds, builds[1:])), builds


def test_state_graph_context_close(ctx):
    ctx.a = Cell('int'); ctx.a.set(1)
    url = graph_url(ctx)
    with updates(url.rsplit('/', 1)[0]) as receive:
        receive('state-graph', timeout=1.2)
        ctx.close()
        assert get(url).status_code == 404
        assert receive('closed') == ('closed', 1001)


def test_state_graph_300_node_publish_budget(ctx):
    ctx.a = Cell('int'); ctx.a.set(1)
    for index in range(299):
        setattr(ctx, 'node_' + str(index), ctx.a)
    ctx.compute()
    controller = ctx._controller
    started = time.perf_counter()
    pub = controller.submit('_state_graph_refresh', klass=4).result(10)
    elapsed = time.perf_counter() - started
    assert len(json.loads(pub[2])['nodes']) == 300
    print(f'300-node state graph publication: {elapsed * 1000:.3f} ms')
    assert elapsed < 0.050


def test_state_graph_provider_timeout(transport_server):
    transport_server.get_timeout = 0.05
    def slow(timeout, fresh):
        time.sleep(0.2)
        return publication()
    url = reserve_graph(transport_server, slow)
    assert get(url).status_code == 503


def test_state_graph_does_no_work_without_observer(transport_server):
    calls = []
    def provider(timeout, fresh):
        calls.append(fresh)
        return publication()
    reserve_graph(transport_server, provider)
    time.sleep(1.1)
    assert calls == []


def test_state_graph_digest_and_marker_change_only_with_payload(ctx):
    ctx.a = Cell('int'); ctx.a.set(1)
    first = ctx._controller.submit('_state_graph_refresh', klass=4).result(10)
    same = ctx._controller.submit('_state_graph_refresh', klass=4).result(10)
    assert same == first
    assert first[1] == hashlib.sha256(first[2]).hexdigest()
    payload = json.loads(first[2])
    assert first[2] == json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    ctx.a.set(2)
    changed = ctx._controller.submit('_state_graph_refresh', klass=4).result(10)
    assert changed[0] == first[0] + 1
    assert changed[1] != first[1]


def test_state_graph_provider_reuses_inflight_refresh_after_timeout():
    """An HTTP timeout must not enqueue another controller snapshot turn."""
    from concurrent.futures import Future
    from threading import Event
    from types import SimpleNamespace
    from seamless_workflow.attachments.share.api import ContextShares

    pending = Future()
    submitted = []
    def submit(operation, *, klass):
        submitted.append((operation, klass))
        return pending
    class FakeContext:
        _closing = False
        _closed_event = Event()
        _state_graph_published = None
        _state_graph_published_at = 0
        _controller = SimpleNamespace(state_graph_dirty=True, submit=submit)
    context = FakeContext()
    shares = ContextShares(context)
    for fresh in (False, False, True):
        with pytest.raises(TimeoutError):
            shares._state_graph_provider(0.005, fresh)
    assert submitted == [('_state_graph_refresh', 4)]
    pub = publication()
    context._state_graph_published = pub
    context._state_graph_published_at = time.monotonic()
    pending.set_result(pub)
    for fresh in (False, True, False):
        assert shares._state_graph_provider(0.05, fresh) == pub
    assert len(submitted) == 1
