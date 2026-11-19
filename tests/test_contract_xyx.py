"""Regressions for no-hashserver Context recovery (workflow-context.md)."""

import asyncio
import threading

import pytest
import traitlets

from seamless import Buffer, CacheMissError, Cell, Checksum
from seamless_workflow import Context
from seamless_workflow.jupyter import traitlet


def identity(value):
    return value


def produce(value):
    return "prepared:" + value


def use(payload, suffix):
    return payload + suffix


class ValueWidget(traitlets.HasTraits):
    value = traitlets.Any(default_value=None, allow_none=True)


def _no_hashserver(monkeypatch):
    from seamless_remote import buffer_remote

    monkeypatch.setattr(buffer_remote, "has_read_server", lambda: False)
    monkeypatch.setattr(buffer_remote, "has_write_server", lambda: False)


def _evicted_text_checksum(value):
    from helpers.reference_lifecycle import force_expiry

    buffer = Buffer(value, "text")
    checksum = buffer.get_checksum()
    del buffer
    force_expiry(checksum)
    return checksum


def _mounted_text_cell(make_context, tmp_path):
    context = make_context()
    context.a = Cell("text")
    context.a.set("last good value")
    path = tmp_path / "value.txt"
    context.a.mount(path, mode="w")
    context.mounts.sync(timeout=10)
    assert path.read_text().strip() == "last good value"
    return context, path


def test_bound_transformers_fingertip_inputs_without_changing_build_options(
    make_context, monkeypatch
):
    _no_hashserver(monkeypatch)
    from seamless_transformer.transformation_class import Transformation
    from seamless_transformer.transformer_class import PythonBashBaseTransformer

    built_meta = []
    require_values = []
    original_build = PythonBashBaseTransformer._build_from_frozen
    original_computation = Transformation.computation

    def record_build(self, frozen, *args, **kwargs):
        built_meta.append(dict(frozen.meta))
        return original_build(self, frozen, *args, **kwargs)

    async def record_computation(self, require_value=False):
        require_values.append(require_value)
        return await original_computation(self, require_value=require_value)

    monkeypatch.setattr(PythonBashBaseTransformer, "_build_from_frozen", record_build)
    monkeypatch.setattr(Transformation, "computation", record_computation)

    ctx = make_context(expression_execution="local")
    first = "first-xyx-value"
    ctx.source = Cell("str")
    ctx.source.set(first)
    ctx.produce = produce
    ctx.produce.local = True
    ctx.produce.celltypes.value = "str"
    ctx.produce.celltypes.result = "str"
    ctx.produce.pins.value = ctx.source
    ctx.intermediate = ctx.produce.result
    ctx.use = use
    ctx.use.local = True
    ctx.use.celltypes.payload = "str"
    ctx.use.celltypes.suffix = "str"
    ctx.use.celltypes.result = "str"
    ctx.use.pins.payload = ctx.intermediate
    ctx.use.pins.suffix = "!"
    assert ctx.use.allow_input_fingertip is False
    ctx.compute(timeout=20)
    old_checksum = ctx.intermediate.checksum
    assert ctx.use.result.value == "prepared:" + first + "!"

    ctx.source.set("other value")
    ctx.use.pins.suffix = "?"
    ctx.compute(timeout=20)
    ctx.prune()
    from helpers.reference_lifecycle import force_expiry

    force_expiry(old_checksum)
    with pytest.raises(CacheMissError):
        old_checksum.resolve()

    ctx.source.set(first)
    ctx.compute(timeout=20)
    assert ctx.use.result.value == "prepared:" + first + "?"
    assert built_meta and all(meta.get("allow_input_fingertip") is True for meta in built_meta)
    assert require_values and all(value is False for value in require_values)
    # Context submissions override the setting operationally; direct builds do not.
    assert ctx.use.allow_input_fingertip is False
    assert ctx.use.build().allow_input_fingertip is False


def test_bound_input_fingertip_failure_keeps_its_category(make_context, monkeypatch):
    _no_hashserver(monkeypatch)
    from seamless import FingertipCategory

    ctx = make_context(expression_execution="local")
    ctx.source = Cell("str")
    ctx.source.set("working value")
    ctx.produce = produce
    ctx.produce.local = True
    ctx.produce.celltypes.value = "str"
    ctx.produce.celltypes.result = "str"
    ctx.produce.pins.value = ctx.source
    ctx.compute(timeout=20)

    ctx.source.set_checksum(Checksum(bytes(range(32))))
    with pytest.raises(CacheMissError) as excinfo:
        ctx.produce.run()
    assert excinfo.value.fingertip_category == FingertipCategory.MATERIALIZATION


@pytest.mark.parametrize("read_server,write_server", [(True, False), (False, True)])
def test_configured_hashserver_keeps_ordinary_bound_execution(
    make_context, monkeypatch, read_server, write_server
):
    from seamless_remote import buffer_remote
    from seamless_transformer.transformation_class import Transformation
    from seamless_transformer.transformer_class import PythonBashBaseTransformer

    monkeypatch.setattr(buffer_remote, "has_read_server", lambda: read_server)
    monkeypatch.setattr(buffer_remote, "has_write_server", lambda: write_server)
    built_meta = []
    require_values = []
    original_build = PythonBashBaseTransformer._build_from_frozen
    original_computation = Transformation.computation

    def record_build(self, frozen, *args, **kwargs):
        built_meta.append(dict(frozen.meta))
        return original_build(self, frozen, *args, **kwargs)

    async def record_computation(self, require_value=False):
        require_values.append(require_value)
        return await original_computation(self, require_value=require_value)

    monkeypatch.setattr(PythonBashBaseTransformer, "_build_from_frozen", record_build)
    monkeypatch.setattr(Transformation, "computation", record_computation)

    ctx = make_context(expression_execution="local")
    ctx.tf = identity
    ctx.tf.pins.value = "cached locally"
    ctx.compute(timeout=20)

    assert built_meta and all(not meta.get("allow_input_fingertip", False) for meta in built_meta)
    assert require_values and all(value is True for value in require_values)
    assert ctx.tf.build().allow_input_fingertip is False


def test_attached_cell_waits_for_its_missing_buffer_before_delivery(
    make_context, monkeypatch, tmp_path
):
    _no_hashserver(monkeypatch)
    ctx, path = _mounted_text_cell(make_context, tmp_path)
    widget = ValueWidget()
    traitlet(ctx.a).link(widget)
    ctx.mounts.sync(timeout=10)
    assert widget.value == "last good value"
    checksum = _evicted_text_checksum("recovered value")
    started = threading.Event()
    resume = threading.Event()
    original_fingertip = Checksum.fingertip

    async def delayed_recovery(candidate, celltype=None):
        if candidate == checksum:
            started.set()
            assert await asyncio.to_thread(resume.wait, 10)
            return Buffer("recovered value", "text")
        return await original_fingertip(candidate, celltype)

    monkeypatch.setattr(Checksum, "fingertip", delayed_recovery)
    ctx.a.set_checksum(checksum)
    try:
        assert started.wait(5), "Context did not start the attached cell fingertip"
        assert ctx.a.state == "waiting"
        assert ctx.a.checksum is None
        assert ctx.a.exception is None
        assert path.read_text().strip() == "last good value"
        assert widget.value == "last good value"
        with pytest.raises(TimeoutError):
            ctx.compute(timeout=0.05)
        with pytest.raises(TimeoutError):
            ctx.mounts.sync(timeout=0.05)
    finally:
        resume.set()

    ctx.compute(timeout=15)
    ctx.mounts.sync(timeout=15)
    assert ctx.a.state == "complete"
    assert ctx.a.checksum == checksum
    assert path.read_text().strip() == "recovered value"
    assert widget.value == "recovered value"


def test_failed_attached_cell_fingertip_applies_checksum_and_leaves_delivery_error(
    make_context, monkeypatch, tmp_path
):
    _no_hashserver(monkeypatch)
    ctx, path = _mounted_text_cell(make_context, tmp_path)
    checksum = Checksum(bytes(range(32)))
    started = threading.Event()
    resume = threading.Event()

    async def failed_recovery(candidate, celltype=None):
        if candidate == checksum:
            started.set()
            assert await asyncio.to_thread(resume.wait, 10)
            raise CacheMissError(candidate)
        raise CacheMissError(candidate)

    monkeypatch.setattr(Checksum, "fingertip", failed_recovery)
    ctx.a.set_checksum(checksum)
    try:
        assert started.wait(5), "Context did not start the attached cell fingertip"
        assert ctx.a.state == "waiting"
        assert ctx.a.checksum is None
        assert path.read_text().strip() == "last good value"
    finally:
        resume.set()

    report = ctx.mounts.sync(timeout=15)
    assert ctx.a.state == "complete"
    assert ctx.a.checksum == checksum
    assert ctx.a.exception is None
    assert isinstance(report[(("a",), "file")]["error"], Exception)
    assert path.read_text().strip() == "last good value"


def test_superseded_fingertip_cannot_apply_when_same_checksum_is_requested_again(
    make_context, monkeypatch, tmp_path
):
    _no_hashserver(monkeypatch)
    ctx, path = _mounted_text_cell(make_context, tmp_path)
    checksum = _evicted_text_checksum("returning value")
    started = [threading.Event(), threading.Event()]
    resume = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    attempts = 0

    async def delayed_recovery(candidate, celltype=None):
        nonlocal attempts
        if candidate == checksum:
            attempt = attempts
            attempts += 1
            started[attempt].set()
            try:
                assert await asyncio.to_thread(resume[attempt].wait, 10)
                return Buffer("returning value", "text")
            finally:
                finished[attempt].set()
        raise CacheMissError(candidate)

    monkeypatch.setattr(Checksum, "fingertip", delayed_recovery)
    ctx.a.set_checksum(checksum)
    assert started[0].wait(5), "Context did not start the first fingertip"
    assert ctx.a.state == "waiting" and ctx.a.checksum is None

    ctx.a.set("middle value")
    ctx.mounts.sync(timeout=15)
    middle_checksum = ctx.a.checksum
    assert path.read_text().strip() == "middle value"

    ctx.a.set_checksum(checksum)
    assert started[1].wait(5), "Context did not start a new fingertip for the returning checksum"
    assert ctx.a.state == "waiting" and ctx.a.checksum is None
    resume[0].set()
    assert finished[0].wait(5)
    assert ctx.a.state == "waiting" and ctx.a.checksum is None
    assert path.read_text().strip() == "middle value"

    resume[1].set()
    ctx.mounts.sync(timeout=15)
    assert ctx.a.state == "complete"
    assert ctx.a.checksum == checksum
    assert ctx.a.checksum != middle_checksum
    assert path.read_text().strip() == "returning value"
