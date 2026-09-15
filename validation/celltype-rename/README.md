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

## Phase 4

Cells now retain their input interpretation and convert to their configured
output type, both standalone and bound. Typed sources supply the input type;
constants retain the serialization type. Public input access is split into
read-only `source` and writable output `checksum`. All six value/buffer/checksum
writes implement declaration versus ownership checks. Expressions snapshot typed
inputs and reject conflicting declarations. Graphs write version 0.4 and retain
the declared input type with constant producers.

Null uses one trivial, cache-independent checksum across celltypes; bytes null
resolves as empty bytes. File mounts read missing, empty, and canonical-null files
as null without rewriting them, and write null as physically empty files,
including compressed-file paths. Directory mounts distinguish missing from empty.
Conversion failures report the cell's exception. Source assignment replacing a
transformer creates a cell with the source type and preserves downstream edges.

The added core and workflow contract tests cover these semantics. The ported
original probes are executable with assertions:

```sh
conda run --no-capture-output -n seamless1 python validation/celltype-rename/celltype_probes.py
```

Validation logs are in `/tmp/celltype-rename-phase4-gate/`. The initial transformer
Expression tests contained two conflicting typed-source declarations; these now
use type inference and their four-case rerun passes. The cancellation CLI test
again exposed a jobserver startup import-lock race, before request execution.
Initializing `seamless.util` before transformer startup avoids that import cycle;
the fresh-service cancellation rerun passes all eight cases. The existing
Expression dependency refhold assertion remains the single baseline failure.
The static retired-name check necessarily retains the graph loader's explicit
legacy `target_celltype` checks, as required for 0.2/0.3 compatibility, in addition
to conversion helpers and the retired-name registry.

Final gate: 149 files, with only the known baseline failure. The full workflow
rerun (`workflow-final.log`) exits zero, including all 24 new workflow contract
cases. The 26 core contract cases and the ported probes also pass.

## Phase 5

Typed transformer arguments now convert from their declared output type to the
pin type before becoming transformation inputs. This covers call-time arguments
and prebound references; snapshots retain dependency references while copying
literal values. Converted integer inputs have the same transformation identity
as the corresponding integer literal.

Canonical null is accepted by required plain/mixed/bytes pins and rejected with
a pin-named error for other required types. Optional null inputs are removed for
all pin types. The Expression null fast path bypasses deserialization and value
conversion; a regression test replaces its decoder with an assertion failure.
Python and compiled execution serialize null results for plain/mixed/bytes and
reject them for other result types. Empty bytes results use the null checksum.

The new Dask test starts a distributed scheduler and workers using the existing
`create_dummy_client` fixture helper, then executes converted Transformation and
Cell-expression dependencies, checks input checksums, and checks optional-binary
null removal and required-int null rejection. The real hashserver/database/
jobserver schema and cache-hit test remains in the full gate.

Logs: `/tmp/celltype-rename-phase5-gate/`. The shutdown-order test initially failed
before the writer flush hook ran; its separate rerun passed all 16 cases. The
hash-type test now expects typed inputs to fail at conversion and report the
input pin, instead of expecting a later deserialization failure; both cases pass.

Final gate: 151 files (two new pin-conversion files), with the same single
Expression input-refhold assertion failure as Phase 4. Workflow exits zero;
all Dask files pass. The final snapshot-copy changes are covered by the 11-case
pin recheck (including a cloned prebound builder) and 19 compiled end-to-end cases.

## Phase 6

`CellBase` now owns the shared value/type/evaluation and reference-lifecycle API.
`Cell` retains projection, validators, mounts, derivation, and source protocols.
`Pin` is a sister class in seamless-transformer, with a Transformer-owned backend;
reads return fresh handles even when an input is unwired. Core rejects a non-Cell
CellBase input with a message directing callers to `pin.source`.

Standalone pin storage is `(input reference, input celltype)`: literals serialize
at assignment and acquire a Transformer refhold; typed inputs remain references.
The six root writes share the declaration/ownership distinction, and deletion
removes declarations only when the signature is not fixed. Pin types delegate to
`Transformer.celltypes`, including compiled inputs. Snapshots record input types
and freeze Cell sources into Expressions; prebound pins build converting
Expressions while call-time values override them. Builder clones retain original
input recipes with independent checksum ownership.

The storage audit found writes only in Pin backend replacement and builder
cloning, both storing reference/type pairs. New tests cover fresh/unwired handles,
all six writes, retained serialization types, failed retypes and recovery, source
refusal, null versus clearing, declaration deletion, compiled pins, and frozen
snapshots over live Cell sources. Existing literal-lifecycle tests now require
checksum refholds. The baseline Expression dependency test is corrected to assert
its input claim remains, while its internally evaluated result has no result
claim; it passes without changing Expression ownership behavior.

Gate logs: `/tmp/celltype-rename-phase6-gate/`. Final snapshot changes are covered
by `pin-handles-final.log`, `compiled-recheck.log`, and `snapshot-recheck.log`.

Final gate: all 153 files pass, including the full workflow suite. Two files are
added relative to Phase 5 (core base contracts and standalone Pin handles).
The prior baseline refhold assertion is now correctly scoped and passes.

## Phase 7

Bound transformer input reads now return fresh Pin handles. Their backend reports
source, original input type, converted checksum/value, state, and conversion
exception. The reactive controller converts each input before constructing a
transformation; failed conversion blocks on that pin and creates no current run.
Connected snapshot calls use the same conversion recipe. Optional-null inputs are
dropped before conversion, while a connected input with no checksum blocks.

Pin writes distinguish declaration from ownership: assignments detach existing
sources, `set*` methods check authority, and checksum/buffer None clears input
without removing the declaration. Deleting a pin declaration is allowed only for
signatureless code. Pin endpoints cannot source edges, and public attempts to use
a Pin as input direct callers to `pin.source`. Standalone builder ingestion
preserves the original pin input types, including declared checksums and retypes;
compiled builder aliases delegate their pin and celltype APIs after binding.

The 40-case retype matrix covers four source kinds and both type-setting APIs.
Additional contracts cover writes, failed conversion without construction,
null/missing distinction, graph roundtrips, stale handles, original compiled
aliases, and input recipes retained through builder replacement. Both original
scratchpad pin probes are ported into the suite; their output shows conversion
and canonical input checksums in the formerly reinterpreting cases.

The final audit found a bare empty-bytes checksum bypassing the same-type bound
pin path. Empty bytes now normalize to canonical null before optional removal,
including standalone pin construction and Expression inputs. Core tests exercise
conversion of that input to five output types; function and bound-pin tests
verify optional absence. A complete core rerun covers the input-key change.

Logs: `/tmp/celltype-rename-phase7-gate/`; initial workflow findings and their
per-file rechecks are retained in `/tmp/celltype-rename-phase7/`. Existing tests
now read Pin `.value`/`.checksum` explicitly and clear with `.checksum = None`.

Final gate: all 157 files pass. Four workflow files are added relative to Phase 6
(the Pin matrix, handle contracts, and two original probes). The complete core
rerun is green, as are the final 17 bound-Pin cases, 12 standalone conversion
cases, and six compiled workflow cases. The final JSON records original runs and
recheck paths separately.

## Phase 8

Updated the core, transformer, database and workflow READMEs; type_bits_design;
mount and attachment designs; pass3, followup and optional-pin rules. Six
historical plans retain their original content with a supersession note, and the
handoff's None-deletion rule points to the current followup section. The naming
and pin-conversion memories now describe the implementation; exact copies are
committed in memory/, and the external memory index was updated.

Final gate: all 157 test files pass (145 baseline files retained, 12 added).
Logs: `/tmp/celltype-rename-phase8-gate/`. Dask test_strict_dunder initially failed
collection with the known Python import-lock race. Its first isolated rerun
passed eight tests but reported a background-thread import-lock warning; the
final isolated rerun passed all eight without warnings. Both reruns and the
initial result remain recorded in phase8.json. No code changes were made in
this documentation phase. Core and transformer README examples also executed
successfully, and git diff --check passed for all edited repositories.

See completion-audit.md for specification-to-evidence mapping. The implementation
retains the plan's stated out-of-scope restrictions, including Pin as a source
and assigning standalone deferred Cell/Expression inputs directly to bound pins.

## Review follow-up (2026-09-15)

Implements §1 of `seamless/celltype-rename-review-decisions.md`, on top of
seamless-core `3701453`, whose conversion engine keeps the checksum for a
conversion between nested celltypes (that document's §3.3).

Baseline repairs. Checks written before that change failed: the bound pin retype
matrix (16 of 40 cases) and `test_celltype_changes.py` (4 of 20) expected a new
checksum for int → float and str → int, and `celltype_probes.py` failed on
int → float. seamless-dask `test_pin_conversion.py` expected int 42's checksum for
an int pin fed by a str; it now expects the str's checksum. Both matrices now carry an explicit `keeps_checksum` column.
`test_declared_checksum_roundtrip_preserves_pin_input_type` declared a checksum
without holding its buffer; it now holds it.

One workflow check still fails, because of code rather than a stale expectation:
`correctness/test_correctness_compiled.py::test_original_compiled_alias_exposes_bound_pins`.
A compiled transformation records every pin as `mixed`, so an `int` pin that keeps a
str's checksum reaches the C function as a string. The decisions document records it
for discussion.

Gate: the core, database, dask and workflow suites pass, apart from that compiled
check. At the committed heads, transformer `test_expression_inputs.py` and
`test_fingertip.py` also fail; the decisions document records both.

Probes. `test_pin_probe.py` and `test_pin_probe2.py` had no assertions, and
`celltype_probes.py` ran in no suite; all three are removed. Their scenarios are
now assertions: the pin matrix gained text → int and int → str rows (56 cases) and
checks the checksum that enters the transformation, reactively and in a snapshot;
transformer `test_retype_converts_at_call`; workflow
`test_standalone_and_bound_retype_agree` and `test_typed_source_constructions_agree`;
core `test_typed_expression_input_supplies_both_types`. The Phase 4 and Phase 7
notes above, which describe these probes as executable or ported, are historical.

Added contracts: the block kind of a failed pin conversion, retired names over the
actual registry, symmetric Expression defaults, pin detachment through the pins
namespace, buffer deposits without a tempref, the `.source`/`.checksum` rows of
connected cells, a null result into a connected optional pin, item-form null
assignment, celltype stability, the database composite key, downstream checksums,
and pin deletion clearing celltypes and optionality.
