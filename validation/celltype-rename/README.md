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

## Phase 2

Input-side Cell/Expression fields, evaluator keys and parameters, workflow builder
ingestion, and Dask's Python payload builder now use `input_celltype`. The old
standalone `celltype` name raises a replacement-directed error; bound `celltype`
still delegates to its backend. Wire keys, database columns and CellConfig are
unchanged. Four additional guard tests verify standalone refusal and continued
bound delegation. All 146 file outcomes and failed test identities match Phase 1;
workflow passed on its initial run. Logs: `/tmp/celltype-rename-phase2/`.

## Phase 3

The output side is `celltype`; input/output defaults are symmetric. Retired
`target_celltype` reads on standalone Cells and Expressions fail clearly, while
bound configuration retains its pre-Phase-4 semantics. Expression wire payloads,
DB columns and composite keys use `input_celltype, celltype` together. Unused
Edge type fields are removed.

Gate: 147 test files; every pre-existing file matches Phase 2. A CLI integration
file initially failed on a Python import-lock deadlock during jobserver startup
(`seamless.util.pylru`), before request handling. Its fresh-service rerun passed
all seven cases. Both logs are retained under `/tmp/celltype-rename-phase3/`.
Six symmetric-default cases and a real remote-schema integration test are added.
The latter starts hashserver/database/jobserver on a fresh cache, evaluates a
str-to-text Expression remotely, inspects the actual SQLite row/columns, clears
the in-process expression cache, and verifies a second evaluation succeeds with
jobserver dispatch replaced by an assertion failure.

Existing development caches require these statements **in order**:

```sql
ALTER TABLE expression RENAME COLUMN celltype TO input_celltype;
ALTER TABLE expression RENAME COLUMN target_celltype TO celltype;
```

Alternatively drop/recreate the expression cache table. Test services use fresh
DBs; no unrelated running service or user cache is modified.
