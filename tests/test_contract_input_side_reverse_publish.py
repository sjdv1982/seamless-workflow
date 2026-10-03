"""Contract test: a bound transformer's pin conversion reverse-publishes
(contracts/internal/checksum-reference-lifecycle.md, §1, ruling 2026-09-30).

A non-scratch pin is an input-side owner. When its conversion is dispatched
(the source buffer is on the hashserver only), the request carries scratch=False, so the
executing side writes the converted buffer to the hashserver.
"""
import os
import subprocess
import sys
import textwrap
import uuid

from test_expression_execution import _write_remote_config


def test_bound_pin_conversion_is_written_to_the_hashserver(tmp_path):
    project = 'bound-input-reverse-publish-' + uuid.uuid4().hex
    _write_remote_config(tmp_path, backend='jobserver', project=project)
    script = textwrap.dedent('''
        import asyncio
        import uuid
        import seamless
        from seamless import Buffer, Cell, Checksum
        import seamless_config
        from seamless.caching.buffer_cache import get_buffer_cache
        from seamless.checksum import expression as expression_mod
        from seamless.checksum.cached_calculate_checksum import checksum_cache
        from seamless.transformer import delayed
        from seamless_workflow import Context
        from seamless_remote import buffer_remote

        def drop_buffer(checksum):
            checksum = Checksum(checksum)
            cache = get_buffer_cache()
            with cache.lock:
                cache.weak_cache.pop(checksum, None)
                cache.strong_cache.pop(checksum, None)
            checksum_cache.pop(checksum, None)
            expression_mod._expression_result_buffers.pop(checksum, None)

        def on_hashserver(checksum):
            length = asyncio.run(buffer_remote.get_buffer_lengths([checksum]))[0]
            # A read server answers /has with a boolean; a read folder a length.
            if isinstance(length, bool):
                return length
            return isinstance(length, int) and length >= 0

        def echo(value):
            return value + "!"

        seamless_config.init()
        try:
            word = "bound-pin-" + uuid.uuid4().hex
            source = Buffer(word, "str")
            source_checksum = source.get_checksum()
            assert asyncio.run(buffer_remote.write_buffer(source_checksum, source))
            drop_buffer(source_checksum)
            del source
            expected = Buffer(word, "text").get_checksum()
            drop_buffer(expected)

            ctx = Context()
            ctx.src = Cell("str", checksum=source_checksum)
            ctx.tf = delayed(echo)
            ctx.tf.celltypes.value = "text"
            ctx.tf.pins.value = ctx.src  # str -> text needs the buffer: dispatched
            ctx.compute(timeout=60)
            assert ctx.tf.pins.value.checksum == expected
            assert on_hashserver(expected), "the dispatched pin conversion was not written"
            assert ctx.tf.state == "complete", (ctx.tf.state, ctx.tf.exception)
            ctx._release_refholds()
        finally:
            seamless.close()
        print("CONTRACT_OK")
    ''')
    proc = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'CONTRACT_OK' in proc.stdout
