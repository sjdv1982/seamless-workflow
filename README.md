# seamless-workflow

Reactive workflow `Context` layer for Seamless.

Context nodes are exposed through the canonical `seamless.Cell` and
`seamless_transformer.Transformer` handles. The experimental wrapper/view surface
described by the earlier context handoff documents is superseded by
[`context-internals-followup-plan.md`](../seamless/context-internals-followup-plan.md).

Whole Context cells can be mounted to files:

```python
from seamless import Cell
from seamless_workflow import Context

with Context() as ctx:
    ctx.config = Cell(celltype="plain")
    ctx.config.mount("config.json")
    ctx.output = Cell(celltype="text")
    ctx.output.set("ready")
    ctx.output.mount("output.txt", mode="w")
    report = ctx.mounts.sync(timeout=10)
```

`mount(path, mode="rw", authority="file", persistent=True)` blocks through the
initial read and first write attempt. Modes are `r`, `w`, and `rw`. Authority
chooses the initial winner; subsequent file edits are authoritative inputs in
sensing modes. `file-strict` reports missing files as cell exceptions. Invalid
or unreadable input also fails the cell, preserves its stored last good value,
and continues monitoring. Delivery failures appear on `ctx.output.mount.error`
and retry with backoff; they do not invalidate the cell's value.

`ctx.compute()` waits only for graph work. Call `ctx.mounts.sync()` before
reading mounted outputs externally, or `await ctx.mounts.synchronization()` in
async code. The returned mapping contains status and errors per node path.
Synchronization establishes a finite filesystem cut; continuously changing
external files can prevent completion, so interactive clients can set a timeout.

Use `del ctx.config.mount` to unmount. `persistent=False` conditionally deletes
unchanged files at unmount or close. Explicit Context close flushes already
requested persistent deliveries, bounded by `close(timeout=60)`. A foreign
modification is never deleted based solely on its path. Two incompatible mounts
of overlapping paths within one process are refused. Write-only mounts pause
after three foreign-write reassertions in 20 seconds; inspect `.mount.error` and
call `.mount.clear_error()` to resume.

Text/code, JSON scalar/plain, bytes, binary, and mixed celltypes are supported.
`.gz` and `.zst` paths compress canonical bytes. `folder` and `deepfolder` use
directories, with per-leaf atomic replacement. Directory trees are not replaced
atomically. Mounts follow a target symlink and preserve it; atomic file writes
break hardlink sharing. Cross-process exclusion and filesystem aliases such as
hardlinks are not covered by the registry.

Graphs retain mount specifications (format `0.3`). **Loading a graph can write
files:** use `ctx.set_graph(graph, mounts=False)` for graphs of unknown origin.
Sensing cells cannot receive incoming edges; mounted celltypes cannot change.
Unmount first. Standalone cells, sub-path projections, transformer pins and code
handles cannot be mounted directly: mount a whole cell and connect it instead.

The transport currently uses polling and a bounded daemon I/O pool. Diagnostics
include `seamless_workflow.diagnostics.record_attachments(ctx)` and the
controllable `attachments.manual.ManualDriver` for ordering tests.

Set `SEAMLESS_MOUNT_NATIVE=1` before the first mount to enable Linux inotify hints
alongside polling. Unsupported platforms and failed watches retain polling.
The experimental `attachments.widget.WidgetDriver(widget).attach(ctx.cell)`
exercises the same session protocol with a traitlets-style callback widget;
widget and manual-driver sessions are never serialized. This is an experimental
second transport, not a promise that arbitrary external services share file
mount conflict policy.
