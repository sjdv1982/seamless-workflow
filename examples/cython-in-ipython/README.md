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
function timing. Edits are sensed automatically while the Context is alive.

For an interactive IPython session:

```python
from workflow import build
ctx = build(i=6000)
ctx.compute()
ctx.mounts.sync(timeout=120)
ctx.result.value, ctx.seconds.value
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
projects its result, HTML, and timing into separate cells. It does not use the
legacy interpreted IPython module injection. Blank lines delimit the code cells
in these `.ipy` files; keep the Cython block together when editing it.

`ctx.seconds` measures only `func(i)`. It is diagnostic data saved with the
transformation result: a cache hit restores the old timing without rerunning
the function. Compilation and mount synchronization are excluded. See
[PROBE-RESULTS.md](PROBE-RESULTS.md) for independently measured wall times.
