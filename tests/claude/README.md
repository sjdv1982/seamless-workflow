# Phase A0 contract suite

The test-writing half of **phase A0** of
[`seamless/attachments-and-mount-design.md`](../../../seamless/attachments-and-mount-design.md)
(§15), mined against the legacy suite in `~/legacy-seamless/tests` as that
section instructs.

**This suite is red on purpose.** A0's exit evidence is *"a red suite that
describes the intended contract"*.  Nothing here is `xfail`-marked: an `xfail`
would hide exactly the signal the phase exists to produce, and — as §15 A1
notes — an `xfail`ed test still runs and still pollutes process-global state.
Strict `xfail` markers arrive at A1, on the suites the design names there.

Each test instead carries the phase at which it is expected to turn green, as a
selectable pytest marker.  A red test whose marker is `a4` is not a bug report;
it is a dated promise.

## Running it

```bash
cd seamless-workflow/tests/claude
./run-tests.sh                 # one pytest process per file (project convention)
./run-tests.sh -m "not slow"   # skip the sleeping transformer bodies
./run-tests.sh -m a1           # only what phase A1 must make green
pytest node-transition/test_transition_eligibility.py   # a single file
```

One process per file is not decoration: process-global cache and refholder state
carries between files, and combining Seamless test files in one pytest run
produces spurious failures.  `conftest.py` resets that state per *test*, which
is necessary but not sufficient — worker processes, the shutdown registry and
module-level caches in `seamless-core` are per-process.

Body durations and timing thresholds are environment-tunable:
`SEAMLESS_CONTRACT_BODY_SECONDS` (default 5, the value §15 A0 uses),
`SEAMLESS_CONTRACT_SHORT_BODY_SECONDS` (2), `SEAMLESS_CONTRACT_PROMPT_SECONDS`
(1), `SEAMLESS_CONTRACT_SETTLE_TIMEOUT` (60).

## Layout

| path | subject |
|---|---|
| `contract_helpers.py` | the two places where the contract is read out of the implementation (node state, barrier entry points), plus observation and execution-counting instruments |
| `conftest.py` | per-test cache and refholder-registry reset; marker registration |
| `node-transition/` | §14.1 — eligibility is not delivery; invalidation; failure and unwired propagation; cache hits |
| `quiescence-barrier/` | §14.2 — `ctx.compute()`, `ctx.a.compute()`, their async forms, and the legacy `compute(timeout)` question |
| `latency/` | §15 A0 — the test "that makes the whole defect self-evident", and the re-execution counter |
| `correctness/` | [MOD-15] non-Python and envelope correctness; [MOD-3] expression regression net; diamonds |

## Status and phase expectations

Measured against the current implementation (one process per file):

| file | tests | pass now | red | target |
|---|---|---|---|---|
| `node-transition/test_transition_eligibility.py` | 7 | 2 | 5 | A1 |
| `node-transition/test_transition_invalidation.py` | 5 | 1 | 4 | A1, one A4 |
| `node-transition/test_transition_unwired.py` | 6 | 4 | 2 | A1 |
| `node-transition/test_transition_failure.py` | 5 | 2 | 3 | A4 |
| `node-transition/test_transition_cache_hit.py` | 2 | 0 | 2 | A4 |
| `quiescence-barrier/test_barrier_context.py` | 6 | 0 | 6 | A3, three A4 |
| `quiescence-barrier/test_barrier_node.py` | 5 | 2 | 3 | A4 |
| `quiescence-barrier/test_barrier_async.py` | 4 | 0 | 4 | A4 |
| `quiescence-barrier/test_barrier_no_pump.py` | 3 | 0 | 3 | A1, two A4 |
| `latency/test_latency_last_pin.py` | 4 | 1 | 3 | A1, one A4 |
| `latency/test_latency_downstream.py` | 4 | 0 | 4 | A1, two A4 |
| `latency/test_latency_delay_port.py` | 3 | 0 | 3 | A1, two A4 |
| `correctness/test_correctness_bash.py` | 5 | 0 | 5 | A1, four A4 |
| `correctness/test_correctness_compiled.py` | 5 | 1 | 4 | A4 |
| `correctness/test_correctness_modules.py` | 4 | 1 | 3 | A4 |
| `correctness/test_correctness_environment.py` | 3 | 2 | 1 | A4 |
| `correctness/test_correctness_no_code.py` | 4 | 0 | 4 | A1 |
| `correctness/test_correctness_expressions.py` | 10 | 9 | 1 | A3 |
| `correctness/test_correctness_fanin.py` | 8 | 2 | 6 | A2 |
| **total** | **93** | **27** | **66** | |

The five `test_correctness_compiled.py` tests are skipped without `gcc`.

Two measurements from that run are worth keeping.  Setting the last pin of a
transformer blocks the caller for a full body duration and returns `complete`
with a result checksum already installed — the defect §15 A0 is built around.
And connecting one downstream transformer to a *settled* node re-executes the
upstream body **seven more times**: `latency/test_latency_downstream.py` counts
eight executions where one is correct, because `_derive_all` re-derives every
node once per convergence pass, makes up to one pass per node, and derivation is
execution.  The cost of adding a node to a settled graph is therefore quadratic
in bodies run.

Four of the green tests are marked `a1`/`a4` rather than `now`, because they are
green today for a reason the A1–A3 limbo removes: they observe a *completed*
transformer, and in limbo no transformer completes.  Expect them to go red at A1
and green again at A4.  That is the limbo working, not a regression.

The 24 tests marked `now` are the regression net: pure propagation completing in-turn,
`unwired` on a missing pin, the sub-path expression behaviour that [MOD-3] must
preserve while deleting the private evaluator, the durable half of `modules` and
`environment`, and the node barrier's existing return value (which
`test_canonical_handles.py:143` already pins).  **They must stay green through
every phase.**

## Decisions this suite writes down

§24.5 requires the meaning of `waiting` to be settled *before* the first
transition test, "which will otherwise encode its author's reading".  It is
settled here as the design's recommended reading **(a)**:

* `waiting` — the node's inputs are not all concrete checksums yet;
* `computing` — inputs are concrete and the work has been submitted, queued or
  running.

So a transformer whose pins are all literals goes straight to `computing` and
never shows `waiting`; only its downstream shows `waiting`.  This matches the
§15 A0 latency script ("set the last pin -> state is computing").  If the
project later chooses reading (b), the assertions to flip are the `computing`
ones in `test_transition_eligibility.py` and `test_latency_delay_port.py`;
everything else is stated as "pending, and no result checksum", which holds
under both.

**The legacy `compute(timeout)` pump is not ported, and neither is a timeout on
the graph-quiescence barrier.**  Legacy `ctx.compute(0.5)` conflated three
things: a pump (in legacy the loop ran in the caller's thread, so the barrier
*was* what advanced the graph), an observation window, and a hang guard.  A
controller thread removes the first; the second becomes a plain `sleep`; the
third belongs to the caller (§14.2).  The suite therefore never advances a
computation by waiting on a barrier, and
`quiescence-barrier/test_barrier_no_pump.py` states the constraint in the form
that survives either API choice: **a barrier never returns a non-quiescent
graph.**  A `timeout=` that raises on expiry passes that; a timeout that returns
early as if settled does not.  (The design is not self-consistent on the word:
§14.2 says barriers take no timeout, [MOD-17] says "barriers need timeouts".
They are about different objects — graph quiescence versus the §21 external
finite cut and its delivery deadlines.  Only the first is constrained here.)

**Transition assertions are on internal node state, never on `.status`**, because
`_PUBLIC_STATUS` maps `waiting` and `computing` to the same string (§15 A0).  The
state is read through the public `get_graph(runtime=True)`, in one helper, so a
later phase changes one function rather than ninety-three tests.

## What A0 still needs that is not in this directory

This is the test-suite half of A0.  Still outstanding from §15 A0:

* the materialisation recording mode of **[MOD-14]** (`seamless.diagnostics.record_materialisation`)
  and the exact expected logs per public operation — the "green log that
  describes the current behaviour" half of A0's exit evidence;
* the one-branch fix of **[MOD-15]** (stop reporting `complete` for a node with
  no executable code).  `correctness/test_correctness_no_code.py` is its
  acceptance test and is red;
* the **[MOD-16]** fix making `Expression` claim its `input_ref`, and its
  lifetime test;
* the semantic decisions **[MOD-2]**, **[MOD-4]**, **[MOD-7]** (**[MOD-1]** is
  already decided).

## Findings from writing these tests

Defects that this suite encodes and the design does not list.  Each has a test;
none required more than reading the code the test walked into.

1. **The cycle check is inverted** (`correctness/test_correctness_fanin.py`).
   `_add_edge` refuses an edge when `_would_cycle(source, target)`, and
   `_would_cycle` walks forward *from the source*, returning true if it reaches
   the target.  Adding `source -> target` closes a cycle exactly when the target
   already reaches the *source*.  Both failure modes follow: a second edge from
   one node into another is refused as a "Dependency cycle" — which is what
   every diamond is, and what `tf.pins.x = ctx.a; tf.pins.y = ctx.a` is — and a
   genuine two-node cycle is accepted (`ctx.b = ctx.a` then `ctx.a = ctx.b`
   installs the back edge silently).  This is why the "diamonds" §15 A0 asks for
   are in their own file instead of in the regression net.
2. **The block reason degrades in both directions** (`node-transition/`).  A
   *transformer* downstream of an `unwired` transformer is reported
   `blocked-by-error` — an error the user will look for and not find — because
   `_apply_pending` folds every non-pending upstream state into the error
   reason.  A *cell* downstream of a node that is already `blocked` is reported
   `blocked-by-unwired`, because `_apply_upstream_state` folds `blocked` and
   `unwired` together.  So a failing transformer reports *error* one hop down
   and *missing connection* two hops down, and a disconnected pin reports the
   opposite.
3. **A Python transformer whose code is cleared reports `Status: OK` with
   `None`** (`correctness/test_correctness_no_code.py`).  [MOD-15] found this
   with bash; it is reachable without leaving Python.  `ctx.tf.code = None` on a
   fully wired transformer turns the value 3 into `None` while the status stays
   OK and nothing reports that the code is gone.
4. **Binding a `CompiledTransformer` loses its language, not only its schema**
   (`correctness/test_correctness_compiled.py`).  The node reports
   `language="python"` and stores the builder object as `callable`, so
   `ctx.tf.pins.a = 2` raises `AttributeError: Unknown transformer pin 'a'`
   against the builder's `(*args, **kwargs)` signature.  Configuration fails
   before execution is ever reached, which puts the representation gap ahead of
   the execution gap in practice as well as in principle.
5. **A missing key and a key holding `null` are indistinguishable**
   (`correctness/test_correctness_expressions.py`).  Both report `complete` with
   value `None`, so a typo in a projection path is consumed downstream as a
   well-formed null.  [MOD-3] moves sub-path reads onto `evaluate_expression`,
   which is where the distinction can be made; the test asserts only that the
   two stop being identical, not which answer is chosen.
6. **`ctx.tf.celltypes` neither normalises nor validates.**  The standalone
   `CelltypesWrapper` maps `int -> "int"` and rejects unknown celltypes; the
   Context's `WorkflowCelltypes` stores `str(value)`, so `celltypes.lines = int`
   records `"<class 'int'>"` and `celltypes.other = "not-a-celltype"` is
   accepted.  This has no test here (config validation is [MOD-6]'s per-field
   validator table, not one of the four areas), but the bash tests use the
   string form because of it.
7. **The failure-propagation transition tests cannot pass at A1**, contrary to
   §15's grouping.  A1 removes in-cascade execution, and execution is the only
   producer of `failed` in a Context: cell validators are not applied, an alias
   edge does not enforce the target celltype, and the two remaining cell-level
   failure paths raise out of the public call instead of recording a node
   failure.  During the A1–A3 limbo no node reaches `failed` at all, so those
   tests are marked `a4`.

## Open questions for whoever implements the phases

1. **Does the node-local barrier raise or return on a `blocked`/`unwired` node?**
   §24.4 settles the Context-wide form as *returns*.  The node-local form raises
   `NodeError` today; `test_barrier_node.py` pins that so the answer is chosen
   rather than drifted into.
2. **`computation` is a name that has to be claimed.**  On a bound `Cell`,
   attribute access is sub-path projection, so `ctx.a.computation` currently
   resolves to a projection of `a`'s value and `ctx.a.computation()` builds an
   `Expression`.  The async barrier has to take the name as a real method, as
   `compute`/`run` already do.  `compute_async` (Cell) and `task` (Transformer)
   exist today as near-misses; the design's names are `compute`/`computation`
   and this suite uses those.
3. **What should a missing-key projection do?**  See finding 5.
4. **Should the per-test reset move to `tests/conftest.py`?**  §15 A1 says A0
   must add it and that it gates A1.  It lives in `tests/claude/conftest.py`
   here, in one function, so lifting it is an import.  Verified: applying it to
   the whole existing `seamless-workflow` suite leaves all 74 tests passing.
   The pollution it prevents is an A1 phenomenon — it needs a failing derivation
   to leave unattributed refholder counts — so today it is prophylactic.  It can
   log `Refholder decref ignored ... already zero` when a failed test's frame
   keeps a Context alive past the reset; that message is an artifact of the
   reset, not a finding.

## Legacy mining notes

`~/legacy-seamless/tests` holds ~296 print-based characterization scripts, of
which 271 call `ctx.compute()`/`equilibrate` and 49 use `time.sleep`.  Porting
means turning expected stdout (`workflow/test-outputs/*.out`) into assertions.
What was taken:

| legacy | ported into | what carried over |
|---|---|---|
| `workflow/delay.py` (+ `test-outputs/delay.out`) | `latency/test_latency_delay_port.py` | two chained sleeping transformers, an edit, observations at fixed offsets; the arithmetic `a + 0.1*delay + 1000` so a stale result is a wrong number rather than a missing one |
| `workflow/bash.py` (+ `.out`) | `correctness/test_correctness_bash.py` | `head -$lines testdata > RESULT`; the `RESULT`-as-directory form arriving as a dict keyed by relative path |
| `workflow/module-simplified.py` (+ `.out`) | `correctness/test_correctness_modules.py` | a module declared as a plain dict, asserted through the transformer's result |
| `workflow/cascade.py` (+ `.out`) | `node-transition/test_transition_failure.py` | make a transformer invalid, then valid again; the downstream must not stay poisoned |
| `workflow/simple-missing.py` (+ `.out`) | `node-transition/test_transition_failure.py` | a downstream reports *upstream*, not an error of its own |
| `workflow/subsubcell.py`, `workflow/subcell.py` | `correctness/test_correctness_expressions.py`, `test_correctness_fanin.py` | deep projections of one root; four depths of one cell feeding one transformer (which is where the diamond defect surfaced) |
| `workflow-core/simple.py`, `simple-async.py` | `quiescence-barrier/` | the sync and async barrier pair, and the "introduce delay, observe, then wait" shape |
| `workflow/eager.py` | `node-transition/test_transition_invalidation.py`, `latency/test_latency_delay_port.py` | edit an upstream, observe that an intermediate cell never shows the previous value (legacy's own comment marks this case as FAILING there) |

Deliberately not ported:

* **`compute(timeout)`** — see *Decisions* above.
* **`preliminary`** (`workflow-core/preliminary.py`,
  `workflow-core/structured_cell/preliminary.py`).  Legacy injected
  `return_preliminary()` and `set_progress()` into the transformer namespace, so
  a running computation could emit intermediate values that became the node's
  value.  That makes `tf_checksum -> result_checksum` stop being a function, and
  writes indistinguishable intermediate buffers to the shared hashserver.  The
  need behind it is real; the design routes it to the runtime-status channel as
  a class-5 progress fact that never installs a node checksum.  When that
  channel exists, these two scripts port as progress assertions and belong with
  the A4 supersession tests.
* **Silk / structured-cell / schema behaviour** (`simplest.py`, `simpler.py`,
  most of `subcell.py`).  Legacy's `.example`, `.schema` and validator surface
  has no counterpart in the current Context, and the design does not introduce
  one; porting those assertions would invent a contract rather than record one.
* **Supersession** (the A4 table in §15) — out of A0's scope by construction: it
  needs the execution records that only exist once submissions go through the
  transformation cache.
