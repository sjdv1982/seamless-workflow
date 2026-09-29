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


def test_scope_pin_and_code_have_no_mount(tmp_path):
    with Context() as c:
        c.tf = add
        c.tf.pins.x = 1
        c.tf.pins.y = 2
        with pytest.raises(AttributeError):
            c.tf.pins.x.mount
        with pytest.raises(AttributeError):
            c.tf.code.mount


def test_scope_transformer_node_is_node_error(tmp_path):
    with Context() as c:
        c.tf = add
        with pytest.raises(NodeError, match="Mounts require an existing whole cell node"):
            c.tf.mount(tmp_path / "x")


def test_scope_missing_node_is_node_error(tmp_path):
    with Context() as c:
        with pytest.raises(NodeError, match="Mounts require an existing whole cell node"):
            c.missing.mount(tmp_path / "x")
        assert not (tmp_path / "x").exists()


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


def test_spec_removed_and_session_closed_on_delete(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("a")
        c.a.mount(p, mode="w")
        del c.a
        c.compute(timeout=10)
        assert ("a",) not in c._mount_sessions
        c.a = Cell(celltype="text")
        c.a.set("again")
        assert c.a.mount.spec is None
        c.a.mount(p, mode="w")  # the registration was released
        c.mounts.sync(timeout=10)
        assert p.read_text() == "again\n"


def test_node_deletion_detach_waits_for_transport_cleanup(tmp_path):
    with Context() as c:
        for n in range(5):
            p = tmp_path / f"a{n}.txt"
            c.a = Cell(celltype="text")
            c.a.set("a")
            c.a.mount(p, persistent=False)
            assert p.exists()
            del c.a
            assert not p.exists()


def test_unmount_waits_for_transport_cleanup(tmp_path):
    with Context() as c:
        for n in range(5):
            p = tmp_path / f"a{n}.txt"
            c.a = Cell(celltype="text")
            c.a.set("a")
            c.a.mount(p, persistent=False)
            assert p.exists()
            del c.a.mount
            assert not p.exists()
            del c.a


def test_same_celltype_empty_builder_detaches_the_mount(tmp_path):
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("value")
        c.a.mount(p, mode="w")
        assert p.read_text() == "value\n"
        c.a = Cell(celltype="text")
        assert c.a.mount.spec is None
        assert c.a.mount.status is None
        assert ("a",) not in c._mount_sessions
        assert c.get_graph()["nodes"][0].get("mount") is None
        assert c.a.checksum is None
        assert p.read_text() == "value\n"  # persistent: detaching leaves the resource
        c.a = "later"
        c.compute(timeout=10)
        assert p.read_text() == "value\n"  # no longer actuated
        c.b = Cell(celltype="text")
        c.b.set("b")
        c.b.mount(p, mode="w")  # the registration was released


@pytest.mark.parametrize("mode", ["r", "w", "rw"])
def test_same_celltype_empty_builder_is_not_refused_clears_and_keeps_file(tmp_path, mode):
    # attachments.md §Topology (the one exempt assignment) and §Detach: not refused,
    # the cell is cleared, and a persistent resource is neither rewritten nor deleted.
    # This half already holds (Implementation status: "The rest already matches").
    p = tmp_path / "a.txt"
    p.write_text("value")
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("value")
        c.a.mount(p, mode=mode)
        before = p.stat().st_mtime_ns
        c.a = Cell(celltype="text")
        assert c.a.checksum is None
        c.compute(timeout=10)
        time.sleep(0.3)
        assert p.exists() and p.read_text().rstrip("\n") == "value"
        assert p.stat().st_mtime_ns == before


def test_subcontext_deletion_detaches_each_attachment(tmp_path):
    # attachments.md §Detach: deleting a subcontext that contains attached cells
    # detaches each attachment (the wait is pinned separately below).
    p1, p2 = tmp_path / "a.txt", tmp_path / "b.txt"
    with Context() as c:
        c.sub.a = Cell(celltype="text")
        c.sub.a.set("a")
        c.sub.a.mount(p1, mode="w")
        c.sub.b = Cell(celltype="text")
        c.sub.b.set("b")
        c.sub.b.mount(p2, mode="w")
        del c.sub
        c.compute(timeout=10)
        assert not c._mount_sessions
        c.x = Cell(celltype="text")
        c.x.set("x")
        c.x.mount(p1, mode="w")  # the registration was released
        c.mounts.sync(timeout=10)
        assert p1.read_text() == "x\n"


def test_subcontext_deletion_waits_for_transport_cleanup(tmp_path):
    with Context() as c:
        for n in range(5):
            p = tmp_path / f"a{n}.txt"
            c.sub.a = Cell(celltype="text")
            c.sub.a.set("a")
            c.sub.a.mount(p, persistent=False)
            assert p.exists()
            del c.sub
            assert not p.exists()


def test_transformer_assignment_onto_mounted_cell_is_refused_and_keeps_spec(tmp_path):
    # attachments.md §Durable spec: assigning a transformer onto a cell node is refused;
    # the refused assignment leaves the spec in place.  (The exception type is the gap
    # pinned below.)
    from seamless.transformer import delayed
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("x")
        c.a.mount(p, mode="w")
        spec = c.a.mount.spec
        for value in (add, delayed(add)):
            with pytest.raises(Exception):
                c.a = value
            assert c.a.mount.spec == spec
            assert c.a.value == "x"


def test_transformer_assignment_onto_mounted_cell_is_node_error(tmp_path):
    from seamless.transformer import delayed
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("x")
        c.a.mount(tmp_path / "a.txt", mode="w")
        for value in (add, delayed(add)):
            with pytest.raises(NodeError):
                c.a = value
            assert c.a.mount.spec is not None


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


def test_python_syntax_error_in_sensed_value_fails_downstream(tmp_path):
    # attachments.md §Sense (ruled 2026-09-28): a mount checks what an assignment
    # checks, and for code text that excludes syntax. A syntax error is not a
    # sense error: the cell is complete, and the SyntaxError is the failure of
    # the transformer that runs the code.
    p = tmp_path / "code.py"
    p.write_text("def f(:\n")
    with Context() as c:
        c.code = Cell(celltype="python")
        c.code.mount(p, mode="r")
        c.compute(timeout=10)
        assert c.code.state == "complete"
        assert c.code.exception is None
        assert c.code.mount.status["sense_error"] is None
        assert c.code.mount.error is None
        c.tf = ident
        c.tf.code = c.code
        c.tf.pins.x = 1
        c.compute(timeout=30)
        assert c.tf.state == "failed"
        assert "syntax" in c.tf.exception.lower()
        assert c.code.state == "complete" and c.code.exception is None
        # A fixed file is an ordinary change.
        p.write_text("def f(x):\n    return x\n")
        c.mounts.sync(timeout=10)
        c.compute(timeout=30)
        assert c.tf.state == "complete"


def test_program_wrong_content_that_parses_is_sensed_like_a_user_write(tmp_path):
    # attachments.md §Sense: content that parses but is wrong for the program is not
    # rejected by the mount; it fails where a user write would, in the transformer.
    source = "def f(x):\n    raise RuntimeError('wrong for the program')\n"
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
        c.tf = ident
        c.tf.code = c.code
        c.tf.pins.x = 1
        c.compute(timeout=30)
        assert c.tf.state == "failed"
        assert c.code.state == "complete" and c.code.exception is None


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
        assert c.a.mount.spec is None
        # clear_error() on an unattached cell is undecided (Implementation status): not pinned


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


# --------------------------------------------------------------------------
# 2026-09-26 adaptation: rulings (anonymous handles; "any state other than
# complete" does not deliver, including the miswired family)
# --------------------------------------------------------------------------

def ident(x):
    return x


def test_scope_projection_handle_is_not_mountable(tmp_path):
    with Context() as c:
        c.a = Cell(celltype="mixed")
        c.a.set({"k": 1})
        with pytest.raises(AttributeError, match="Only whole Context cell nodes can be mounted"):
            c.a["k"].mount(tmp_path / "proj")
        assert c.a.mount.spec is None


@pytest.mark.skip(reason=(
    "attachments.md §Scope: mounting a bound as_celltype handle is 'unspecified -- deferred; "
    "do not depend on either outcome'. No outcome is contract, so none is pinned (an earlier "
    "version xfail-pinned AttributeError('Only whole Context cell nodes can be mounted'), which "
    "is not a ruling). Un-skip and assert once the author rules"))
def test_scope_as_celltype_handle_mount_is_deferred(tmp_path):
    with Context() as c:
        c.a = Cell(celltype="mixed")
        c.a.set({"k": 1})
        c.a.as_celltype("plain").mount(tmp_path / "anon")


def test_miswired_upstream_does_not_actuate_and_barrier_settles(tmp_path):
    p = tmp_path / "out.txt"
    with Context() as c:
        c.src = Cell(celltype="mixed")
        c.src.set({"a": 1})
        c.tf = ident
        c.tf.pins.x = c.src["a"]
        c.out = c.tf.result
        c.compute(timeout=30)
        c.out.mount(p, mode="w")
        c.mounts.sync(timeout=10)
        assert p.read_text() == "1\n"
        before = p.stat().st_mtime_ns
        c.src.celltype = "plain"  # the pin (mixed) no longer matches the projected source
        deadline = time.monotonic() + 10
        while c.tf.state != "miswired" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert c.tf.state == "miswired"
        time.sleep(0.5)
        # attachments.md §Actuate: any state other than complete delivers nothing
        assert c.out.state != "complete"
        assert p.read_text() == "1\n" and p.stat().st_mtime_ns == before
        # node-state side, and the barrier resolves on a non-complete actuating node
        assert c.out.state == "blocked" and c.out.block_reason == "blocked-by-miswiring"
        report = c.mounts.sync(timeout=5)
        assert report[("out",)]["in_sync"] is False and report[("out",)]["error"] is None


# --------------------------------------------------------------------------
# 2026-09-26 coverage pass: statements of attachments.md that had no test
# --------------------------------------------------------------------------

def test_remedy_mounted_code_cell_connected_to_transformer_code(tmp_path):
    # attachments.md §Scope: "attach a cell and connect it" is the supported way to
    # edit transformer code externally.
    p = tmp_path / "code.py"
    p.write_text("def f(x):\n    return x + 1\n")
    with Context() as c:
        c.code = Cell(celltype="python")
        c.code.mount(p, mode="r")
        c.tf = ident
        c.tf.code = c.code
        c.tf.pins.x = 1
        c.compute(timeout=30)
        assert c.tf.result.value == 2
        p.write_text("def f(x):\n    return x + 100\n")
        c.mounts.sync(timeout=10)
        c.compute(timeout=30)
        assert c.tf.result.value == 101


def test_user_write_clears_the_sense_error():
    # attachments.md §Sense: "any value write clears the sense error" -- a user
    # assignment as well as a valid observation.
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="rw")
        d.observe(rejected="unreadable")
        c.get_graph()
        assert c.a.state == "failed" and _status(c, "a")["sense_error"] is not None
        c.a = "user"
        assert c.a.state == "complete" and c.a.value == "user"
        assert c.a.exception is None and _status(c, "a")["sense_error"] is None
        d.ack(d.deliveries.popleft())
        c.get_graph()
        del c.a.mount


def test_sense_error_masks_but_keeps_stored_value(tmp_path):
    # attachments.md §Sense errors fail the cell: published checksum dropped, but
    # get_graph() still records the last good value.
    p = tmp_path / "a.json"
    with Context() as c:
        c.a = Cell(celltype="plain")
        c.a.set({"x": 1})
        good = c.a.checksum.hex()
        c.a.mount(p, mode="rw")
        p.write_text("broken")
        c.mounts.sync(timeout=10)
        assert c.a.state == "failed"
        assert not c.a.checksum
        node = [n for n in c.get_graph()["nodes"] if n["path"] == ["a"]][0]
        assert node["value"]["checksum"] == good


def test_disappearance_does_not_clear_the_node(tmp_path):
    # attachments.md §Sense: "Disappearance does not clear the node."
    p = tmp_path / "a.txt"
    p.write_text("hello")
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.mount(p, mode="r")
        cs = c.a.checksum
        p.unlink()
        report = c.mounts.sync(timeout=10)
        assert c.a.checksum == cs and c.a.value == "hello"
        assert report[("a",)]["state"] == "active"  # still monitoring
        p.write_text("back")
        c.mounts.sync(timeout=10)
        assert c.a.value == "back"


def test_waiting_node_does_not_actuate():
    # attachments.md §Actuate: a node in any state other than complete (here: waiting,
    # a cell mid-recompute) delivers nothing.
    with Context() as c:
        c.src = Cell(celltype="int")
        c.src.set(1)
        c.tf = slow_ident
        c.tf.pins.x = c.src
        c.out = c.tf.result
        c.compute(timeout=30)
        d = ManualDriver().attach(c.out, 1, mode="w")
        c.src = 2
        c.get_graph()
        assert c.out.state != "complete"
        assert not d.deliveries and not _status(c, "out")["pending"]
        c.compute(timeout=30)
        c.get_graph()
        delivery = d.deliveries.popleft()
        assert delivery.lease.checksum.resolve("int") == 2
        d.ack(delivery)
        c.get_graph()
        del c.out.mount


def test_latest_discipline_replaces_pending_and_releases_its_claim():
    # attachments.md §Actuate, latest discipline: at most one pending and one in-flight;
    # a new request replaces the pending one and releases its claim; a series of edits
    # produces one write of the last value.
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        in_flight = d.deliveries.popleft()
        c.a = "three"
        c.get_graph()
        replaced = _session(c, "a").pending
        c.a = "four"
        c.a = "five"
        c.get_graph()
        assert replaced.lease.released
        assert _session(c, "a").in_flight is in_flight
        assert not d.deliveries
        d.ack(in_flight)
        c.get_graph()
        last = d.deliveries.popleft()
        assert last.lease.checksum.resolve("text") == "five"
        assert not d.deliveries
        d.ack(last)
        c.get_graph()
        assert last.lease.released


def test_failed_delivery_retries_immediately_on_value_change():
    # attachments.md §Write failures: retried with backoff "and immediately whenever the
    # node's value changes".
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        d.ack(d.deliveries.popleft(), outcome="error")
        c.get_graph()
        assert _session(c, "a").retry_at > time.monotonic() + 0.5  # backoff not yet due
        assert not d.deliveries
        c.a = "three"
        c.get_graph()
        delivery = d.deliveries.popleft()
        assert delivery.lease.checksum.resolve("text") == "three"
        d.ack(delivery)
        c.get_graph()
        assert c.a.mount.error is None


def test_delivery_resolves_never_computes_cache_miss_is_a_delivery_error(tmp_path):
    # attachments.md §A delivery resolves; it never computes: an unresolvable payload is
    # an ordinary delivery error on the attachment, never on the cell, and the barrier
    # resolves with it reported (a failed delivery waiting for retry counts as settled).
    from seamless import Checksum
    p = tmp_path / "a.txt"
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set_checksum(Checksum(bytes(range(32))))
        c.a.mount(p, mode="w")  # does not raise for the resource's state
        assert c.a.exception is None
        assert isinstance(c.a.mount.error, MountError)
        assert "CacheMissError" in str(c.a.mount.error)
        report = c.mounts.sync(timeout=10)
        assert isinstance(report[("a",)]["error"], MountError)
        assert not p.exists()


def test_barrier_resolves_with_a_sense_error(tmp_path):
    # attachments.md §The cut barrier: resolves rather than raises when a cell has a
    # sense error; the report says which.
    p = tmp_path / "a.json"
    p.write_text("broken")
    with Context() as c:
        c.a = Cell(celltype="plain")
        c.a.mount(p, mode="r")
        report = c.mounts.sync(timeout=10)
        assert isinstance(report[("a",)]["sense_error"], MountError)


def test_barrier_timeout_raises_withdraws_and_cancels_nothing():
    # attachments.md §The cut barrier: expiry raises TimeoutError, withdraws the
    # predicate and cancels nothing.
    with Context() as c:
        c.a = Cell(celltype="text")
        c.a.set("one")
        d = ManualDriver().attach(c.a, "one", mode="w")
        c.a = "two"
        in_flight = d.deliveries[0]
        with pytest.raises(TimeoutError):
            c.mounts.sync(timeout=0.3)
        assert _session(c, "a").in_flight is in_flight  # nothing cancelled
        d.ack(d.deliveries.popleft())
        c.get_graph()
        assert not _status(c, "a")["in_flight"]
        del c.a.mount


def slow_ident(x):
    import time
    time.sleep(1.5)
    return x
