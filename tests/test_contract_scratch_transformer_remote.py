"""A scratch transformer with literal pins can run on a jobserver.

contracts/pins.md, *Scratch at the pin*: literal pins, code, and modules are
published regardless of result scratch; the result remains scratch.
"""
import os
import subprocess
import sys
import textwrap
import uuid

from test_expression_execution import _write_remote_config


def test_scratch_transformer_with_literal_pins_runs_on_a_jobserver(tmp_path):
    project = 'scratch-remote-' + uuid.uuid4().hex
    _write_remote_config(tmp_path, backend='jobserver', project=project)
    script = textwrap.dedent('''
        import asyncio
        import uuid
        import seamless
        import seamless_config
        from seamless.transformer import delayed
        from seamless_workflow import Context
        from seamless_remote import buffer_remote

        def suffix(word):
            return word + "-suffix"

        def on_hashserver(checksum):
            length = asyncio.run(buffer_remote.get_buffer_lengths([checksum]))[0]
            # A read server answers /has with a boolean; a read folder a length.
            if isinstance(length, bool):
                return length
            return isinstance(length, int) and length >= 0

        seamless_config.init()
        try:
            ctx = Context()
            ctx.tf = delayed(suffix)
            ctx.tf.scratch = True
            word = "scratch-remote-" + uuid.uuid4().hex
            ctx.tf.pins.word = word
            ctx.compute(timeout=60)
            assert ctx.tf.state == "complete", (ctx.tf.state, ctx.tf.exception)
            claims = {
                role: checksum
                for checksum, role in ctx._refheld_checksums()
                if role.startswith("transformer:tf:")
            }
            for role in ("transformer:tf:pin:word", "transformer:tf:code"):
                assert on_hashserver(claims[role]), role + " was not published"
            result = ctx._graph.nodes[("tf",)].current_checksum
            assert result is not None
            assert not on_hashserver(result), "a scratch result was published"
            ctx._release_refholds()
        finally:
            seamless.close()
        print("SCRATCH_REMOTE_OK")
    ''')
    proc = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'SCRATCH_REMOTE_OK' in proc.stdout
