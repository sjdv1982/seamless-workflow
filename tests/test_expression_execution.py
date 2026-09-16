"""Expression locality through a real Context and temporary cluster."""
import os
import subprocess
import sys
import textwrap
import uuid

import pytest


def _write_remote_config(workdir, *, backend, project):
    cluster = f"cluster-{uuid.uuid4().hex}"
    (workdir / "seamless.yaml").write_text(
        "\n".join(
            [
                "- clusters:",
                f"    {cluster}:",
                "      type: local",
                "      workers: 1",
                "      frontends:",
                "        - hashserver:",
                f"            bufferdir: {workdir / 'buffers'}",
                "            conda: seamless1",
                "            port_start: 10000",
                "            port_end: 19999",
                "          database:",
                f"            database_dir: {workdir / 'buffers'}",
                "            conda: seamless1",
                "            port_start: 20000",
                "            port_end: 29999",
                "          jobserver:",
                "            conda: seamless1",
                "            network_interface: 0.0.0.0",
                "            port_start: 20000",
                "            port_end: 29999",
                "          daskserver:",
                "            network_interface: 0.0.0.0",
                "            port_start: 20000",
                "            port_end: 29999",
                "      default_queue: default",
                "      queues:",
                "        default:",
                "          conda: seamless1",
                "          interactive: true",
                "          walltime: 10m",
                "          memory: 30000MB",
                f"- project: {project}",
                "- execution: remote",
                f"- remote: {backend}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (workdir / "seamless.profile.yaml").write_text(
        f"- cluster: {cluster}\n",
        encoding="utf-8",
    )


def test_context_projection_dispatches_hashserver_only_input(tmp_path):
    project = 'expression-auto-location-' + uuid.uuid4().hex
    _write_remote_config(tmp_path, backend='jobserver', project=project)
    script = textwrap.dedent('''
        import asyncio
        import seamless
        from seamless import Buffer, Checksum, Cell
        import seamless_config
        from seamless_workflow import Context
        from seamless.caching.buffer_cache import get_buffer_cache
        from seamless.checksum import expression as expression_mod
        from seamless.checksum.cached_calculate_checksum import checksum_cache
        from seamless_remote import buffer_remote, jobserver_remote

        seamless_config.init()
        try:
            source = Buffer({"value": "from jobserver"}, "plain")
            source_checksum = source.get_checksum()
            assert asyncio.run(buffer_remote.write_buffer(source_checksum, source))

            cache = get_buffer_cache()
            with cache.lock:
                cache.weak_cache.pop(source_checksum, None)
                cache.strong_cache.pop(source_checksum, None)
            checksum_cache.pop(source_checksum, None)
            expression_mod._expression_result_buffers.pop(source_checksum, None)
            expression_mod.get_expression_cache().clear()

            dispatches = []
            original_run_expression = jobserver_remote.run_expression
            async def record_dispatch(*args, **kwargs):
                dispatches.append((args, kwargs))
                return await original_run_expression(*args, **kwargs)
            jobserver_remote.run_expression = record_dispatch

            ctx = Context()
            ctx.source = Cell("plain")
            ctx.source.checksum = source_checksum
            ctx.projected = ctx.source["value"]
            ctx.compute(timeout=30)
            assert ctx.projected.value == "from jobserver", ctx.projected.exception
            assert len(dispatches) == 1, dispatches
            args, kwargs = dispatches[0]
            assert Checksum(args[0]) == source_checksum
            assert args[1:] == ("value", "plain", "plain")
            assert kwargs == {}
        finally:
            if "ctx" in locals():
                ctx.close()
            seamless.close()
        print("AUTO_EXPRESSION_JOBSERVER_OK")
    ''')
    proc = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                          capture_output=True, text=True, timeout=90,
                          env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'AUTO_EXPRESSION_JOBSERVER_OK' in proc.stdout


@pytest.mark.parametrize("backend", ["jobserver", "daskserver"])
@pytest.mark.parametrize("mode", ["standalone", "bound"])
def test_missing_input_through_jobserver_fails_and_recovers(tmp_path, mode, backend):
    project = 'expression-missing-recovery-' + uuid.uuid4().hex
    _write_remote_config(tmp_path, backend=backend, project=project)
    script = textwrap.dedent(f'''
        import asyncio
        import seamless
        from seamless import Buffer, CacheMissError, Cell, Checksum
        import seamless_config
        from seamless.caching import buffer_writer
        from seamless.caching.buffer_cache import get_buffer_cache
        from seamless.checksum import expression as expression_mod
        from seamless.checksum.cached_calculate_checksum import checksum_cache
        from seamless_remote import buffer_remote
        from seamless_workflow import Context

        def drop_buffer(checksum):
            checksum = Checksum(checksum)
            cache = get_buffer_cache()
            with cache.lock:
                cache.weak_cache.pop(checksum, None)
                cache.strong_cache.pop(checksum, None)
            checksum_cache.pop(checksum, None)
            expression_mod._expression_result_buffers.pop(checksum, None)

        source = Buffer({{"value": "recovered"}}, "plain")
        source_checksum = source.get_checksum()
        source_content = source.content
        buffer_writer.purge()
        drop_buffer(source_checksum)

        seamless_config.init()
        ctx = None
        try:
            if {mode!r} == "standalone":
                cell = Cell(
                    "str",
                    checksum=source_checksum,
                    input_celltype="plain",
                    path="value",
                )
            else:
                ctx = Context()
                ctx.source = Cell("plain")
                ctx.source.checksum = source_checksum
                ctx.projected = ctx.source["value"]
                ctx.projected.celltype = "str"
                cell = ctx.projected
                ctx.compute(timeout=30)

            assert cell.checksum is None
            assert cell.state == "failed"
            assert isinstance(cell.exception, CacheMissError), cell.exception
            assert cell.exception.checksum == source_checksum
            assert "Traceback" not in str(cell.exception)

            source = Buffer(source_content, checksum=source_checksum)
            assert asyncio.run(buffer_remote.write_buffer(source_checksum, source))
            cell.clear_exception()
            if ctx is not None:
                ctx.compute(timeout=30)
            assert cell.checksum == Buffer("recovered", "str").get_checksum()
        finally:
            if ctx is not None:
                ctx.close()
            seamless.close()
        print("MISSING_INPUT_RECOVERY_OK")
    ''')
    proc = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                          capture_output=True, text=True, timeout=90,
                          env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'MISSING_INPUT_RECOVERY_OK' in proc.stdout
