import pytest


def test_evicted_transform_cache_result_is_recomputed_for_plain_process_pin(
    make_context, monkeypatch
):
    from seamless import CacheMissError, Cell
    from seamless_remote import buffer_remote
    from seamless.transformer import spawn
    from seamless_transformer.worker import shutdown_workers
    from helpers.reference_lifecycle import force_expiry

    monkeypatch.setattr(buffer_remote, "has_read_server", lambda: False)
    monkeypatch.setattr(buffer_remote, "has_write_server", lambda: False)

    def fit_echo(value):
        return {"value": value}

    def parse_best_fits(fit_echo, gate):
        return {"value": fit_echo["value"], "gate": gate}

    spawn(1)
    ctx = make_context(expression_execution="local")
    try:
        ctx.source = Cell("plain")
        ctx.fit_echo = fit_echo
        ctx.fit_echo.local = True
        ctx.fit_echo.celltypes.value = "plain"
        ctx.fit_echo.celltypes.result = "plain"
        ctx.fit_echo.pins.value = ctx.source

        ctx.gate = Cell("plain")
        ctx.parse_best_fits = parse_best_fits
        ctx.parse_best_fits.local = True
        ctx.parse_best_fits.celltypes.fit_echo = "plain"
        ctx.parse_best_fits.celltypes.gate = "plain"
        ctx.parse_best_fits.celltypes.result = "plain"
        ctx.parse_best_fits.pins.fit_echo = ctx.fit_echo.result
        ctx.parse_best_fits.pins.gate = ctx.gate

        ctx.source.set("X")
        ctx.compute(timeout=30)
        x_checksum = ctx._graph.nodes[("fit_echo",)].current_checksum
        x_tf = ctx._runtime.current_runs[("fit_echo",)].identity_checksum

        ctx.source.set("Y")
        ctx.compute(timeout=30)
        ctx.prune()
        force_expiry(x_checksum)
        with pytest.raises(CacheMissError):
            x_checksum.resolve()

        ctx.source.set("X")
        ctx.compute(timeout=30)
        restored = ctx._graph.nodes[("fit_echo",)].current_checksum
        restored_tf = ctx._runtime.current_runs[("fit_echo",)].identity_checksum
        assert restored == x_checksum
        assert restored_tf == x_tf

        ctx.gate.set("go")
        ctx.compute(timeout=30)
        assert ctx.parse_best_fits.result.value == {"value": "X", "gate": "go"}
    finally:
        ctx.close()
        shutdown_workers()


def test_folder_root_input_is_materialized_for_spawned_worker(
    make_context, monkeypatch
):
    from seamless import Cell
    from seamless_remote import buffer_remote
    from seamless.transformer import spawn
    from seamless_transformer.worker import shutdown_workers

    monkeypatch.setattr(buffer_remote, "has_read_server", lambda: False)
    monkeypatch.setattr(buffer_remote, "has_write_server", lambda: False)

    def make_folder(value):
        return {"content.txt": value.encode()}

    def consume_folder(folder):
        return "worker received folder"

    spawn(1)
    ctx = make_context(expression_execution="local")
    try:
        ctx.source = Cell("plain")

        ctx.folder_producer = make_folder
        ctx.folder_producer.local = True
        ctx.folder_producer.celltypes.value = "plain"
        ctx.folder_producer.celltypes.result = "folder"
        ctx.folder_producer.pins.value = ctx.source

        ctx.folder_consumer = consume_folder
        ctx.folder_consumer.local = True
        ctx.folder_consumer.celltypes.folder = "folder"
        ctx.folder_consumer.celltypes.result = "plain"
        ctx.folder_consumer.pins.folder = ctx.folder_producer.result

        ctx.source.set("X")
        ctx.compute(timeout=30)
        assert ctx.folder_consumer.result.value == "worker received folder"
    finally:
        ctx.close()
        shutdown_workers()
