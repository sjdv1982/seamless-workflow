"""Context formation and requests for content-addressed celljoins (§8/§9)."""
from copy import deepcopy
from threading import Event

import pytest
from seamless import Buffer, Cell, Checksum
from seamless.checksum import celljoin as join_mod
from seamless.checksum import expression as expression_mod


def _wire(ctx, order=('a', 'b')):
    ctx.a = Cell('plain'); ctx.a.set(17)
    ctx.b = Cell('plain'); ctx.b.set(23)
    ctx.join = Cell('plain')
    for name in order:
        ctx.join[name] = getattr(ctx, name)


def test_fact_identity_process_cache_and_reordered_context_hit(make_context, monkeypatch):
    first = make_context(); _wire(first); first.compute(timeout=10)
    spec = join_mod.parse_celljoin(join_mod.build_celljoin(None, {'a': first.a.checksum, 'b': first.b.checksum}), 'plain')
    key = join_mod.celljoin_cache_key(spec.checksum, 'plain')
    assert expression_mod.get_expression_cache()[key] == first.join.checksum
    assert ('celljoin', spec.checksum.hex(), 'plain', False) in first._facts
    def forbidden(*args, **kwargs):
        raise AssertionError('second Context must use process cache')
    monkeypatch.setattr(join_mod, 'evaluate_celljoin', forbidden)
    second = make_context(); _wire(second, ('b', 'a')); second.compute(timeout=10)
    assert second.join.checksum == first.join.checksum
    assert second.join.state == 'complete'


def test_failure_is_not_cached_clear_exception_retries(make_context, monkeypatch):
    original = join_mod.evaluate_celljoin
    calls = []
    def flaky(spec, get_buffer):
        # Wiring edges separately may evaluate a transient one-member join.
        if len(spec.members) == 2:
            calls.append(spec.checksum)
            if len(calls) == 1:
                raise TypeError('deliberate join failure')
        return original(spec, get_buffer)
    monkeypatch.setattr(join_mod, 'evaluate_celljoin', flaky)
    ctx = make_context()
    ctx.a = Cell('plain'); ctx.a.set(17001)
    ctx.b = Cell('plain'); ctx.b.set(23001)
    ctx.join = Cell('plain'); ctx.join['a'] = ctx.a; ctx.join['b'] = ctx.b
    ctx.compute(timeout=10)
    assert ctx.join.state == 'failed'
    assert 'deliberate join failure' in str(ctx.join.exception)
    failed_key = join_mod.celljoin_cache_key(calls[0], 'plain')
    assert failed_key not in expression_mod.get_expression_cache()
    ctx.join.clear_exception(); ctx.compute(timeout=10)
    assert ctx.join.state == 'complete'
    assert ctx.join.value == {'a': 17001, 'b': 23001}
    assert len(calls) == 2


@pytest.mark.parametrize('celltype,member_type', [('deepcell', 'mixed'), ('deepfolder', 'bytes'), ('folder', 'bytes')])
def test_rootless_deep_join_needs_no_member_buffers(make_context, monkeypatch, celltype, member_type):
    absent = Checksum('c9' * 32)
    ctx = make_context(); ctx.member = Cell(member_type, checksum=absent)
    ctx.join = Cell(celltype)
    original = join_mod.evaluate_celljoin
    seen = []
    def inspect(spec, get_buffer):
        seen.append(spec.celltype)
        def forbidden(checksum):
            raise AssertionError("rootless deep join requested a buffer")
        return original(spec, forbidden)
    monkeypatch.setattr(join_mod, 'evaluate_celljoin', inspect)
    ctx.join['item'] = ctx.member; ctx.compute(timeout=10)
    assert ctx.join.state == 'complete'
    assert ctx.join.checksum == Buffer({'item': absent.hex()}, 'plain').get_checksum()
    assert seen == ['deepfolder' if celltype == 'folder' else celltype]


@pytest.mark.parametrize('key,error', [('<root>', ValueError), ('<numeric>', ValueError), (-1, ValueError), (1.5, TypeError), (None, TypeError), (True, TypeError)])
def test_forbidden_key_assignment_preserves_graph(make_context, key, error):
    ctx = make_context(); ctx.source = 1; ctx.join = Cell('plain')
    before = deepcopy(ctx.get_graph())
    with pytest.raises(error):
        ctx.join[key] = ctx.source
    assert ctx.get_graph() == before


@pytest.mark.parametrize('key', ['<root>', '<numeric>', -1, 1.5, None, True])
def test_forbidden_key_loaded_graph_is_miswired(make_context, key):
    ctx = make_context(); ctx.source = 1; ctx.join = Cell('plain')
    ctx.join['valid'] = ctx.source; ctx.compute(timeout=10)
    graph = deepcopy(ctx.get_graph())
    graph['connections'][0]['target'] = ['join', key]
    restored = make_context(); restored.set_graph(graph); restored.compute(timeout=10)
    assert restored.join.state == 'miswired'
    assert restored.join.checksum is None
    assert restored.join.exception is None


def test_mixed_key_kinds_fail_at_formation(make_context):
    ctx = make_context(); ctx.source = 1; ctx.join = Cell('plain')
    ctx.join.set([0, 0]); ctx.join[0] = ctx.source; ctx.join['a'] = ctx.source
    ctx.compute(timeout=10)
    assert ctx.join.state == 'failed'
    assert ctx.join.checksum is None
    assert not any(k[0] == 'celljoin' for k in ctx._facts)


def test_missing_member_is_final_without_fingertip(make_context, monkeypatch):
    absent = Checksum('d7' * 32)
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary join cannot fingertip')
    monkeypatch.setattr(Checksum, 'fingertip', forbidden)
    ctx = make_context(); ctx.source = Cell('plain', checksum=absent)
    ctx.join = Cell('plain'); ctx.join['a'] = ctx.source; ctx.compute(timeout=10)
    assert ctx.join.state == 'failed'
    assert absent.hex() in str(ctx.join.exception)


@pytest.mark.parametrize('scratch', [False, True])
@pytest.mark.parametrize('kind', ['root', 'member', 'explicit'])
def test_input_conversions_are_scratch_materializing_requests(make_context, monkeypatch, scratch, kind):
    import seamless_workflow.context as context_mod
    original = context_mod.evaluate_expression_placed
    requests = []
    async def inspect(checksum, path, input_type, output_type, **kwargs):
        requests.append((path, input_type, output_type, kwargs.get('scratch'), kwargs.get('materialize')))
        return await original(checksum, path, input_type, output_type, **kwargs)
    monkeypatch.setattr(context_mod, 'evaluate_expression_placed', inspect)
    ctx = make_context(); ctx.source = Cell('text'); ctx.source.set('7')
    if kind == 'root':
        ctx.join = Cell('text'); ctx.join.set('{"kept": 1}')
        ctx.join.celltype = 'plain'; ctx.join.scratch = scratch
        # Complete the ordinary, non-join retype before observing join requests.
        ctx.compute(timeout=10)
        requests.clear()
        ctx.member = Cell('plain'); ctx.member.set(7); ctx.join['a'] = ctx.member
    else:
        ctx.join = Cell('plain'); ctx.join.scratch = scratch
        ctx.join['a'] = ctx.source.as_celltype('mixed') if kind == 'explicit' else ctx.source
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete'
    expected = {'kept': 1, 'a': 7} if kind == 'root' else {'a': '7' if kind == 'explicit' else 7}
    assert ctx.join.value == expected
    relevant = [r for r in requests if r[1] != r[2]]
    assert relevant
    assert all(r[3:] == (True, True) for r in relevant)


@pytest.mark.parametrize('scratch', [False, True])
def test_projected_member_last_link_is_non_scratch_value_request(make_context, monkeypatch, scratch):
    import seamless_workflow.context as context_mod
    original = context_mod.evaluate_expression_placed
    requests = []
    async def inspect(checksum, path, input_type, output_type, **kwargs):
        if path:
            requests.append((kwargs.get('scratch'), kwargs.get('materialize')))
        return await original(checksum, path, input_type, output_type, **kwargs)
    monkeypatch.setattr(context_mod, 'evaluate_expression_placed', inspect)
    ctx = make_context(); ctx.source = Cell('plain'); ctx.source.set([4, 9])
    ctx.join = Cell('plain'); ctx.join.scratch = scratch; ctx.join['a'] = ctx.source[1]
    ctx.compute(timeout=10)
    assert ctx.join.value == {'a': 9}
    assert requests and all(r == (False, True) for r in requests)


def test_graph_unchanged_and_build_is_snapshot(make_context):
    ctx = make_context(); _wire(ctx); before = deepcopy(ctx.get_graph())
    ctx.compute(timeout=10)
    assert ctx.get_graph() == before
    snapshot = ctx.join.build(); checksum = ctx.join.checksum
    assert snapshot.input_checksum == checksum
    ctx.a.set(99); ctx.compute(timeout=10)
    assert snapshot.run() == {'a': 17, 'b': 23}
    assert ctx.join.value == {'a': 99, 'b': 23}


def test_supersession_softcancels_membership_and_replaces_job(make_context, monkeypatch):
    import seamless_workflow.context as context_mod
    original = join_mod.evaluate_celljoin
    softcancel = context_mod.softcancel_expression
    entered, release, cancelled = Event(), Event(), Event()
    calls = []
    def gate(spec, get_buffer):
        calls.append(spec)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return original(spec, get_buffer)
    def observe(key, member):
        if key[0] == 'celljoin':
            cancelled.set()
        return softcancel(key, member)
    monkeypatch.setattr(join_mod, 'evaluate_celljoin', gate)
    monkeypatch.setattr(context_mod, 'softcancel_expression', observe)
    ctx = make_context()
    ctx.source = Cell('plain'); ctx.source.set(10)
    ctx.join = Cell('plain')
    try:
        ctx.join['a'] = ctx.source
        assert entered.wait(10)
        assert ctx.join.state == 'waiting'
        old_jobs = set(ctx._jobs)
        ctx.source.set(11)
        assert cancelled.wait(10)
        assert not old_jobs.intersection(ctx._jobs)
    finally:
        release.set()
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete'
    assert ctx.join.value == {'a': 11}


@pytest.mark.parametrize('celltype', ['deepcell', 'deepfolder'])
@pytest.mark.parametrize('scratch', [False, True])
def test_projected_deep_member_keeps_checksum_request_with_absent_child(
    make_context, monkeypatch, celltype, scratch,
):
    """Deep member edges copy a checksum without requesting the child's value."""
    from seamless_workflow import Context
    absent = Checksum('e3' * 32)
    index = Buffer({'child': absent.hex()}, 'plain')
    index.tempref()
    step = Context._evaluate_deep_step
    requests = []
    def observe(self, *args, **kwargs):
        requests.append((kwargs['scratch'], kwargs['materialize']))
        return step(self, *args, **kwargs)
    monkeypatch.setattr(Context, '_evaluate_deep_step', observe)
    resolution = Checksum.resolution
    async def forbid_child_resolution(self, *args, **kwargs):
        assert self != absent, 'deep member edge must never fetch child bytes'
        return await resolution(self, *args, **kwargs)
    monkeypatch.setattr(Checksum, 'resolution', forbid_child_resolution)
    ctx = make_context()
    ctx.source = Cell(celltype, checksum=index.get_checksum())
    ctx.join = Cell(celltype); ctx.join.scratch = scratch
    ctx.join['copied'] = ctx.source['child']
    ctx.compute(timeout=10)
    assert ctx.join.state == 'complete'
    assert ctx.join.checksum == Buffer({'copied': absent.hex()}, 'plain').get_checksum()
    assert requests and all(request == (scratch, False) for request in requests)
