"""Phase B: per-driver slots, shared sensing authority and paced delivery.

The accepted rulings specialize the Phase A contracts: one file and one widget
slot, tuple report keys, latest accepted input wins, and minimum 2/3-second
spacing between cell deliveries in each session.
"""
import time

import ipywidgets as widgets
import pytest
import traitlets

from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.attachments import ConflictError, MountError
from seamless_workflow.attachments.widget import WidgetDriver
from seamless_workflow.errors import AuthorityError
from seamless_workflow.jupyter import output, traitlet


class Value(traitlets.HasTraits):
    value = traitlets.Any(default_value=None, allow_none=True)


def sync(ctx):
    return ctx.mounts.sync(timeout=10)


def setup_cell(ctx, name="a", value=1):
    setattr(ctx, name, Cell(celltype="int"))
    getattr(ctx, name).set(value)


def key(name, driver):
    return ((name,), driver)


def both(ctx, path, value=1):
    setup_cell(ctx, value=value)
    ctx.a.mount(path, mode="rw", authority="cell")
    t = traitlet(ctx.a)
    widget = Value()
    t.link(widget)
    sync(ctx)
    return t, widget


@pytest.mark.parametrize("first", ["file", "widget"])
def test_file_and_widget_slots_coexist_in_either_attach_order(tmp_path, first):
    with Context() as ctx:
        setup_cell(ctx)
        path = tmp_path / "a.txt"
        widget = Value()
        if first == "file":
            ctx.a.mount(path, mode="rw", authority="cell")
            t = traitlet(ctx.a)
            t.link(widget)
        else:
            t = traitlet(ctx.a)
            t.link(widget)
            ctx.a.mount(path, mode="rw", authority="cell")
        report = sync(ctx)
        assert set(report) == {key("a", "file"), key("a", "widget")}
        assert report.in_sync
        assert ctx.a.mount.spec.driver == "file"
        assert traitlet(ctx.a) is t
        assert path.read_text().strip() == "1" and widget.value == 1
        ctx.a = 7
        sync(ctx)
        assert path.read_text().strip() == "7" and t.value == widget.value == 7


def test_two_sensing_slots_newest_accepted_write_wins_and_fans_out(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        path.write_text("4\n")
        sync(ctx)
        assert ctx.a.value == t.value == widget.value == 4
        widget.value = 8
        sync(ctx)
        assert ctx.a.value == 8 and path.read_text().strip() == "8"
        path.write_text("12\n")
        sync(ctx)
        assert ctx.a.value == t.value == widget.value == 12
        ctx.a = 16
        sync(ctx)
        assert path.read_text().strip() == "16" and widget.value == 16


def test_valid_widget_write_clears_file_sense_error_for_all_slots(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        path.write_text("invalid integer\n")
        report = sync(ctx)
        assert ctx.a.state == "failed" and isinstance(ctx.a.exception, str)
        assert isinstance(report[key("a", "file")]["sense_error"], MountError)
        assert ctx.mounts.errors[key("a", "file")]
        widget.value = 7
        report = sync(ctx)
        assert ctx.a.state == "complete" and ctx.a.value == 7
        assert ctx.a.exception is None
        assert all(status["sense_error"] is None for status in report.values())
        assert not ctx.mounts.errors
        assert path.read_text().strip() == "7" and t.value == 7


def test_valid_file_write_clears_widget_sense_error_for_all_slots(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        widget.value = "invalid integer"
        report = sync(ctx)
        assert ctx.a.state == "failed"
        assert isinstance(report[key("a", "widget")]["sense_error"], MountError)
        assert ctx.mounts.errors[key("a", "widget")]
        path.write_text("9\n")
        report = sync(ctx)
        assert ctx.a.value == t.value == widget.value == 9
        assert ctx.a.exception is None
        assert all(status["sense_error"] is None for status in report.values())
        assert not ctx.mounts.errors


def test_user_value_write_clears_each_slot_sense_error(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        widget.value = "invalid"
        sync(ctx)
        assert ctx.a.state == "failed"
        ctx.a = 7
        report = sync(ctx)
        assert ctx.a.value == widget.value == 7
        assert all(status["sense_error"] is None for status in report.values())
        path.write_text("invalid\n")
        sync(ctx)
        assert ctx.a.state == "failed"
        ctx.a = 9
        report = sync(ctx)
        assert ctx.a.value == t.value == 9 and path.read_text().strip() == "9"
        assert all(status["sense_error"] is None for status in report.values())


def test_file_unmount_preserves_hub_and_hub_destroy_preserves_file(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        del ctx.a.mount
        assert ctx.a.mount.spec is None and ctx.a.mount.status is None
        assert traitlet(ctx.a) is t
        widget.value = 5
        report = sync(ctx)
        assert set(report) == {key("a", "widget")}
        assert ctx.a.value == 5
        assert path.read_text().strip() == "1"
        ctx.a.mount(path, mode="rw", authority="cell")
        t.destroy()
        assert ctx.a.mount.spec.driver == "file"
        ctx.a = 8
        report = sync(ctx)
        assert set(report) == {key("a", "file")}
        assert path.read_text().strip() == "8" and widget.value == 5


def test_file_surface_on_widget_only_cell_is_unattached_and_delete_is_noop():
    with Context() as ctx:
        setup_cell(ctx)
        t, widget = traitlet(ctx.a), Value()
        t.link(widget)
        assert ctx.a.mount.spec is None
        assert ctx.a.mount.status is None and ctx.a.mount.error is None
        del ctx.a.mount
        assert traitlet(ctx.a) is t
        widget.value = 8
        sync(ctx)
        assert ctx.a.value == 8


def test_file_slot_is_single_and_failed_second_file_attach_preserves_both(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        with pytest.raises(ValueError, match="Cell is already mounted; unmount first"):
            ctx.a.mount(tmp_path / "second.txt", mode="w")
        assert traitlet(ctx.a) is t
        assert ctx.a.mount.spec.path == str(path.resolve())
        ctx.a = 3
        sync(ctx)
        assert widget.value == 3 and path.read_text().strip() == "3"


@pytest.mark.parametrize("operation", ["delete", "reload", "empty_builder", "close"])
def test_whole_node_lifecycle_detaches_both_slots(tmp_path, operation):
    ctx = Context()
    try:
        path = tmp_path / "a.txt"
        t, widget = both(ctx, path)
        if operation == "delete":
            del ctx.a
        elif operation == "reload":
            ctx.set_graph(ctx.get_graph(), mounts=False)
        elif operation == "empty_builder":
            ctx.a = Cell(celltype="int")
        else:
            ctx.close()
        widget.value = 4
        assert t.value == 1
        t.value = 5
        assert widget.value == 4
        assert path.read_text().strip() == "1"
        if operation in {"reload", "empty_builder"}:
            assert not sync(ctx)
    finally:
        ctx.close()


def test_graph_serializes_only_file_and_reload_restores_only_file(tmp_path):
    with Context() as ctx:
        path = tmp_path / "a.txt"
        old, widget = both(ctx, path)
        graph = ctx.get_graph()
        node = next(node for node in graph["nodes"] if node["path"] == ["a"])
        assert node["mount"].get("driver", "file") == "file"
        assert "attachments" not in node
        assert "widget-" not in str(graph)
        ctx.set_graph(graph)
        report = sync(ctx)
        assert set(report) == {key("a", "file")}
        ctx.a = 7
        sync(ctx)
        assert path.read_text().strip() == "7" and widget.value == 1
        fresh = traitlet(ctx.a)
        assert fresh is not old
        sync(ctx)
        assert set(sync(ctx)) == {key("a", "file"), key("a", "widget")}


def test_widget_upgrade_preserves_existing_file_session(tmp_path):
    with Context() as ctx:
        setup_cell(ctx)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="rw", authority="cell")
        t = traitlet(ctx.a)
        before = ctx._mount_sessions[key("a", "file")]
        t.link(Value())
        sync(ctx)
        assert ctx._mount_sessions[key("a", "file")] is before
        assert ctx.a.mount.spec.driver == "file"


def test_connected_output_supports_file_and_hub_refuses_only_widget_upgrade(tmp_path):
    with Context() as ctx:
        setup_cell(ctx, "source", 2)
        ctx.a = ctx.source
        ctx.compute(timeout=30)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="w")
        t = traitlet(ctx.a)
        widget = Value()
        t.connect(widget)
        out = output(ctx.a)
        sync(ctx)
        with pytest.raises(AuthorityError):
            t.link(Value())
        assert traitlet(ctx.a) is t and ctx.a.mount.spec.driver == "file"
        ctx.source = 6
        ctx.compute(timeout=30)
        sync(ctx)
        assert path.read_text().strip() == "6" and widget.value == 6
        assert "6" in str(out.output_instance.outputs)


def test_removing_one_sensing_slot_does_not_release_other_topology_guard(tmp_path):
    with Context() as ctx:
        t, widget = both(ctx, tmp_path / "a.txt")
        setup_cell(ctx, "source", 9)
        del ctx.a.mount
        with pytest.raises(AuthorityError):
            ctx.a = ctx.source
        with pytest.raises(AuthorityError):
            ctx.a.set_checksum(None)
        t.destroy()
        ctx.a = ctx.source
        ctx.compute(timeout=30)
        assert ctx.a.value == 9


def test_conflict_detector_and_clear_are_isolated_between_slots(tmp_path):
    with Context() as ctx:
        setup_cell(ctx)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="w")
        t = traitlet(ctx.a)
        for value in (10, 11, 12):
            t.value = value
            sync(ctx)
        assert isinstance(t.error, ConflictError)
        assert ctx.a.mount.error is None
        assert set(ctx.mounts.errors) == {key("a", "widget")}
        ctx.a.mount.clear_error()
        assert isinstance(t.error, ConflictError)
        ctx.a = 6
        sync(ctx)
        assert path.read_text().strip() == "6"
        t.clear_error()
        report = sync(ctx)
        assert t.value == 6 and not ctx.mounts.errors and report.in_sync


@pytest.fixture
def widget_deliveries(monkeypatch):
    """Observe transport calls without altering the dispatcher's clock."""
    calls = []
    original = WidgetDriver.deliver
    def record(driver, reg, delivery):
        calls.append((id(driver), time.monotonic(), delivery.lease.checksum.resolve("int")))
        return original(driver, reg, delivery)
    monkeypatch.setattr(WidgetDriver, "deliver", record)
    return calls


def test_delivery_spacing_includes_initial_delivery_and_coalesces_pending(widget_deliveries):
    with Context() as ctx:
        setup_cell(ctx, value=0)
        t = traitlet(ctx.a)
        for value in range(1, 11):
            ctx.a = value
        sync(ctx)
        values = [entry[2] for entry in widget_deliveries]
        assert values == [0, 10]
        times = [entry[1] for entry in widget_deliveries]
        assert times[1] - times[0] >= 2 / 3 - 0.005
        assert t.value == 10


def test_sync_timeout_respects_delivery_spacing_and_does_not_cancel_pending(widget_deliveries):
    with Context() as ctx:
        setup_cell(ctx, value=1)
        t = traitlet(ctx.a)
        ctx.a = 2
        with pytest.raises(TimeoutError):
            ctx.mounts.sync(timeout=0.1)
        assert t.value == 1
        sync(ctx)
        assert t.value == 2
        assert widget_deliveries[-1][1] - widget_deliveries[0][1] >= 2 / 3 - 0.005


def test_spacing_is_per_session_without_a_rolling_window_cap(widget_deliveries):
    with Context() as ctx:
        setup_cell(ctx, value=0)
        t = traitlet(ctx.a)
        for value in (1, 2, 3, 4):
            ctx.a = value
            sync(ctx)
        times = [entry[1] for entry in widget_deliveries]
        assert len(times) == 5
        assert all(b - a >= 2 / 3 - 0.005 for a, b in zip(times, times[1:]))
        assert times[-1] - times[0] < 5
        assert t.value == 4


def test_different_widget_sessions_deliver_independently(widget_deliveries):
    with Context() as ctx:
        setup_cell(ctx, "a", 0)
        setup_cell(ctx, "b", 0)
        ta, tb = traitlet(ctx.a), traitlet(ctx.b)
        ctx.a = 1
        ctx.b = 2
        sync(ctx)
        grouped = {}
        for driver, timestamp, value in widget_deliveries:
            grouped.setdefault(driver, []).append((timestamp, value))
        assert len(grouped) == 2
        last = [entries[-1][0] for entries in grouped.values()]
        assert abs(last[0] - last[1]) < 0.5
        assert ta.value == 1 and tb.value == 2
        assert all(entries[-1][0] - entries[0][0] >= 2 / 3 - 0.005 for entries in grouped.values())


def test_widget_sensing_is_not_delayed_by_cell_delivery_spacing():
    with Context() as ctx:
        setup_cell(ctx)
        widget = Value()
        t = traitlet(ctx.a)
        t.link(widget)
        sync(ctx)
        widget.value = 8
        sync(ctx)
        assert ctx.a.value == t.value == 8


def test_detach_with_throttled_delivery_discards_old_pending_value():
    with Context() as ctx:
        setup_cell(ctx)
        old = traitlet(ctx.a)
        widget = Value()
        old.connect(widget)
        ctx.a = 7
        old.destroy()
        fresh = traitlet(ctx.a)
        sync(ctx)
        assert fresh.value == 7 and fresh is not old
        before = widget.value
        ctx.a = 9
        sync(ctx)
        assert fresh.value == 9 and widget.value == before


@pytest.fixture
def file_deliveries(monkeypatch):
    from seamless_workflow.attachments.fs.service import FileSystemService
    calls = []
    original = FileSystemService.deliver
    def record(service, reg, delivery):
        calls.append((id(reg), time.monotonic(), delivery.lease.checksum.resolve("int")))
        return original(service, reg, delivery)
    monkeypatch.setattr(FileSystemService, "deliver", record)
    return calls


def test_file_delivery_spacing_and_pending_coalescing(tmp_path, file_deliveries):
    with Context() as ctx:
        setup_cell(ctx, value=0)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="w")
        for value in range(1, 11):
            ctx.a = value
        sync(ctx)
        assert [entry[2] for entry in file_deliveries] == [0, 10]
        assert file_deliveries[-1][1] - file_deliveries[0][1] >= 2 / 3 - 0.005
        assert path.read_text().strip() == "10"


def test_file_and_widget_delivery_clocks_are_independent_on_same_node(tmp_path, file_deliveries, widget_deliveries):
    with Context() as ctx:
        setup_cell(ctx, value=0)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="w")
        t = traitlet(ctx.a)
        ctx.a = 8
        sync(ctx)
        assert file_deliveries[-1][2] == widget_deliveries[-1][2] == 8
        assert abs(file_deliveries[-1][1] - widget_deliveries[-1][1]) < 0.5
        for calls in (file_deliveries, widget_deliveries):
            assert calls[-1][1] - calls[0][1] >= 2 / 3 - 0.005
        assert t.value == 8 and path.read_text().strip() == "8"


def test_async_barrier_includes_both_slots_and_respects_pending_delivery(tmp_path):
    import asyncio
    with Context() as ctx:
        t, widget = both(ctx, tmp_path / "a.txt")
        ctx.a = 8
        async def settle():
            return await ctx.mounts.synchronization(timeout=10)
        report = asyncio.run(settle())
        assert set(report) == {key("a", "file"), key("a", "widget")}
        assert report.in_sync and t.value == widget.value == 8


def test_nonpersistent_file_unmount_cleanup_does_not_detach_widget(tmp_path):
    with Context() as ctx:
        setup_cell(ctx)
        path = tmp_path / "a.txt"
        ctx.a.mount(path, mode="w", persistent=False)
        t, widget = traitlet(ctx.a), Value()
        t.link(widget)
        sync(ctx)
        assert path.exists()
        del ctx.a.mount
        assert not path.exists()
        widget.value = 6
        sync(ctx)
        assert ctx.a.value == t.value == 6


def test_missing_payload_reports_delivery_error_for_both_slots(tmp_path):
    from seamless import Checksum
    with Context() as ctx:
        ctx.a = Cell(celltype="int")
        ctx.a.set_checksum(Checksum(bytes(range(32))))
        ctx.a.mount(tmp_path / "a.txt", mode="w")
        t = traitlet(ctx.a)
        report = sync(ctx)
        expected = {key("a", "file"), key("a", "widget")}
        assert set(report) == set(ctx.mounts.errors) == expected
        assert all(isinstance(status["error"], MountError) for status in report.values())
        assert isinstance(t.error, MountError) and isinstance(ctx.a.mount.error, MountError)
        assert ctx.a.exception is None
        ctx.a = 7
        report = sync(ctx)
        assert report.in_sync and not ctx.mounts.errors


def test_internal_manual_driver_uses_same_tuple_report_and_error_keys(monkeypatch):
    from seamless_workflow.attachments.manual import ManualDriver
    with Context() as ctx:
        ctx.a = Cell(celltype="int")
        driver = ManualDriver().attach(ctx.a, 3, mode="r")
        request_cut = driver.request_cut
        def answer_cut(reg, cut_id):
            request_cut(reg, cut_id)
            driver.cut()
        monkeypatch.setattr(driver, "request_cut", answer_cut)
        report = sync(ctx)
        assert set(report) == {key("a", "manual")}
        driver.observe(rejected="invalid integer")
        report = sync(ctx)
        assert isinstance(report[key("a", "manual")]["sense_error"], MountError)
        assert set(ctx.mounts.errors) == {key("a", "manual")}


def test_close_waits_for_paced_persistent_file_delivery_without_stalling(tmp_path):
    """A subprocess bounds a broken close without hanging fixture cleanup."""
    import json
    import subprocess
    import sys
    path = tmp_path / "close-final.txt"
    script = '''
import json
import sys
import time
from pathlib import Path
from seamless import Cell
from seamless_workflow import Context

path = Path(sys.argv[1])
ctx = Context()
ctx.a = Cell(celltype="int")
ctx.a.set(1)
start = time.monotonic()
ctx.a.mount(path, mode="w", persistent=True)
assert path.read_text().strip() == "1"
ctx.a = 2
close_start = time.monotonic()
ctx.close()
print(json.dumps({"value": path.read_text().strip(), "elapsed": time.monotonic() - start,
                  "close_elapsed": time.monotonic() - close_start}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    result_data = json.loads(result.stdout.strip().splitlines()[-1])
    assert result_data["value"] == "2"
    assert result_data["elapsed"] >= 2 / 3 - 0.005
    assert result_data["close_elapsed"] < 5
    assert "ClosedContextError" not in result.stderr
