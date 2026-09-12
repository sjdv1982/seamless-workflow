# Celltype rename and pin redesign validation

The accompanying plan is the implementation specification. Each phase is committed
only after its verification gate. Tests run in the `seamless1` conda environment,
with a fresh pytest process for each file. Workflow uses `tests/run-tests.sh`.

## Phase 0

The prerequisite edits were already committed before this work began:
core `be8f7b1`, workflow `1695b27`. Both working trees were clean.
Workflow work continues on `celltype-rename`; other implementation repositories
remain on `cells-and-expressions`. Unrelated edits in the `seamless` design
repository are preserved.

Baseline logs: `/tmp/celltype-rename-baseline/`. The per-file summary is recorded
here once the complete baseline finishes. Non-workflow files have a 180-second
process deadline; a timeout is recorded separately from a test failure.

## Specification precedence

Where phase checklists conflict with the detailed decided semantics, use the
latter: mounted null cells write empty files without a type error, and required
`bytes` pins accept canonical null as `b""`. The explicit Phase 5 requirement also
allows null results for `bytes`. No approval pauses are required between phase
commits: the user's implementation request authorizes those commits.
