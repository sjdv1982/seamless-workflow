"""Contract tests for file mounts (feature 11).

Oracle: seamless/docs/agent/contracts/mounts.md. Each test names the section it
pins. Only rules not already pinned by test_mounts.py, test_mount_null.py,
test_mount_transport.py and test_cell_semantics.py are here.

Settled: ``Cell.exception`` is a string (register §2 item 5, implemented);
``ctx.a.mount.error`` and ``status['sense_error']`` stay exception objects.
"""
import gzip
import os
import time

import pytest

from seamless import Buffer, Cell, Checksum
from seamless.checksum.null import NULL_CHECKSUM
from seamless_workflow import Context
from seamless_workflow.errors import AuthorityError, NodeError, PathError
from seamless_workflow.attachments import AttachmentSpec, MountError
from seamless_workflow.attachments.policy import (ABSENT, INVALID, classify_observation,
                                                  decide_initial, detector)
from seamless_workflow.attachments.manual import ManualDriver
from seamless_workflow.diagnostics import record_attachments


NULL_HEX = Checksum(NULL_CHECKSUM).hex()


def _leaf(content):
    buf = Buffer(content)
    buf.tempref()
    return buf.get_checksum().hex()


def _assert_sense_error(cell):
    """A sense error: failed cell, string exception, MountError object on the mount."""
    assert cell.state == 'failed'
    assert isinstance(cell.exception, str) and cell.exception
    assert isinstance(cell.mount.status['sense_error'], MountError)


# --- The API ---------------------------------------------------------------

def test_mount_is_a_class_property_and_value_key_mount_is_reachable_by_item(tmp_path):
    # mounts.md *The API*: `mount` is a class attribute of Cell (property with
    # deleter); a value key named `mount` is reachable only as ctx.a["mount"].
    assert isinstance(Cell.__dict__.get('mount') or getattr(type(Cell()), 'mount'), property)
    with Context() as c:
        c.a = {'mount': 5, 'other': 1}
        assert c.a['mount'].value == 5
        assert c.a.mount.spec is None


def test_mounts_is_reserved_on_context_and_in_graph():
    # mounts.md *The API*: exact AttributeError message; a graph node at
    # ("mounts",) is refused with PathError.
    with Context() as c:
        with pytest.raises(AttributeError, match='mounts is reserved for the Context mount API'):
            c.mounts = 4
        c.x = Cell(celltype='int'); c.x.set(1)
        graph = c.get_graph()
        graph['nodes'][0]['path'] = ['mounts']
        with pytest.raises(PathError, match='mounts is a reserved Context API name'):
            c.set_graph(graph)


def test_mount_returns_none(tmp_path):
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x')
        assert c.a.mount(tmp_path / 'a.txt') is None


@pytest.mark.parametrize('path', ['', 'a\0b'])
def test_path_must_be_nonempty_text(tmp_path, path):
    # mounts.md *The API*, path normalization.
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x')
        with pytest.raises(ValueError, match='mount path must be a nonempty text path'):
            c.a.mount(path)
        assert c.a.mount.spec is None


def test_relative_path_stored_as_given_resolved_against_cwd_at_attach(tmp_path, monkeypatch):
    # "The spec stores the path as given ... The registry resolves it against the
    # current working directory at attach time."
    monkeypatch.chdir(tmp_path)
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('value')
        c.a.mount('rel.txt', mode='w')
        assert c.a.mount.spec.path == 'rel.txt'
        c.mounts.sync(timeout=5)
        other = tmp_path / 'elsewhere'; other.mkdir(); monkeypatch.chdir(other)
        c.a = 'changed'; c.mounts.sync(timeout=5)
        assert (tmp_path / 'rel.txt').read_text() == 'changed\n'
        assert not (other / 'rel.txt').exists()
        assert c.get_graph()['nodes'][0]['mount']['path'] == 'rel.txt'


def test_status_keys_and_values(tmp_path):
    # mounts.md *status*: the key set is contract.
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p)
        c.mounts.sync(timeout=5)
        status = c.a.mount.status
        assert set(status) == {'state', 'node_checksum', 'disk_checksum', 'in_sync',
                               'pending', 'in_flight', 'sense_error', 'error'}
        cs = Buffer('v', 'text').get_checksum().hex()
        assert status['state'] == 'active'
        assert status['node_checksum'] == status['disk_checksum'] == cs
        assert status['in_sync'] is True
        assert status['pending'] is False and status['in_flight'] is False
        assert status['sense_error'] is None and status['error'] is None


# --- Errors table ------------------------------------------------------------

@pytest.mark.parametrize('kwargs,exc,match', [
    (dict(mode='x'), ValueError, 'mode must be r, w or rw'),
    (dict(authority='disk'), ValueError, 'invalid mount authority'),
    (dict(persistent=1), TypeError, 'persistent must be bool'),
    (dict(mode='w', authority='file-strict'), ValueError, 'file-strict requires sensing and persistent=True'),
    (dict(authority='file-strict', persistent=False), ValueError, 'file-strict requires sensing and persistent=True'),
])
def test_invalid_request_raises_and_leaves_cell_unmounted(tmp_path, kwargs, exc, match):
    # mounts.md *Spec validation and normalization* and *Errors*.
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x')
        with pytest.raises(exc, match=match):
            c.a.mount(tmp_path / 'a.txt', **kwargs)
        assert c.a.mount.spec is None and c.a.mount.status is None
    assert not (tmp_path / 'a.txt').exists()


def test_write_only_file_authority_normalized_to_cell(tmp_path):
    # "mode='w' with authority='file' is silently normalized to authority='cell'",
    # visible on spec.authority and in get_graph().
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x')
        c.a.mount(tmp_path / 'a.txt', mode='w', authority='file')
        assert c.a.mount.spec.authority == 'cell'
        assert c.get_graph()['nodes'][0]['mount']['authority'] == 'cell'
    assert AttachmentSpec('p', 'w', 'file').authority == 'cell'


@pytest.mark.parametrize('celltype', ['checksum', 'deepcell', 'module'])
def test_unmountable_celltypes(tmp_path, celltype):
    # mounts.md *Mountable celltypes*: TypeError("Celltype '<ct>' is not mountable").
    with Context() as c:
        c.a = Cell(celltype=celltype)
        with pytest.raises(TypeError, match=f"Celltype '{celltype}' is not mountable"):
            c.a.mount(tmp_path / 'x')
        assert c.a.mount.spec is None


def test_deepfolder_default_mode_message_names_both_remedies(tmp_path):
    with Context() as c:
        c.a = Cell(celltype='deepfolder')
        with pytest.raises(TypeError) as info:
            c.a.mount(tmp_path / 'd')
        assert str(info.value) == ("Celltype 'deepfolder' can only be sensed: mount with mode='r', "
                                   "or use celltype 'folder' to write")


def test_already_mounted_is_value_error(tmp_path):
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x'); c.a.mount(tmp_path / 'a.txt')
        with pytest.raises(ValueError):
            c.a.mount(tmp_path / 'b.txt')
        assert c.a.mount.spec.path == str(tmp_path / 'a.txt')


@pytest.mark.xfail(strict=False, reason="mounts.md \u00a7Errors: NodeError for a missing node is unreachable "
                   "publicly; ctx.missing is a MissingView and ctx.missing.mount(...) raises TypeError")
def test_node_error_for_missing_node(tmp_path):
    with Context() as c:
        with pytest.raises(NodeError):
            c.missing.mount(tmp_path / 'b')


@pytest.mark.xfail(strict=False, reason="mounts.md \u00a7Errors: NodeError for a non-cell node is unreachable "
                   "publicly; a transformer handle has no .mount attribute (AttributeError)")
def test_node_error_for_non_cell_node(tmp_path):
    def f(x):
        return x
    with Context() as c:
        c.tf = f
        with pytest.raises(NodeError):
            c.tf.mount(tmp_path / 'a')


def test_mount_on_non_cell_or_missing_node_is_refused(tmp_path):
    # What the code does today for the two NodeError rows: refused, nothing attached.
    def f(x):
        return x
    with Context() as c:
        c.tf = f
        with pytest.raises((NodeError, AttributeError)):
            c.tf.mount(tmp_path / 'a')
        with pytest.raises((NodeError, TypeError)):
            c.missing.mount(tmp_path / 'b')
        assert c.mounts.sync(timeout=1) == {}


def test_sensing_mount_on_connected_node_is_authority_error(tmp_path):
    # Errors: "a sensing mount on a node with an incoming edge". Actuating is fine.
    with Context() as c:
        c.src = Cell(celltype='text'); c.src.set('x')
        c.a = Cell(celltype='text'); c.a = c.src
        for mode in ('r', 'rw'):
            with pytest.raises(AuthorityError):
                c.a.mount(tmp_path / f'{mode}.txt', mode=mode)
            assert c.a.mount.spec is None
        c.a.mount(tmp_path / 'w.txt', mode='w'); c.mounts.sync(timeout=5)
        assert (tmp_path / 'w.txt').read_text() == 'x\n'


def test_clearing_a_mounted_cell_is_refused_in_every_mode(tmp_path):
    # Errors: AuthorityError clearing a mounted cell; the refusal fires
    # regardless of mode (register Appendix C, false design statements).
    for mode in ('r', 'w', 'rw'):
        with Context() as c:
            c.a = Cell(celltype='text'); c.a.set('x')
            c.a.mount(tmp_path / f'{mode}.txt', mode=mode)
            with pytest.raises(AuthorityError, match='Cannot clear a mounted cell; unmount first'):
                c.a.checksum = None
            assert c.a.mount.spec is not None


def test_unmounted_cell_handle_answers_none_and_clear_error_raises_key_error():
    # mounts.md *Implementation status*: .spec/.status/.error answer None and
    # `del ctx.a.mount` is a silent no-op, while clear_error() raises KeyError.
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('x')
        assert c.a.mount.spec is None and c.a.mount.status is None and c.a.mount.error is None
        del c.a.mount
        with pytest.raises(KeyError):
            c.a.mount.clear_error()


# --- The initial decision table ------------------------------------------------

def test_initial_policy_no_value_and_null_node_arguments():
    # The rows that need the no_value / node_is_null inputs.
    rw_file = AttachmentSpec('a', 'rw', 'file')
    r_file = AttachmentSpec('a', 'r', 'file')
    strict = AttachmentSpec('a', 'rw', 'file-strict')
    w = AttachmentSpec('a', 'w', 'cell')
    F = 'f' * 64
    N = 'e' * 64
    # no value, zero-byte / empty directory -> node <- null (row 5), for any authority
    assert decide_initial(strict, F, None, no_value=True) == 'sense-null'
    assert decide_initial(AttachmentSpec('a', 'r', 'cell'), F, None, no_value=True) == 'sense-null'
    # complete N, zero-byte: row 3 writes under w/rw, row 4 nothing under r
    assert decide_initial(rw_file, F, N, no_value=True) == 'write'
    assert decide_initial(w, F, N, no_value=True) == 'write'
    assert decide_initial(r_file, F, N, no_value=True) == 'nothing'
    # zero-byte under file-strict is not "absent": it cannot override N
    assert decide_initial(strict, F, N, no_value=True) == 'write'
    # row 2: N null with absent / zero-byte -> nothing, any mode
    for spec in (rw_file, r_file, w):
        assert decide_initial(spec, ABSENT, N, node_is_null=True) == 'nothing'
        assert decide_initial(spec, F, N, no_value=True, node_is_null=True) == 'nothing'
    # row 1 precedes row 2
    assert decide_initial(strict, ABSENT, N, node_is_null=True) == 'error'
    # row 6: w with a rejected file writes N
    assert decide_initial(w, INVALID, N) == 'write'
    # row 9: file-strict with a rejected file
    assert decide_initial(strict, INVALID, N) == 'error'
    # no-value table row 2 precedes row 3
    assert decide_initial(strict, INVALID, None) == 'error'


def test_initial_rejected_file_without_value_is_sense_error(tmp_path):
    # No-value table row 2; "the session starts with the belief INVALID";
    # mounts never give up: fixing the file recovers the cell.
    p = tmp_path / 'a.json'; p.write_text('broken')
    with Context() as c:
        c.a = Cell(celltype='plain')
        assert c.a.mount(p) is None
        _assert_sense_error(c.a)
        assert c.a.mount.status['disk_checksum'] == INVALID
        assert c.a.mount.error is None
        assert p.read_text() == 'broken'
        p.write_text('{"a": 1}'); c.mounts.sync(timeout=5)
        assert c.a.state == 'complete' and c.a.value == {'a': 1}


def test_initial_rejected_file_with_value_keeps_value_and_belief_invalid(tmp_path):
    # Complete-value table row 9: sense error, N kept (graph value), belief INVALID.
    p = tmp_path / 'a.json'; p.write_text('broken')
    with Context() as c:
        c.a = Cell(celltype='plain'); c.a.set({'x': 1}); old = c.a.checksum.hex()
        c.a.mount(p)
        _assert_sense_error(c.a)
        assert c.a.mount.status['disk_checksum'] == INVALID
        assert c.get_graph()['nodes'][0]['value']['checksum'] == old


def test_strict_absent_with_value_keeps_value(tmp_path):
    # Complete-value table row 1: "sense error; N is kept".
    p = tmp_path / 'missing.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('kept'); old = c.a.checksum.hex()
        c.a.mount(p, authority='file-strict')
        _assert_sense_error(c.a)
        assert c.a.mount.status['disk_checksum'] == ABSENT
        assert c.get_graph()['nodes'][0]['value']['checksum'] == old
        assert not p.exists()


def test_initially_empty_directory_without_value_is_null(tmp_path):
    # No-value table row 5: an empty directory -> node <- null.
    p = tmp_path / 'd'; p.mkdir()
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        assert c.a.state == 'complete' and c.a.value is None
        assert c.a.checksum == Checksum(NULL_CHECKSUM)


@pytest.mark.parametrize('mode', ['rw', 'w'])
def test_zero_byte_file_with_value_is_overwritten(tmp_path, mode):
    # Complete-value table row 3: zero-byte file + w/rw -> write N.
    p = tmp_path / 'a.txt'; p.write_bytes(b'')
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('cell')
        c.a.mount(p, mode=mode)
        c.mounts.sync(timeout=5)
        assert c.a.value == 'cell'
        assert p.read_text() == 'cell\n'


def test_empty_directory_with_value_is_written(tmp_path):
    # Complete-value table row 3 for a directory mount.
    p = tmp_path / 'd'; p.mkdir()
    cs = _leaf(b'leaf')
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.set({'x': cs})
        c.a.mount(p, mode='rw')
        c.mounts.sync(timeout=5)
        assert (p / 'x').read_bytes() == b'leaf'
        assert c.a.value == {'x': cs}


def test_cell_authority_file_becomes_baseline_and_later_changes_flow_in(tmp_path):
    # Complete-value table row 8: "F becomes the baseline, and later file changes flow in".
    p = tmp_path / 'a.txt'; p.write_text('file')
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('cell')
        c.a.mount(p, mode='r', authority='cell')
        assert c.a.value == 'cell'
        assert c.a.mount.status['disk_checksum'] == Buffer('file', 'text').get_checksum().hex()
        p.write_text('edited'); c.mounts.sync(timeout=5)
        assert c.a.value == 'edited'


def test_write_only_equal_file_is_not_rewritten(tmp_path):
    # Complete-value table row 5: w, F = N -> nothing (file untouched, even non-canonical bytes).
    p = tmp_path / 'a.json'; p.write_text('{ "a" :  1 }')
    before = p.stat()
    with Context() as c:
        c.a = Cell(celltype='plain'); c.a.set({'a': 1})
        c.a.mount(p, mode='w'); c.mounts.sync(timeout=5)
        assert c.a.mount.status['in_sync']
    assert p.read_text() == '{ "a" :  1 }'
    assert p.stat().st_ino == before.st_ino and p.stat().st_mtime_ns == before.st_mtime_ns


# --- Observation classification --------------------------------------------------

def test_classify_observation_all_classes():
    F, G, H = 'f' * 64, 'a' * 64, 'b' * 64
    assert classify_observation(3, 3, F, G) == 'stale'
    assert classify_observation(2, 3, F, G) == 'stale'
    assert classify_observation(4, 3, F, F) == 'unchanged'
    assert classify_observation(4, 3, ABSENT, ABSENT, fingerprint=None, disk_fingerprint=None) == 'unchanged'
    assert classify_observation(4, 3, INVALID, INVALID, fingerprint=(1,), disk_fingerprint=(1,)) == 'unchanged'
    # For the sentinels, the fingerprint must match too
    assert classify_observation(4, 3, INVALID, INVALID, fingerprint=(2,), disk_fingerprint=(1,)) == 'rejected'
    assert classify_observation(4, 3, INVALID, F) == 'rejected'
    assert classify_observation(4, 3, ABSENT, F) == 'absent'
    assert classify_observation(4, 3, H, F, in_flight=H) == 'echo'
    assert classify_observation(4, 3, H, F, in_flight=G) == 'foreign'
    # unchanged precedes echo
    assert classify_observation(4, 3, F, F, in_flight=F) == 'unchanged'


def test_echo_of_in_flight_delivery_is_adopted():
    # *Observation classification*: echo = "our own write, seen before its
    # acknowledgement: adopt it as the belief". The node is not changed.
    with Context() as c, record_attachments(c) as log:
        c.a = Cell(celltype='text'); c.a.set('one')
        d = ManualDriver().attach(c.a, 'one')
        c.a = 'two'; c.get_graph()
        delivery = d.deliveries.popleft()
        d.observe('two'); c.get_graph()
        assert c.a.value == 'two'
        assert c.a.mount.status['disk_checksum'] == Buffer('two', 'text').get_checksum().hex()
        assert c.a.mount.status['in_flight']
        d.ack(delivery); c.get_graph()
        assert c.a.mount.status['in_sync']
        classes = [dict(e[3])['classification'] for e in log.entries() if e[2] == 'observation']
        assert classes == ['echo']


def test_identical_rewrite_and_touch_are_unchanged(tmp_path):
    # `unchanged`: "Identical-content rewrites and touch end here" -- in `w`
    # mode that means no reassert and no detector count.
    p = tmp_path / 'a.txt'
    with Context() as c, record_attachments(c) as log:
        c.a = Cell(celltype='text'); c.a.set('ours'); c.a.mount(p, mode='w')
        c.mounts.sync(timeout=5)
        for _ in range(4):
            p.write_text('ours\n'); os.utime(p); c.mounts.sync(timeout=5)
        assert c.a.mount.status['state'] == 'active' and c.a.mount.error is None
        kinds = [e[2] for e in log.entries()]
        assert 'reassert' not in kinds


def test_absent_clears_sense_error_without_changing_node(tmp_path):
    # `absent`: "the node is left unchanged; ... otherwise any sense error is cleared".
    p = tmp_path / 'a.json'; p.write_text('{"a": 1}')
    with Context() as c:
        c.a = Cell(celltype='plain'); c.a.mount(p)
        p.write_text('broken'); c.mounts.sync(timeout=5)
        _assert_sense_error(c.a)
        p.unlink(); c.mounts.sync(timeout=5)
        assert c.a.state == 'complete' and c.a.exception is None
        assert c.a.value == {'a': 1}
        assert c.a.mount.status['sense_error'] is None
        assert c.a.mount.status['disk_checksum'] == ABSENT


def test_write_mode_reasserts_after_deletion(tmp_path):
    # `absent` in `w` mode: reassert; Non-goals: "an actuating mount rewrites the
    # file ... at the next reassert in w mode".
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('ours'); c.a.mount(p, mode='w')
        c.mounts.sync(timeout=5)
        p.unlink(); c.mounts.sync(timeout=5)
        assert p.read_text() == 'ours\n'


def test_rw_mode_deletion_does_not_recreate(tmp_path):
    # Non-goals: "Recreating a file the user deleted, on its own" -- under rw the
    # deletion leaves the node unchanged and the file is not rewritten until the
    # next value change.
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('ours'); c.a.mount(p, mode='rw')
        c.mounts.sync(timeout=5)
        p.unlink(); c.mounts.sync(timeout=5)
        assert not p.exists() and c.a.value == 'ours'
        c.a = 'next'; c.mounts.sync(timeout=5)
        assert p.read_text() == 'next\n'


def test_foreign_sensed_value_is_not_written_back(tmp_path):
    # "A foreign observation in a sensing mode installs the checksum, sets the belief
    # and the actuation baseline in the same turn -- so the value is not written
    # straight back"; "non-canonical bytes are never rewritten".
    p = tmp_path / 'a.json'; p.write_text('{"a": 1}')
    with Context() as c:
        c.a = Cell(celltype='plain'); c.a.mount(p, mode='rw')
        p.write_text('{ "a" :   2 }'); c.mounts.sync(timeout=5)
        assert c.a.value == {'a': 2}
        assert c.mounts.sync(timeout=5)[('a',)]['in_sync']
        assert p.read_text() == '{ "a" :   2 }'


def test_emptied_directory_reads_as_empty_index(tmp_path):
    # "an emptied directory maps to {}" / "a directory emptied after mounting reads as {}".
    p = tmp_path / 'd'; p.mkdir(); (p / 'a').write_bytes(b'x')
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        assert set(c.a.value) == {'a'}
        (p / 'a').unlink(); c.mounts.sync(timeout=5)
        assert c.a.value == {} and c.a.checksum != Checksum(NULL_CHECKSUM)


@pytest.mark.xfail(strict=False, reason="mounts.md \u00a7Mountable celltypes: 'Code text is not syntax-checked' "
                   "is false for python -- the python celltype's HashType validation parses the code, on "
                   "assignment and on mount alike")
def test_code_text_file_is_not_syntax_checked(tmp_path):
    p = tmp_path / 'code.py'; p.write_text('def (:\n')
    with Context() as c:
        c.a = Cell(celltype='python'); c.a.mount(p, mode='r')
        assert c.a.state == 'complete' and c.a.value == 'def (:\n'


@pytest.mark.parametrize('celltype,content', [('python', 'def (:\n'), ('yaml', 'a: [broken\n'),
                                              ('ipython', '%%magic !!\n'), ('python', 'x = 1\n')])
def test_mount_parses_code_text_exactly_like_assignment(tmp_path, celltype, content):
    # *Canonical bytes*: "a mount parses neither more leniently nor more strictly
    # than a user assignment of the same value".
    p = tmp_path / 'code'; p.write_text(content)
    with Context() as c:
        c.u = Cell(celltype=celltype)
        try:
            c.u.set(content); assigned = c.u.state == 'complete'
        except Exception:
            assigned = False
        c.a = Cell(celltype=celltype); c.a.mount(p, mode='r')
        assert (c.a.state == 'complete') == assigned
        if assigned:
            assert c.a.checksum == c.u.checksum
        else:
            _assert_sense_error(c.a)


def test_zero_byte_bytes_file_reads_as_null_resolving_to_empty(tmp_path):
    # *Null files*: "bytes null reads as b''".
    p = tmp_path / 'blob'; p.write_bytes(b'data')
    with Context() as c:
        c.a = Cell(celltype='bytes'); c.a.mount(p, mode='r')
        assert c.a.value == b'data'
        p.write_bytes(b''); c.mounts.sync(timeout=5)
        assert c.a.checksum == Checksum(NULL_CHECKSUM)
        assert c.a.value == b''


def test_null_value_with_absent_file_is_in_sync(tmp_path):
    # *Null files*: "A null value with an absent file therefore counts as in sync".
    p = tmp_path / 'missing.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set(None); c.a.mount(p, mode='w')
        report = c.mounts.sync(timeout=5)
        assert report[('a',)]['in_sync'] and report.in_sync
        assert not p.exists()


def test_newline_only_file_is_parsed_normally(tmp_path):
    p = tmp_path / 'a.txt'; p.write_bytes(b'\n')
    q = tmp_path / 'b.json'; q.write_bytes(b'\n')
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.mount(p, mode='r')
        assert c.a.state == 'complete' and c.a.checksum != Checksum(NULL_CHECKSUM)
        assert c.a.checksum == Buffer('\n', 'text').get_checksum()
        c.b = Cell(celltype='int'); c.b.mount(q, mode='r')
        _assert_sense_error(c.b)


# --- Compression ------------------------------------------------------------

def test_gzip_write_is_deterministic(tmp_path):
    # "gzip with mtime=0 at level 6"
    p = tmp_path / 'a.txt.gz'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('hello'); c.a.mount(p, mode='w')
        c.mounts.sync(timeout=5)
    assert p.read_bytes() == gzip.compress(b'hello\n', compresslevel=6, mtime=0)


def test_foreign_recompression_of_identical_content_is_unchanged(tmp_path):
    # "A foreign recompression of identical content therefore classifies as unchanged."
    p = tmp_path / 'a.txt.gz'
    with Context() as c, record_attachments(c) as log:
        c.a = Cell(celltype='text'); c.a.set('hello'); c.a.mount(p, mode='w')
        c.mounts.sync(timeout=5)
        p.write_bytes(gzip.compress(b'hello\n', compresslevel=1, mtime=12345))
        recompressed = p.read_bytes()
        c.mounts.sync(timeout=5)
        classes = [dict(e[3])['classification'] for e in log.entries() if e[2] == 'observation']
        assert classes and set(classes) == {'unchanged'}
        assert 'reassert' not in [e[2] for e in log.entries()]
    assert p.read_bytes() == recompressed


# --- Fingerprints: mounts never give up ---------------------------------------

@pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason='root ignores permissions')
def test_unreadable_file_fails_cell_and_recovers_on_permission_fix(tmp_path):
    # *Fingerprints*: ctime makes a permission change visible, "so a file that was
    # unreadable is read again once it is fixed".
    p = tmp_path / 'a.txt'; p.write_text('one')
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.mount(p, mode='r')
        p.write_text('two'); p.chmod(0)
        try:
            c.mounts.sync(timeout=5)
            _assert_sense_error(c.a)
        finally:
            p.chmod(0o644)
        c.mounts.sync(timeout=5)
        assert c.a.state == 'complete' and c.a.value == 'two'


def test_same_size_same_mtime_rewrite_is_detected(tmp_path):
    # *Racy files*: a same-size rewrite that keeps mtime is still re-read while
    # within the racy window (and the inode/ctime change also reveal it).
    p = tmp_path / 'a.txt'; p.write_text('aaa')
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.mount(p, mode='r')
        st = p.stat()
        with open(p, 'r+b') as f:
            f.write(b'bbb')
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        c.mounts.sync(timeout=5)
        assert c.a.value == 'bbb'


# --- Conditional atomic writes ----------------------------------------------

def test_write_breaks_hardlink(tmp_path):
    # "Replacing a hardlinked target breaks the link, so other names keep the old content."
    p = tmp_path / 'a.txt'; p.write_text('old')
    alias = tmp_path / 'alias.txt'; os.link(p, alias)
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('new'); c.a.mount(p, mode='w')
        c.mounts.sync(timeout=5)
    assert p.read_text() == 'new\n' and alias.read_text() == 'old'


def test_write_leaves_no_temporary_files(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v1'); c.a.mount(p, mode='w')
        for n in range(3):
            c.a = f'v{n + 2}'; c.mounts.sync(timeout=5)
    assert sorted(os.listdir(tmp_path)) == ['a.txt']


# --- Reassert and detector -----------------------------------------------------

def test_detector_window_and_threshold():
    # "three reasserts within a rolling 20-second window"
    stamps = ()
    stamps, tripped = detector(stamps, 0.); assert not tripped
    stamps, tripped = detector(stamps, 10.); assert not tripped
    stamps, tripped = detector(stamps, 25.); assert not tripped   # 0. has aged out
    stamps, tripped = detector(stamps, 29.); assert tripped


def test_detector_trip_stops_actuation_and_keeps_registration(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('ours'); c.a.mount(p, mode='w')
        for n in range(3):
            p.write_text(f'theirs{n}'); c.mounts.sync(timeout=5)
        assert c.a.mount.status['state'] == 'tripped'
        assert c.mounts.errors[('a',)] is c.a.mount.error
        c.a = 'newer'; c.mounts.sync(timeout=5)
        assert p.read_text() == 'theirs2'
        assert c.a.mount.spec is not None
        assert c.a.state == 'complete' and c.a.exception is None


# --- Directory mounts --------------------------------------------------------

def test_directory_ignores_temporary_names(tmp_path):
    p = tmp_path / 'd'; p.mkdir(); (p / 'a').write_bytes(b'a')
    (p / '.b.seamless-0123.tmp').write_bytes(b'tmp')
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        assert set(c.a.value) == {'a'}


def test_directory_suffix_stripped_leaf_is_decompressed(tmp_path):
    p = tmp_path / 'd'; p.mkdir(); (p / 'x.gz').write_bytes(gzip.compress(b'payload'))
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        assert c.a.value == {'x': Buffer(b'payload').get_checksum().hex()}


def test_directory_suffix_collision_is_rejected(tmp_path):
    p = tmp_path / 'd'; p.mkdir()
    (p / 'x').write_bytes(b'one'); (p / 'x.gz').write_bytes(gzip.compress(b'two'))
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        _assert_sense_error(c.a)
        (p / 'x.gz').unlink(); c.mounts.sync(timeout=5)
        assert c.a.value == {'x': Buffer(b'one').get_checksum().hex()}


def test_directory_symlink_leaf_is_rejected(tmp_path):
    target = tmp_path / 'outside'; target.write_bytes(b'o')
    p = tmp_path / 'd'; p.mkdir(); (p / 'link').symlink_to(target)
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        _assert_sense_error(c.a)
        assert 'Directory mounts do not follow leaf symlinks' in c.a.exception


def test_directory_symlinked_subdirectory_not_traversed(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'o').write_bytes(b'o')
    p = tmp_path / 'd'; p.mkdir(); (p / 'a').write_bytes(b'a'); (p / 'sub').symlink_to(outside)
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='r')
        assert c.a.state == 'complete' and set(c.a.value) == {'a'}


@pytest.mark.parametrize('authority,cleaned', [('file', False), ('cell', True)])
def test_directory_cleanup_only_for_w_or_cell_authority(tmp_path, authority, cleaned):
    # "Cleanup applies only when mode == 'w' or authority == 'cell'. Otherwise
    # extraneous files are left in place. Empty subdirectories are then pruned."
    p = tmp_path / 'd'; p.mkdir(); (p / 'keep').write_bytes(b'k')
    cs = _leaf(b'k')
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.set({'keep': cs})
        c.a.mount(p, mode='rw', authority=authority); c.mounts.sync(timeout=5)
        (p / 'sub').mkdir(); (p / 'sub' / 'extra').write_bytes(b'e')
        c.mounts.sync(timeout=5)
        c.a = {'keep': cs, 'new': _leaf(b'n')}; c.mounts.sync(timeout=5)
        assert (p / 'new').read_bytes() == b'n'
        if cleaned:
            assert not (p / 'sub').exists()
        else:
            assert (p / 'sub' / 'extra').read_bytes() == b'e'


def test_directory_unsafe_index_key_is_a_delivery_error(tmp_path):
    p = tmp_path / 'd'; p.mkdir()
    with Context() as c:
        c.a = Cell(celltype='folder')
        c.a.set({'../escape': _leaf(b'x')})
        c.a.mount(p, mode='w')
        assert isinstance(c.a.mount.error, MountError)
        assert c.a.exception is None
        assert not (tmp_path / 'escape').exists()


def test_null_directory_reports_no_error(tmp_path):
    # "the only signal is in_sync: False with an empty ctx.mounts.errors"
    p = tmp_path / 'd'; p.mkdir(); (p / 'a').write_bytes(b'x')
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.mount(p, mode='rw')
        c.a.set(None); report = c.mounts.sync(timeout=5)
        assert not report.in_sync and report.errors == {} and c.mounts.errors == {}
        assert report[('a',)]['error'] is None and report[('a',)]['sense_error'] is None
        cs = _leaf(b'y')
        c.a = {'b': cs}; report = c.mounts.sync(timeout=5)
        assert report.in_sync


# --- sync() report ------------------------------------------------------------

def test_sync_report_shape_errors_and_in_sync(tmp_path):
    from seamless_workflow.attachments.session import SyncReport
    good = tmp_path / 'good.txt'; bad = tmp_path / 'bad.json'; bad.write_text('broken')
    with Context() as c:
        c.g = Cell(celltype='text'); c.g.set('g'); c.g.mount(good)
        c.b = Cell(celltype='plain'); c.b.mount(bad, mode='r')
        report = c.mounts.sync(timeout=5)
        assert isinstance(report, SyncReport) and isinstance(report, dict)
        assert set(report) == {('g',), ('b',)}
        assert report[('g',)]['in_sync'] and not report[('b',)]['in_sync']
        assert not report.in_sync
        assert set(report.errors) == {('b',)} and isinstance(report.errors[('b',)], MountError)
        assert c.mounts.errors == report.errors
        # detached copies: later changes do not alter the report
        bad.write_text('{"a": 1}'); c.mounts.sync(timeout=5)
        assert report[('b',)]['sense_error'] is not None
        assert c.mounts.errors == {}
        assert c.mounts.sync(timeout=5).in_sync


def test_report_in_sync_false_when_a_mount_has_an_error(tmp_path):
    p = tmp_path / 'missing' / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p, mode='w')
        report = c.mounts.sync(timeout=5)
        assert isinstance(report.errors[('a',)], MountError)
        assert not report.in_sync


# --- Delivery retry without an external turn ---------------------------------------

def test_failed_delivery_retries_without_a_context_call(tmp_path):
    # *Implementation status*: "on an idle Context a backoff fires within about
    # one poll interval" after it is due -- no Context call is needed.
    p = tmp_path / 'missing' / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('value'); c.a.mount(p, mode='w')
        assert isinstance(c.a.mount.error, MountError)
        p.parent.mkdir()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not p.exists():
            time.sleep(.1)
        assert p.read_text() == 'value\n'
        c.mounts.sync(timeout=5)
        assert c.a.mount.error is None


# --- Unmount, persistence and close -------------------------------------------

def test_unmount_deletes_unchanged_non_persistent_file_before_returning(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p, persistent=False)
        c.mounts.sync(timeout=5); assert p.exists()
        del c.a.mount
        assert not p.exists()
        assert c.a.value == 'v'


def test_unmount_deletes_non_persistent_directory_tree(tmp_path):
    p = tmp_path / 'd'
    with Context() as c:
        c.a = Cell(celltype='folder'); c.a.set({'sub/x': _leaf(b'x')})
        c.a.mount(p, mode='w', persistent=False); c.mounts.sync(timeout=5)
        assert (p / 'sub' / 'x').exists()
        del c.a.mount
        assert not p.exists()


def test_persistent_file_survives_unmount_and_close(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p)
        del c.a.mount
        assert p.read_text() == 'v\n'
        c.a.mount(p)
    assert p.read_text() == 'v\n'


def test_node_deletion_deletes_non_persistent_file(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p, persistent=False)
        c.mounts.sync(timeout=5)
        del c.a
        c.compute(timeout=5)
        deadline = time.monotonic() + 5
        while p.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not p.exists()


@pytest.mark.parametrize('reattach', [True, False])
def test_set_graph_never_deletes_non_persistent_file(tmp_path, reattach):
    # "A set_graph swap keeps the file ... whether or not the new graph re-attaches
    # the same path."
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v'); c.a.mount(p, persistent=False)
        c.mounts.sync(timeout=5)
        graph = c.get_graph()
        if not reattach:
            del graph['nodes'][0]['mount']
        c.set_graph(graph)
        assert p.read_text() == 'v\n'
        assert (c.a.mount.spec is not None) == reattach


# --- Path-overlap registry -------------------------------------------------------

def test_non_persistent_directory_overlap_refused_even_read_only(tmp_path):
    # "An overlap is refused ... if either side actuates or either side is a
    # non-persistent directory."
    p = tmp_path / 'd'; p.mkdir(); (p / 'x').write_bytes(b'x')
    with Context() as a, Context() as b:
        a.t = Cell(celltype='folder'); a.t.mount(p, mode='r', persistent=False)
        b.leaf = Cell(celltype='bytes')
        with pytest.raises(ValueError, match='Mount path overlaps existing registration'):
            b.leaf.mount(p / 'x', mode='r')
        b.t = Cell(celltype='folder')
        with pytest.raises(ValueError, match='Mount path overlaps existing registration'):
            b.t.mount(p, mode='r')


def test_read_only_prefix_overlap_allowed(tmp_path):
    p = tmp_path / 'd'; p.mkdir(); (p / 'x').write_bytes(b'x')
    with Context() as c:
        c.t = Cell(celltype='folder'); c.t.mount(p, mode='r')
        c.leaf = Cell(celltype='bytes'); c.leaf.mount(p / 'x', mode='r')
        assert c.leaf.value == b'x'


def test_actuating_overlap_refused_within_one_context(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('a'); c.a.mount(p, mode='r')
        c.b = Cell(celltype='text'); c.b.set('b')
        with pytest.raises(ValueError, match='Mount path overlaps existing registration'):
            c.b.mount(p, mode='rw')
        assert c.b.mount.spec is None


# --- Graph serialization ------------------------------------------------------------

def test_graph_mount_entry_is_normalized_spec(tmp_path):
    p = tmp_path / 'a.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('v')
        c.a.mount(p, mode='rw', authority='cell', persistent=False)
        graph = c.get_graph()
        assert graph['__seamless_workflow__'] == '0.4'
        assert graph['nodes'][0]['mount'] == {'path': str(p), 'mode': 'rw',
                                              'authority': 'cell', 'persistent': False}


def _graph_with_mount(tmp_path, celltype='text', **mount):
    with Context() as c:
        setattr(c, 'a', Cell(celltype=celltype))
        return {**c.get_graph(), 'nodes': [{**c.get_graph()['nodes'][0],
                                             'mount': {'path': str(tmp_path / 'x'), 'mode': 'rw',
                                                       'authority': 'file', 'persistent': True, **mount}}]}


@pytest.mark.parametrize('mounts', [True, False])
@pytest.mark.parametrize('celltype,mount,match', [
    ('text', dict(mode='x'), 'Invalid mount spec: mode must be r, w or rw'),
    ('text', dict(authority='x'), 'Invalid mount spec: invalid mount authority'),
    ('text', dict(persistent='yes'), 'Invalid mount spec: persistent must be bool'),
    ('text', dict(mode='w', authority='file-strict'), 'Invalid mount spec: file-strict requires'),
    ('text', dict(unknown=1), 'Invalid mount spec'),
    ('deepcell', {}, "Invalid mount spec: Celltype 'deepcell' is not mountable"),
    ('deepfolder', dict(mode='rw'), "Invalid mount spec: Celltype 'deepfolder' can only be sensed"),
])
def test_invalid_graph_mount_spec_is_path_error_even_without_mounts(tmp_path, mounts, celltype, mount, match):
    # *Graph serialization*: validated by prepare_graph before `mounts` is consulted.
    graph = _graph_with_mount(tmp_path, celltype, **mount)
    with Context() as c:
        c.keep = Cell(celltype='int'); c.keep.set(1)
        with pytest.raises(PathError, match=match):
            c.set_graph(graph, mounts=mounts)
        assert c.keep.value == 1
    assert not (tmp_path / 'x').exists()


@pytest.mark.parametrize('mounts', [True, False])
def test_graph_connection_into_sensing_mount_is_path_error(tmp_path, mounts):
    with Context() as c:
        c.src = Cell(celltype='text'); c.src.set('s')
        c.a = Cell(celltype='text'); c.a = c.src
        graph = c.get_graph()
    for node in graph['nodes']:
        if node['path'] == ['a']:
            node['mount'] = {'path': str(tmp_path / 'x'), 'mode': 'r', 'authority': 'file', 'persistent': True}
    with Context() as c:
        with pytest.raises(PathError):
            c.set_graph(graph, mounts=mounts)


@pytest.mark.parametrize('mounts', [True, False])
def test_graph_mount_on_non_cell_node_is_path_error(tmp_path, mounts):
    def f(x):
        return x
    with Context() as c:
        c.tf = f
        graph = c.get_graph()
    for node in graph['nodes']:
        if node['path'] == ['tf']:
            node['mount'] = {'path': str(tmp_path / 'x'), 'mode': 'rw', 'authority': 'file', 'persistent': True}
    with Context() as c:
        with pytest.raises(PathError):
            c.set_graph(graph, mounts=mounts)


def test_graph_load_with_broken_file_does_not_fail(tmp_path):
    # "the state of a file never makes the load fail -- it becomes a cell
    # exception or a mount error, with that mount monitoring."
    p = tmp_path / 'a.json'
    with Context() as c:
        c.a = Cell(celltype='plain'); c.a.set({'a': 1}); c.a.mount(p, mode='r')
        graph = c.get_graph()
    p.write_text('broken')
    with Context() as c:
        c.set_graph(graph, mounts=True)
        _assert_sense_error(c.a)
        p.write_text('{"a": 2}'); c.mounts.sync(timeout=5)
        assert c.a.value == {'a': 2}


def test_graph_load_with_write_mounts_writes_files_and_blocks(tmp_path):
    # "Loading a graph with w/rw mounts writes files" and set_graph(mounts=True)
    # blocks through the initial acknowledgements.
    p = tmp_path / 'out.txt'
    with Context() as c:
        c.a = Cell(celltype='text'); c.a.set('written'); c.a.mount(p, mode='w')
        graph = c.get_graph()
    p.unlink()
    with Context() as c:
        c.set_graph(graph, mounts=True)
        assert p.read_text() == 'written\n'
    p.unlink()
    with Context() as c:
        c.set_graph(graph, mounts=False)
        assert c.a.value == 'written' and c.a.mount.spec is None
        assert not p.exists()
