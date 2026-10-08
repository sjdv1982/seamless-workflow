"""Phase A widget contract acceptance tests, using real traitlets and ipywidgets.

The public barrier is mounts.sync; private transport access is restricted to
controlling the debounce interval in interleaving tests.
"""
import gc
import threading
import time
import weakref
from pathlib import Path

import pytest
import traitlets
import ipywidgets as widgets

from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.diagnostics import record_attachments
from seamless_workflow.errors import AuthorityError, ClosedContextError, NodeError
from seamless_workflow.jupyter import output, traitlet


class Value(traitlets.HasTraits):
    value = traitlets.Any(default_value=None, allow_none=True)


def add(a, b):
    return a + b


def fail_on_two(x):
    if x == 2:
        raise RuntimeError("boom")
    return x * 10


def sync(ctx):
    return ctx.mounts.sync(timeout=10)


def int_cell(ctx, name="a", value=1):
    setattr(ctx, name, Cell(celltype="int"))
    getattr(ctx, name).set(value)


def transport(ctx, name="a"):
    return ctx._mount_sessions[(name,)].registration.service


def test_section_seven_two_sliders_transformer_and_output():
    with Context() as ctx:
        int_cell(ctx, "a", 2)
        int_cell(ctx, "b", 3)
        ctx.tf = add
        ctx.tf.pins.a = ctx.a
        ctx.tf.pins.b = ctx.b
        ctx.c = ctx.tf.result
        a, b = widgets.IntSlider(), widgets.IntSlider()
        traitlet(ctx.a).link(a)
        traitlet(ctx.b).link(b)
        out = output(ctx.c)
        a.value, b.value = 7, 8
        sync(ctx)
        ctx.compute(timeout=30)
        sync(ctx)
        assert ctx.c.value == 15
        assert out.output_instance.outputs
        assert "15" in str(out.output_instance.outputs)


def test_existing_cell_wins_initial_widget_value_and_round_trip():
    with Context() as ctx:
        int_cell(ctx, value=12)
        widget = widgets.IntSlider(value=4)
        t = traitlet(ctx.a)
        assert t.value == 12
        t.link(widget)
        sync(ctx)
        assert widget.value == ctx.a.value == 12
        widget.value = 17
        sync(ctx)
        assert ctx.a.value == 17
        ctx.a = 23
        sync(ctx)
        assert widget.value == t.value == 23


@pytest.mark.parametrize("value", [19, None])
def test_empty_cell_takes_widget_value_including_initial_absence(value):
    with Context() as ctx:
        ctx.a = Cell(celltype="plain")
        widget = Value(value=value)
        t = traitlet(ctx.a)
        assert t.value is None
        t.link(widget)
        sync(ctx)
        assert ctx.a.state == "complete"
        assert ctx.a.value == value


def test_one_hub_multiple_links_and_individual_unlink():
    with Context() as ctx:
        int_cell(ctx)
        t = traitlet(ctx.a)
        assert traitlet(ctx.a) is t
        a, b = Value(), Value()
        link_a, link_b = t.link(a), t.link(b)
        assert link_a is not None and link_b is not None
        a.value = 7
        sync(ctx)
        assert t.value == b.value == ctx.a.value == 7
        link_a.unlink()
        b.value = 9
        sync(ctx)
        assert ctx.a.value == 9 and a.value == 7


def test_session_retains_hub_link_and_widget_without_return_values():
    with Context() as ctx:
        int_cell(ctx)
        widget = Value()
        reference = weakref.ref(widget)
        traitlet(ctx.a).link(widget)
        del widget
        gc.collect()
        assert reference() is not None
        ctx.a = 8
        sync(ctx)
        assert reference().value == 8


def test_observe_calls_immediately_and_delivers_on_worker_thread():
    with Context() as ctx:
        int_cell(ctx, value=3)
        t = traitlet(ctx.a)
        events = []
        t.observe(lambda change: events.append((change["new"], threading.get_ident())), names="value")
        assert events[0][0] == 3
        caller = threading.get_ident()
        ctx.a = 4
        sync(ctx)
        assert events[-1][0] == 4
        assert events[-1][1] != caller


def test_observe_empty_hub_has_no_initial_call():
    with Context() as ctx:
        ctx.a = Cell(celltype="int")
        events = []
        traitlet(ctx.a).observe(events.append, names="value")
        assert events == []
        ctx.a = 5
        sync(ctx)
        assert events[-1]["new"] == 5


def test_connected_cell_connect_and_refused_upgrade_preserve_links():
    with Context() as ctx:
        int_cell(ctx, "source", 2)
        ctx.a = ctx.source
        ctx.compute(timeout=30)
        t = traitlet(ctx.a)
        widget = Value()
        t.connect(widget)
        sync(ctx)
        with pytest.raises(AuthorityError, match="Sensing mount cannot have incoming edges; unmount first"):
            t.link(Value(value=17))
        assert traitlet(ctx.a) is t
        ctx.source = 6
        ctx.compute(timeout=30)
        sync(ctx)
        assert widget.value == t.value == 6
        widget.value = 9
        sync(ctx)
        assert ctx.a.value == t.value == 6


def test_connect_coercion_never_changes_cell():
    with Context() as ctx:
        int_cell(ctx, value=50)
        widget = widgets.IntSlider(max=30)
        traitlet(ctx.a).connect(widget)
        sync(ctx)
        assert widget.value == 30 and ctx.a.value == 50


def test_link_coercion_converges_on_attach_and_delivery():
    with Context() as ctx:
        int_cell(ctx, value=50)
        widget = widgets.IntSlider(max=30)
        traitlet(ctx.a).link(widget)
        sync(ctx)
        assert widget.value == ctx.a.value == 30
        ctx.a = 70
        sync(ctx)
        assert widget.value == ctx.a.value == 30


def test_none_never_propagates_in_either_link_direction():
    with Context() as ctx:
        ctx.a = Cell(celltype="plain")
        ctx.a.set(4)
        t, widget = traitlet(ctx.a), Value()
        t.link(widget)
        widget.value = None
        sync(ctx)
        assert ctx.a.value == t.value == 4
        ctx.a = None
        sync(ctx)
        assert t.value is None
        widget.value = 8
        sync(ctx)
        assert ctx.a.value == 8
        ctx.a = None
        sync(ctx)
        assert t.value is None and widget.value == 8


def test_invalid_widget_value_fails_cell_and_valid_value_recovers():
    with Context() as ctx:
        int_cell(ctx, value=3)
        widget = widgets.Text(value="3")
        t = traitlet(ctx.a)
        t.link(widget)
        widget.value = "not an integer"
        report = sync(ctx)
        assert ctx.a.state == "failed" and isinstance(ctx.a.exception, str)
        assert "widget-" in ctx.a.exception
        assert report[("a",)]["sense_error"] is not None
        assert t.error is None
        widget.value = "11"
        sync(ctx)
        assert ctx.a.state == "complete" and ctx.a.value == 11


def test_refusing_target_does_not_fail_other_links_or_delivery(caplog):
    with Context() as ctx:
        ctx.a = Cell(celltype="plain")
        ctx.a.set("first")
        t = traitlet(ctx.a)
        refusing, accepting = widgets.IntSlider(), Value()
        t.connect(refusing)
        t.connect(accepting)
        ctx.a = "second"
        sync(ctx)
        assert accepting.value == t.value == "second"
        assert t.error is None
        assert caplog.records


def test_rapid_changes_are_one_debounced_sense():
    with Context() as ctx:
        int_cell(ctx, value=0)
        widget = widgets.IntSlider()
        traitlet(ctx.a).link(widget)
        sync(ctx)
        with record_attachments(ctx) as log:
            for value in range(1, 11):
                widget.value = value
            time.sleep(0.3)
            sync(ctx)
            foreign = [event for event in log.entries() if event[2] == "observation" and dict(event[3]).get("classification") == "foreign"]
        assert ctx.a.value == 10
        assert len(foreign) == 1


def test_cut_flushes_debounce_and_compute_is_graph_only():
    with Context() as ctx:
        int_cell(ctx)
        widget = Value()
        traitlet(ctx.a).link(widget)
        sync(ctx)
        transport(ctx).debounce = 60
        widget.value = 8
        ctx.compute(timeout=10)
        assert ctx.a.value == 1
        sync(ctx)
        assert ctx.a.value == 8


def test_pending_widget_change_beats_incoming_cell_delivery():
    with Context() as ctx:
        int_cell(ctx)
        widget = Value()
        traitlet(ctx.a).link(widget)
        sync(ctx)
        transport(ctx).debounce = 60
        widget.value = 8
        ctx.a = 3
        sync(ctx)
        assert ctx.a.value == widget.value == 8


def test_no_downgrade_after_unlink_and_destroy_allows_incoming_edge():
    with Context() as ctx:
        int_cell(ctx)
        int_cell(ctx, "source", 9)
        t = traitlet(ctx.a)
        t.link(Value()).unlink()
        with pytest.raises(AuthorityError, match="Sensing mount is the producer; unmount first"):
            ctx.a = ctx.source
        t.destroy()
        ctx.a = ctx.source
        ctx.compute(timeout=30)
        assert ctx.a.value == 9


@pytest.mark.parametrize("operation", ["destroy", "unmount", "reload", "close", "delete", "empty_builder"])
def test_every_detach_unlinks_and_old_hub_is_inert(operation):
    ctx = Context()
    try:
        int_cell(ctx)
        t, a, b = traitlet(ctx.a), Value(), Value()
        t.link(a)
        t.connect(b)
        sync(ctx)
        if operation == "destroy":
            t.destroy()
        elif operation == "unmount":
            del ctx.a.mount
        elif operation == "reload":
            ctx.set_graph(ctx.get_graph())
        elif operation == "close":
            ctx.close()
        elif operation == "delete":
            del ctx.a
        else:
            ctx.a = Cell(celltype="int")
        a.value = 7
        assert t.value == 1 and b.value == 1
        t.value = 8
        assert a.value == 7 and b.value == 1
        if operation in {"destroy", "unmount", "reload", "empty_builder"}:
            fresh = traitlet(ctx.a)
            assert fresh is not t
            ctx.a = 9
            sync(ctx)
            assert a.value == 7 and b.value == 1
    finally:
        ctx.close()


def test_widget_session_never_serialized_and_file_mount_is_exclusive(tmp_path):
    with Context() as ctx:
        int_cell(ctx)
        t = traitlet(ctx.a)
        t.link(Value())
        assert all("mount" not in node for node in ctx.get_graph()["nodes"])
        with pytest.raises(ValueError, match="Cell is already mounted; unmount first"):
            ctx.a.mount(tmp_path / "value", mode="w")
        t.destroy()
        ctx.a.mount(tmp_path / "value", mode="w")
        with pytest.raises(ValueError, match="Cell is already mounted; unmount first"):
            traitlet(ctx.a)


@pytest.mark.parametrize("kind", ["standalone", "subpath", "result", "missing", "transformer"])
def test_scope_errors(kind):
    with Context() as ctx:
        ctx.a = {"b": 1}
        ctx.tf = add
        if kind == "standalone":
            cell = Cell(celltype="int")
        elif kind == "subpath":
            cell = ctx.a.b
        elif kind == "result":
            cell = ctx.tf.result
        elif kind == "missing":
            cell = ctx.missing
        else:
            cell = ctx.tf
        error = AttributeError if kind in {"standalone", "subpath", "result"} else NodeError
        message = "Widgets attach to whole Context cell nodes" if error is AttributeError else "Mounts require an existing whole cell node"
        with pytest.raises(error, match=message):
            traitlet(cell)


@pytest.mark.parametrize("celltype", ["checksum", "deepcell", "module"])
def test_unmountable_celltypes(celltype):
    with Context() as ctx:
        ctx.a = Cell(celltype=celltype)
        with pytest.raises(TypeError, match=f"Celltype '{celltype}' is not mountable"):
            traitlet(ctx.a)


def test_free_functions_do_not_reserve_cell_projection_names():
    with Context() as ctx:
        ctx.a = {"traitlet": 2, "output": 3}
        assert ctx.a.traitlet.value == 2
        assert ctx.a.output.value == 3


def test_celltype_frozen_and_clear_refused_while_hub_exists():
    with Context() as ctx:
        int_cell(ctx)
        traitlet(ctx.a)
        with pytest.raises(AuthorityError):
            ctx.a.set_checksum(None)
        with pytest.raises(ValueError, match="Mounted celltype cannot change; unmount first"):
            ctx.a.celltype = "float"


def test_failed_computation_keeps_widget_and_output_last_value():
    with Context() as ctx:
        ctx.tf = fail_on_two
        ctx.tf.pins.x = 1
        ctx.a = ctx.tf.result
        ctx.compute(timeout=30)
        widget = Value()
        traitlet(ctx.a).connect(widget)
        out = output(ctx.a)
        sync(ctx)
        before = out.output_instance.outputs
        assert widget.value == 10 and before
        ctx.tf.pins.x = 2
        ctx.compute(timeout=30)
        sync(ctx)
        assert ctx.a.state != "complete"
        assert widget.value == 10 and out.output_instance.outputs == before


@pytest.mark.parametrize("celltype,value,mimetype", [
    ("text", "hello", None), ("int", 7, None),
    ("plain", {"answer": 42}, None), ("python", "x = 1", None),
    ("bytes", b"abc", None), ("text", "<b>hi</b>", "text/html"),
    ("plain", {"answer": 42}, "application/json"),
])
def test_output_replaces_content_and_passes_layout(celltype, value, mimetype):
    with Context() as ctx:
        ctx.a = Cell(celltype=celltype)
        ctx.a.set(value)
        layout = widgets.Layout(width="200px")
        out = output(ctx.a, layout=layout, mimetype=mimetype)
        sync(ctx)
        assert out.output_instance.layout.width == "200px"
        assert len(out.output_instance.outputs) == 1
        assert "data" in out.output_instance.outputs[0]
        ctx.a.set(value)
        sync(ctx)
        assert len(out.output_instance.outputs) == 1
        if mimetype == "text/html":
            assert out.output_instance.outputs[0]["data"]["text/html"] == value


def test_output_empty_cell_and_invalid_mimetype():
    with Context() as ctx:
        ctx.a = Cell(celltype="int")
        out = output(ctx.a)
        assert out.output_instance.outputs == ()
        with pytest.raises(ValueError):
            output(ctx.a, mimetype="application/unsupported")
        ctx.a = 4
        sync(ctx)
        assert out.output_instance.outputs


def test_public_operations_after_close_raise_closed_context():
    ctx = Context()
    int_cell(ctx)
    cell = ctx.a
    t = traitlet(cell)
    ctx.close()
    for operation in (lambda: traitlet(cell), lambda: output(cell), t.destroy, t.clear_error):
        with pytest.raises(ClosedContextError):
            operation()


def test_real_kernel_notebook_no_deadlock():
    import nbformat
    from nbclient import NotebookClient
    notebook_path = Path(__file__).parent / "notebooks" / "widgets.ipynb"
    notebook = nbformat.read(notebook_path, as_version=4)
    NotebookClient(notebook, timeout=90, kernel_name="python3", resources={"metadata": {"path": str(notebook_path.parent)}}).execute()
    assert all(output.get("output_type") != "error" for cell in notebook.cells for output in cell.get("outputs", ()))


def test_output_hub_foreign_edits_reassert_trip_and_clear_error():
    from seamless_workflow.attachments import ConflictError
    with Context() as ctx:
        int_cell(ctx, value=1)
        t = traitlet(ctx.a)
        for value in (10, 11, 12):
            t.value = value
            sync(ctx)
        assert isinstance(t.error, ConflictError)
        assert t.status["state"] == "tripped"
        assert ctx.a.value == 1
        t.clear_error()
        sync(ctx)
        assert t.error is None and t.value == 1


def test_missing_payload_is_delivery_error_on_hub_not_cell():
    from seamless import Checksum
    from seamless_workflow.attachments import MountError
    with Context() as ctx:
        ctx.a = Cell(celltype="text")
        ctx.a.set_checksum(Checksum(bytes(range(32))))
        t = traitlet(ctx.a)
        report = sync(ctx)
        assert isinstance(t.error, MountError)
        assert "widget-" in str(t.error) and "CacheMissError" in str(t.error)
        assert ctx.a.exception is None
        assert isinstance(report[("a",)]["error"], MountError)
        ctx.a = "available"
        sync(ctx)
        assert t.value == "available" and t.error is None


def test_coerced_delivery_converges_every_link_regardless_of_link_order():
    with Context() as ctx:
        int_cell(ctx, value=10)
        t = traitlet(ctx.a)
        slider = widgets.IntSlider(max=30)
        text = widgets.IntText()
        t.link(slider)
        t.link(text)
        sync(ctx)
        ctx.a = 50
        sync(ctx)
        assert ctx.a.value == t.value == slider.value == text.value == 30


def test_destroyed_hub_cannot_inspect_detach_or_reset_replacement_hub():
    from seamless_workflow.attachments import ConflictError
    with Context() as ctx:
        int_cell(ctx, value=1)
        old = traitlet(ctx.a)
        old.destroy()
        fresh = traitlet(ctx.a)
        assert fresh is not old
        old.destroy()
        assert traitlet(ctx.a) is fresh
        assert old.status is None and old.error is None
        for value in (10, 11, 12):
            fresh.value = value
            sync(ctx)
        assert isinstance(fresh.error, ConflictError)
        error = fresh.error
        old.clear_error()
        assert fresh.error is error and fresh.status["state"] == "tripped"
        assert old.status is None and old.error is None
        fresh.clear_error()
        sync(ctx)
        assert fresh.value == 1 and fresh.error is None


def test_destroyed_hub_cannot_detach_replacement_file_mount(tmp_path):
    with Context() as ctx:
        int_cell(ctx, value=1)
        old = traitlet(ctx.a)
        old.destroy()
        path = tmp_path / "replacement.txt"
        ctx.a.mount(path, mode="w")
        spec = ctx.a.mount.spec
        old.destroy()
        assert ctx.a.mount.spec == spec
        assert old.status is None and old.error is None
        ctx.a = 8
        sync(ctx)
        assert path.read_text().strip() == "8"


def test_widget_edit_coercion_converges_source_linked_before_clamping_widget():
    with Context() as ctx:
        int_cell(ctx, value=10)
        t = traitlet(ctx.a)
        text = widgets.IntText()
        slider = widgets.IntSlider(max=30)
        t.link(text)
        t.link(slider)
        sync(ctx)
        text.value = 50
        sync(ctx)
        assert ctx.a.value == t.value == slider.value == text.value == 30
