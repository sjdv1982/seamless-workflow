# Cython in IPython

Port of legacy `tests/workflow-core/injection4.py`, using its unchanged
`cell-ipython-ORIGINAL.ipy` and `cell-ipython-OPTIMIZED.ipy` snapshots.
The calculation, `%%cython -a` annotation, and `func_html = _.data` are preserved.

Run from this directory in the `seamless1` environment:

```bash
conda activate seamless1
python workflow.py
```

Requires IPython, Cython, setuptools, and a C compiler. Open the generated
`cell-ipython.html` in a browser and edit the mounted `cell-ipython.ipy`.
Copy either snapshot over it, or interpolate between them. Press Enter to wait
for graph computation and filesystem delivery and print the current result and
transformer execution time from its logs. Edits are sensed automatically while
the Context is alive.

For an interactive IPython session:

```python
from workflow import build
ctx = build(i=6000)
ctx.compute()
ctx.mounts.sync(timeout=120)
ctx.result.value, ctx.tf.logs
# Edit cell-ipython.ipy, then synchronize before reading the output file.
ctx.mounts.sync(timeout=120)
ctx.compute()
ctx.mounts.sync(timeout=120)
# When finished:
ctx.close()
import seamless
seamless.close()
```

Modern Seamless accepts Python and Bash transformers. This example executes the
IPython source in a fresh `InteractiveShell` inside a Python transformer, then
projects its result and HTML into separate cells. The transformer's `logs`
property includes an `Execution time` line. It reports the whole transformer
wall time, including shell setup and Cython compilation; mount synchronization
is outside the transformation and is excluded. A cache hit returns the log from
the cached transformation. The reported time is rounded to tenths of a second.
This example does not use the legacy interpreted IPython module injection.
Blank lines delimit the code cells in these `.ipy` files; keep the Cython block
together when editing it. See [PROBE-RESULTS.md](PROBE-RESULTS.md) for
independently measured wall times.
