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

## Phase 1

Guardrails added with an initially empty retired-name registry; names enter it
when their APIs are removed in subsequent phases. Expression type and validator
fields are keyword-only. Positional call sites in core and transformer tests
were updated. Six new tests cover reads/writes/deletion on Cell, SubCell, and a
bound backend, Expression reads, explicit item navigation, and positional refusal.

Final gate: 146 files, the same single transformer reference-ownership failure
as baseline; all workflow files pass. The first workflow run had two intermittent
failures: `test_a_repaired_failure_revokes_before_it_recomputes` observed a fast
completion, and a downstream latency test timed out and hung during cleanup.
The former passed an isolated rerun; the latter passed three isolated reruns.
A complete workflow rerun, without the other suites running concurrently, passed.
Raw first-run and rerun logs are retained in `/tmp/celltype-rename-phase1/`.
