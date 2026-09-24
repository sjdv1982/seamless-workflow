"""Contract tests for the attachment framework (seamless/docs/agent/contracts/attachments.md).

Each test names the section of attachments.md whose rule it pins.  File-driver
specifics (bytes, paths, canonicalization, the detector thresholds) belong to
contracts/mounts.md and are not restated here; the file driver is used only as
the production transport, and ``ManualDriver`` wherever an interleaving has to be
forced deterministically.
"""
import copy
import threading
import time

import pytest

from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.errors import (
    AuthorityError, ClosedContextError, NodeError, PathError, ReentrantContextError,
)
from seamless_workflow.attachments import AttachmentSpec, ConflictError, MountError
from seamless_workflow.attachments.manual import ManualDriver


def add(x, y):
    return x + y


def fail_on_two(x):
    if x == 2:
        raise RuntimeError("boom")
    return x * 10


def _status(ctx, name):
    return ctx._controller.call("_mount_status", (name,), klass=4)


def _session(ctx, name):
    return ctx._mount_sessions[(name,)]


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=False, reason=(
    "attachments.md §Scope: standalone Cell.mount must raise "
    "AttributeError('mount is only available for bound workflow cells'); the "
    "property's AttributeError is swallowed by Cell.__getattr__, which re-raises "
    "a bare AttributeError('mount')"))
def test_scope_standalone_cell_message():
    with pytest.raises(AttributeError, match="only available for bound workflow cells"):
        Cell().mount("x")


def test_scope_transformer_result_and_subpath_are_not_mountable(tmp_path):
    with Context() as c:
        c.tf = add
        c.tf.pins.x = 1
        c.tf.pins.y = 2
        c.compute(timeout=30)
        with pytest.raises(AttributeError, match="Only whole Context cell nodes can be mounted"):
            c.tf.result.mount(tmp_path / "r")
        c.p = {"a": 1}
        with pytest.raises(AttributeError, match="Only whole Context cell nodes can be mounted"):
            c.p["a"].mount(tmp_path / "q")
        assert c.p.mount.spec is None


def test_scope_pin_transformer_and_code_have_no_mount(tmp_path):
    with Context() as c:
        c.tf = add
        c.tf.pins.x = 1
        c.tf.pins.y = 2
        with pytest.raises(AttributeError):
            c.tf.pins.x.mount
        with pytest.raises(AttributeError):
            c.tf.mount
        with pytest.raises(AttributeError):
            c.tf.code.mount


def test_scope_missing_or_transformer_node_is_node_error(tmp_path):
    # Not reachable through a public handle (a stale handle fails first), so
    # pinned at the controller's validation step that every attach passes through.
    with Context() as c:
        c.tf = add
        spec = AttachmentSpec(str(tmp_path / "x"))
        for path in (("tf",), ("missing",)):
            with pytest.raises(NodeError, match="Mounts require an existing whole cell node"):
                c._controller.call("_mount_validate", path, spec, klass=4)


def test_scope_already_mounted_is_value_error(tmp_path):
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(tmp_path / "a.txt", mode="w")
        with pytest.raises(ValueError, match="Cell is already mounted; unmount first"):
            c.a.mount(tmp_path / "other.txt", mode="w")
        assert c.a.mount.spec.path == str(tmp_path / "a.txt")


def test_public_calls_raise_after_close(tmp_path):
    c = Context()
    c.a = Cell(celltype="text")
    c.a.set("a")
    handle = c.a
    c.close()
    with pytest.raises(ClosedContextError):
        handle.mount(tmp_path / "a.txt")
    with pytest.raises(ClosedContextError):
        handle.mount.unmount()
    with pytest.raises(ClosedContextError):
        handle.mount.clear_error()
    with pytest.raises(ClosedContextError):
        c.mounts.sync(timeout=1)


def test_public_calls_are_reentrant_errors_on_controller_thread(tmp_path, monkeypatch):
    c = Context()
    try:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.b = Cell(celltype="text")
        c.b.set("b")
        c.b.mount(tmp_path / "b.txt", mode="w")
        a, b = c.a, c.b
        observed = []
        after_turn = Context._after_turn

        def check(self):
            if self is c and not observed:
                for operation in (lambda: a.mount(tmp_path / "a.txt"),
                                  lambda: b.mount.unmount(),
                                  lambda: b.mount.clear_error(),
                                  lambda: c.mounts.sync(timeout=1)):
                    with pytest.raises(ReentrantContextError):
                        operation()
                observed.append(True)
            return after_turn(self)

        monkeypatch.setattr(Context, "_after_turn", check)
        c.a = "trigger"
        assert observed
        monkeypatch.setattr(Context, "_after_turn", after_turn)
        assert c.a.mount.spec is None and c.b.mount.spec is not None
    finally:
        c.close()


# --------------------------------------------------------------------------
# Durable spec and ephemeral session
# --------------------------------------------------------------------------

def test_spec_survives_value_writes_and_config_edits(tmp_path):
    with Context() as c:
        c.a = Cell(celltype="plain")
        c.a.set(1)
        c.a.mount(tmp_path / "a.json")
        spec = c.a.mount.spec
        c.a = {"x": 2}
        assert c.a.mount.spec == spec
        c.a.scratch = True
        assert c.a.mount.spec == spec
        c.a = Cell(celltype="plain")  # same-celltype builder: still a cell
        assert c.a.mount.spec == spec


def test_spec_removed_and_session_closed_on_delete(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(p, mode="w")
        reg = _session(c, "a").registration
        del c.a
        c.compute(timeout=10)
        assert ("a",) not in c._mount_sessions
        assert reg.closing or reg.closed  # cleanup itself runs asynchronously
        c.a = Cell(celltype="text")
        c.a.set("again")
        assert c.a.mount.spec is None
        c.a.mount(p, mode="w")  # the registration was released
        c.mounts.sync(timeout=10)
        assert p.read_text() == "again\n"


def test_reattach_and_reload_get_fresh_session_ids(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(p)
        first = _session(c, "a").session_id
        del c.a.mount
        c.a.mount(p)
        second = _session(c, "a").session_id
        c.set_graph(c.get_graph())
        third = _session(c, "a").session_id
        assert len({first, second, third}) == 3


def test_late_observation_from_old_session_is_discarded():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        old_driver = ManualDriver().attach(c.a, "one")
        late = old_driver.observation("from-the-old-session")
        del c.a.mount
        ManualDriver().attach(c.a, "one")
        old_driver.registration.sink("_mount_observed", late)
        c.get_graph()
        assert c.a.value == "one"
        assert late.leases[0].released


def test_only_file_driver_is_serializable():
    with pytest.raises(ValueError, match="Only file mounts are serializable"):
        AttachmentSpec("m", driver="manual").to_graph()
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        ManualDriver().attach(c.a, "a")
        assert "mount" not in c.get_graph()["nodes"][0]
        del c.a.mount


def test_set_graph_detaches_without_deleting_nonpersistent_file(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(p, persistent=False)
        assert p.exists()
        c.set_graph(c.get_graph(), mounts=False)
        assert c.a.mount.spec is None
        assert p.read_text() == "a\n"


# --------------------------------------------------------------------------
# Topology rules
# --------------------------------------------------------------------------

def test_sensing_mount_refuses_incoming_edge_at_subpath(tmp_path):
    with Context() as c:
        c.src = Cell(celltype="int")
        c.src.set(5)
        c.a = Cell(celltype="plain")
        c.a.set({"k": 1})
        c.a.mount(tmp_path / "a.json")
        with pytest.raises(AuthorityError, match="Sensing mount is the producer; unmount first"):
            c.a["k"] = c.src
        with pytest.raises(AuthorityError, match="Sensing mount is the producer; unmount first"):
            c.a = c.src
        assert c.a.value == {"k": 1}


@pytest.mark.parametrize("mode", ["r", "rw"])
def test_sensing_attach_refused_when_node_has_incoming_edge(tmp_path, mode):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.src = Cell(celltype="text")
        c.src.set("s")
        c.a = Cell(celltype="text")
        c.a = c.src
        with pytest.raises(AuthorityError, match="Sensing mount cannot have incoming edges; unmount first"):
            c.a.mount(p, mode=mode)
        # nothing installed, reservation released
        assert c.a.mount.spec is None
        c.b = Cell(celltype="text")
        c.b.set("b")
        c.b.mount(p, mode="w")
        assert p.read_text() == "b\n"


def test_write_only_attach_allowed_with_incoming_edge(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.src = Cell(celltype="text")
        c.src.set("s")
        c.a = Cell(celltype="text")
        c.a = c.src
        c.a.mount(p, mode="w")
        c.mounts.sync(timeout=10)
        assert p.read_text() == "s\n"


def test_graph_with_sensing_mount_and_incoming_connection_is_refused(tmp_path):
    with Context() as c:
        c.src = Cell(celltype="text")
        c.src.set("s")
        c.a = Cell(celltype="text")
        c.a = c.src
        c.a.mount(tmp_path / "a.txt", mode="w")
        graph = copy.deepcopy(c.get_graph())
    for node in graph["nodes"]:
        if "mount" in node:
            node["mount"]["mode"] = "rw"
            node["mount"]["authority"] = "file"
    with Context() as c2:
        with pytest.raises(PathError, match="Sensing mounts cannot have incoming connections"):
            c2.set_graph(graph)


@pytest.mark.parametrize("mode", ["r", "w", "rw"])
def test_celltype_frozen_and_clearing_refused_in_every_mode(tmp_path, mode):
    p = tmp_path / "a.txt"
    p.write_text("a")
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(p, mode=mode)
        with pytest.raises(ValueError, match="Mounted celltype cannot change; unmount first"):
            c.a.celltype = "bytes"
        with pytest.raises(ValueError, match="Mounted celltype cannot change; unmount first"):
            c.a = Cell(celltype="bytes")
        for clear in (lambda: setattr(c.a, "checksum", None),
                      lambda: setattr(c.a, "buffer", None),
                      lambda: c.a.set_checksum(None)):
            with pytest.raises(AuthorityError, match="Cannot clear a mounted cell; unmount first"):
                clear()
        assert c.a.value == "a" and c.a.mount.spec is not None


# --------------------------------------------------------------------------
# Sense
# --------------------------------------------------------------------------

def test_sense_supersedes_undispatched_pending_delivery_not_in_flight():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one")
        c.a = "two"
        in_flight = d.deliveries.popleft()
        c.a = "three"
        c.get_graph()
        pending = _session(c, "a").pending
        assert pending is not None
        d.observe("foreign")
        c.get_graph()
        assert c.a.value == "foreign"
        assert _session(c, "a").pending is None and pending.lease.released
        assert _session(c, "a").in_flight is in_flight  # never cancelled
        d.ack(in_flight)
        c.get_graph()
        assert not d.deliveries
        del c.a.mount


def test_sense_and_user_write_have_equal_authority():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="r")
        d.observe("sensed")
        c.get_graph()
        assert c.a.value == "sensed"
        c.a = "user"
        assert c.a.value == "user"
        d.observe("sensed-again")
        c.get_graph()
        assert c.a.value == "sensed-again"
        del c.a.mount


def test_program_invalid_content_is_sensed_like_a_user_write(tmp_path):
    source = "def f(:\n"
    p = tmp_path / "code.py"
    p.write_text(source)
    with Context() as c:
        c.code = Cell(celltype="python")
        c.code.mount(p, mode="r")
        c.user = Cell(celltype="python")
        c.user.set(source)
        assert c.code.state == c.user.state == "complete"
        assert c.code.exception is None
        assert c.code.checksum == c.user.checksum


# --------------------------------------------------------------------------
# Actuate
# --------------------------------------------------------------------------

def test_only_complete_actuates_failed_upstream_keeps_resource(tmp_path):
    p = tmp_path / "out.txt"
    with Context() as c:
        c.tf = fail_on_two
        c.tf.pins.x = 1
        c.out = c.tf.result
        c.compute(timeout=30)
        c.out.mount(p, mode="w")
        c.mounts.sync(timeout=10)
        assert p.read_text() == "10\n"
        before = p.stat().st_mtime_ns
        c.tf.pins.x = 2
        c.compute(timeout=30)
        report = c.mounts.sync(timeout=10)  # resolves although the node is not complete
        assert c.out.state != "complete"
        assert p.read_text() == "10\n" and p.stat().st_mtime_ns == before
        assert report[("out",)]["in_sync"] is False and report[("out",)]["error"] is None


def test_pending_equivalent_to_resource_is_dropped_on_ack():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        in_flight = d.deliveries.popleft()
        c.a = "three"
        c.a = "two"
        c.get_graph()
        d.ack(in_flight)
        c.get_graph()
        assert not d.deliveries
        assert not _status(c, "a")["pending"] and not _status(c, "a")["in_flight"]


def test_compute_does_not_wait_for_delivery():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        c.compute(timeout=5)
        assert _status(c, "a")["in_flight"]
        d.ack(d.deliveries.popleft())
        c.get_graph()


def test_no_settled_predicate():
    with Context() as c:
        assert not hasattr(c.mounts, "settled")


# --------------------------------------------------------------------------
# Error model
# --------------------------------------------------------------------------

def test_sense_error_typing_and_prefix(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("broken")
    with Context() as c:
        c.a = Cell(celltype="plain")
        c.a.set({"x": 1})
        c.b = c.a
        c.a.mount(p)
        assert c.a.state == "failed"
        assert isinstance(c.a.exception, str)
        assert c.a.exception.startswith(f"{p}: ")
        status = c.a.mount.status
        assert isinstance(status["sense_error"], MountError)
        assert str(status["sense_error"]) == c.a.exception
        assert c.a.mount.error is None  # sense errors live on the cell, not .error
        assert c.b.state == "blocked" and c.b.block_reason == "blocked-by-error"
        assert isinstance(c.mounts.errors[("a",)], MountError)
        del c.a.mount
        assert c.a.state == "complete" and c.a.value == {"x": 1}


def test_clear_exception_on_sense_error_requests_reobservation():
    class Recording(ManualDriver):
        def __init__(self):
            super().__init__()
            self.polls = []

        def poll(self, reg, *, force=False):
            self.polls.append(force)

    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = Recording().attach(c.a, "one", mode="r")
        d.observe(rejected="unreadable")
        c.get_graph()
        assert c.a.state == "failed"
        c.a.clear_exception()
        c.get_graph()
        assert d.polls == [True]
        assert c.a.state == "failed"  # not re-derived: the resource owns the error
        d.observe("fixed")
        c.get_graph()
        assert c.a.state == "complete" and c.a.value == "fixed"
        del c.a.mount


def test_delivery_backoff_doubles_and_caps_at_60s():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        session = _session(c, "a")
        delays = []
        for _ in range(8):
            d.ack(d.deliveries.popleft(), outcome="error")
            c.get_graph()
            assert c.a.exception is None and isinstance(c.a.mount.error, MountError)
            delays.append(round(session.retry_at - time.monotonic()))
            session.retry_at = 0  # make the retry due
            c.get_graph()
        assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
        d.ack(d.deliveries.popleft())
        c.get_graph()
        assert c.a.mount.error is None and session.retry_delay == 1


def test_failed_delivery_retries_without_any_context_call(tmp_path):
    p = tmp_path / "missing" / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("value")
        c.a.mount(p, mode="w")
        assert isinstance(c.a.mount.error, MountError)
        p.parent.mkdir()
        deadline = time.monotonic() + 15
        while not p.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert p.read_text() == "value\n"


# --------------------------------------------------------------------------
# Oscillation detector (framework side only)
# --------------------------------------------------------------------------

def test_tripped_attachment_is_paused_not_dead():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("ours")
        d = ManualDriver().attach(c.a, "ours", mode="w")
        d.observe("theirs0")
        c.get_graph()
        first = d.deliveries.popleft()  # reassert #1 in flight
        d.observe("theirs1")
        c.get_graph()  # reassert #2 pending behind it
        assert _status(c, "a")["pending"]
        d.observe("theirs2")
        c.get_graph()  # reassert #3 trips
        status = _status(c, "a")
        assert status["state"] == "tripped" and isinstance(status["error"], ConflictError)
        assert not status["pending"]
        d.ack(first)
        c.get_graph()
        assert not d.deliveries
        c.a = "new"
        c.get_graph()
        assert not d.deliveries and not _status(c, "a")["pending"]
        d.observe("theirs9")
        c.get_graph()
        from seamless import Buffer
        assert _status(c, "a")["disk_checksum"] == Buffer("theirs9", "text").get_checksum().hex()
        c.a.mount.clear_error()
        c.get_graph()
        assert _status(c, "a")["state"] == "active" and c.a.mount.error is None
        delivery = d.deliveries.popleft()
        assert delivery.lease.checksum.resolve("text") == "new"
        d.ack(delivery)
        c.get_graph()


# --------------------------------------------------------------------------
# Barrier
# --------------------------------------------------------------------------

def test_close_fails_a_pending_sync_barrier():
    c = Context()
    c.a = Cell(celltype="text")
    c.a.set("one")
    d = ManualDriver().attach(c.a, "one", mode="w")
    c.a = "two"
    outcome = []

    def waiter():
        try:
            c.mounts.sync(timeout=30)
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)
    assert not outcome  # in-flight delivery keeps it unsettled
    c.close(timeout=0.5)
    thread.join(10)
    assert outcome and isinstance(outcome[0], ClosedContextError)


# --------------------------------------------------------------------------
# Detach and close
# --------------------------------------------------------------------------

def test_unattached_surface():
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        assert c.a.mount.spec is None
        assert c.a.mount.status is None
        assert c.a.mount.error is None
        del c.a.mount  # silent no-op
        with pytest.raises(Exception):  # bare KeyError today; type not contract
            c.a.mount.clear_error()


def test_close_flushes_already_requested_pending_once():
    c = Context()
    c.a = Cell(celltype="text")
    c.a.set("one")
    d = ManualDriver().attach(c.a, "one", mode="w")
    c.a = "two"
    first = d.deliveries.popleft()
    c.a = "three"  # pending behind the in-flight one
    c.get_graph()
    closer = threading.Thread(target=c.close, kwargs={"timeout": 10})
    closer.start()
    time.sleep(0.2)
    d.ack(first)
    deadline = time.monotonic() + 5
    while not d.deliveries and time.monotonic() < deadline:
        time.sleep(0.05)
    flushed = d.deliveries.popleft()
    assert flushed.lease.checksum.resolve("text") == "three"
    d.ack(flushed)
    closer.join(10)
    assert not closer.is_alive()


def test_close_does_not_flush_or_retry_a_failed_delivery():
    c = Context()
    c.a = Cell(celltype="text")
    c.a.set("one")
    d = ManualDriver().attach(c.a, "one", mode="w")
    c.a = "two"
    d.ack(d.deliveries.popleft(), outcome="error")
    c.get_graph()
    assert _status(c, "a")["pending"]
    c.close(timeout=2)
    assert not d.deliveries


def test_close_timeout_bounds_an_unacknowledged_delivery():
    c = Context()
    c.a = Cell(celltype="text")
    c.a.set("one")
    d = ManualDriver().attach(c.a, "one", mode="w")
    c.a = "two"
    start = time.monotonic()
    c.close(timeout=0.5)
    assert time.monotonic() - start < 10
    assert d.registration.closed
