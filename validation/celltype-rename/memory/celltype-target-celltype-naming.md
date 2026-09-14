---
name: celltype-target-celltype-naming
description: "Implemented Cell/Expression naming: output celltype, derived input_celltype, source/checksum split; graph 0.4 and lockstep DB/wire schema"
metadata: 
  node_type: memory
  type: project
  originSessionId: 057e2dcf-f13d-4224-9fdb-59619e0be785
  modified: 2026-09-14T00:00:00.000Z
---

Implemented phase-by-phase from `/home/agent/.claude/plans/ok-please-write-a-curried-toucan.md` (2026-09-14). Validation and per-phase commits are recorded in `seamless-workflow/validation/celltype-rename/`.

`Cell.celltype` and `Expression.celltype` are output types. Cell `input_celltype` is read-only: live typed-source type, or the serialization/declaration type stored with a literal/checksum; None when unwired. Retyping converts the original input. A new connected Cell copies its source's type once; rewiring an existing Cell preserves its output type. Expression type arguments are keyword-only; typed-input overrides must agree. Cell snapshots freeze the recipe.

Public `input_ref` and `target_celltype` are retired and blocked before structural projection; dictionary keys remain available by item access. `_input_ref` is private. Read-only `source` reports the upstream reference (None for owned literals); `checksum`, `buffer`, and `value` report the produced output, including connected Cells. `Cell(checksum=...)` and `Cell(source=...)` are exclusive.

Attribute writes to value/buffer/checksum declare input and detach a connection. The methods set/set_buffer/set_checksum require ownership. Checksum declarations accept input_celltype; changing interpretation requires reassignment. Value None stores canonical null; checksum/buffer None clears input; del deletes. Mounted input clearing is refused.

Canonical null is b"null\n", SHA-256 38e0b9de817f645c4bec37c0d4a3e58baecccb040f5718dc069a72c7385a0bed. It is valid Cell storage in every type and converts unchanged without cache dependence. Bytes null resolves to b""; empty bytes normalize to null. Missing, empty and canonical-null files read as null and preserve their physical form; null writes an empty file (also gzip/zstd). Missing directories are null; empty directories are {}.

Graph 0.4 stores one Cell output type, with input type retained in constant recipes. Legacy 0.2/0.3 graph targets are accepted only when absent/equal; differing targets raise a path error. Expression identity, DB columns and wire keys use (input_checksum, path, input_celltype, celltype). Pure conversion helpers may still use explicit source_celltype/target_celltype names. The development DB migration is in seamless-database/README.md.
