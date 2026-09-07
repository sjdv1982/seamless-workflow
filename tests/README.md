# `seamless-workflow` tests

Two things live here, in one directory and under one `conftest.py`:

* the **pre-existing suite** — the top-level `test_*.py` files, 74 tests, green;
* the **phase-A0 contract suite** — `node-transition/`, `quiescence-barrier/`,
  `latency/` and `correctness/`, 108 tests, **red on purpose** — the
  test-writing half of phase A0 of
  [`seamless/attachments-and-mount-design.md`](../../seamless/attachments-and-mount-design.md)
  (§15), mined against the legacy suite in `~/legacy-seamless/tests` as that
  section instructs.

A0's exit evidence is *"a red suite that describes the intended contract"*.
Nothing in the contract suite is `xfail`-marked: an `xfail` would hide exactly
the signal the phase exists to produce, and — as §15 A1 notes — an `xfail`ed test
still runs and still pollutes process-global state.  Strict `xfail` markers
arrive at A1, on the suites the design names there.

Each contract test instead carries the phase at which it is expected to turn
green, as a selectable pytest marker.  A red test whose marker is `a4` is not a
bug report; it is a dated promise.  The top-level files carry no marker: they
predate the phases and are unconditional.

## Running it

Requires the `seamless1` environment, which puts the sibling repositories
(`seamless`, `seamless-core`, `seamless-transformer`, `seamless-config`,
`seamless-database`, `seamless-signature`, `seamless-workflow`) on the path.

```bash
conda activate seamless1
cd seamless-workflow/tests
./run-tests.sh                 # one pytest process per file (project convention)
./run-tests.sh -m "not slow"   # skip the sleeping transformer bodies
./run-tests.sh -m now          # the regression net, which must always be green
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
(1), `SEAMLESS_CONTRACT_SETTLE_TIMEOUT` (60, the default only — most call sites
pass an explicit bound), `SEAMLESS_CONTRACT_POLL_INTERVAL` (0.02).  Without `gcc`
or without
`seamless_signature` importable, `correctness/test_correctness_compiled.py`
skips rather than failing for an environmental reason.

## Layout

| path | subject |
|---|---|
| `conftest.py` | per-test cache and refholder-registry reset (whole directory); marker registration; `make_context` |
| `contract_helpers.py` | what the API cannot spell: the graph-wide snapshot (`runtime`, `states`, `quiescent`), the two oracles (`settle`, `try_settle`), the `timed` stopwatch, and `compute_or_settle` — the one dated exception |
| `helpers/reference_lifecycle.py` | shim onto `seamless-core/tests/helpers`, for forcing buffer expiry |
| `test_*.py` | the pre-existing suite: assignment semantics, handles, pins, GC, refholder lifecycle, runtime and prune, state machine, transformer binding |
| `node-transition/` | §14.1 — eligibility is not delivery; invalidation; failure and unwired propagation; cache hits |
| `quiescence-barrier/` | §14.2 and §27 — `ctx.compute()`, `ctx.a.compute()`, their async forms, the `timeout` contract, and the legacy pump |
| `latency/` | §15 A0 — the test "that makes the whole defect self-evident", and the re-execution measurement through the observation log |
| `correctness/` | [MOD-15] non-Python and envelope correctness; [MOD-3] expression regression net; [MOD-16] expression lifetime; diamonds |

## Status and phase expectations

Measured against the current implementation, one process per file:

| file | tests | pass now | red | target |
|---|---|---|---|---|
| `node-transition/test_transition_eligibility.py` | 7 | 2 | 5 | A1 |
| `node-transition/test_transition_invalidation.py` | 9 | 4 | 5 | A1, one A4; three `now` negatives |
| `node-transition/test_transition_unwired.py` | 6 | 4 | 2 | A1 |
| `node-transition/test_transition_failure.py` | 5 | 2 | 3 | A4 |
| `node-transition/test_transition_cache_hit.py` | 2 | 0 | 2 | A4 |
| `quiescence-barrier/test_barrier_context.py` | 6 | 0 | 6 | A3, three A4 |
| `quiescence-barrier/test_barrier_node.py` | 5 | 2 | 3 | A4 |
| `quiescence-barrier/test_barrier_async.py` | 5 | 0 | 5 | A4 |
| `quiescence-barrier/test_barrier_timeout.py` | 8 | 0 | 8 | A3, seven A4 |
| `quiescence-barrier/test_barrier_no_pump.py` | 2 | 1 | 1 | A1 |
| `latency/test_latency_last_pin.py` | 4 | 1 | 3 | A1, one A4 |
| `latency/test_latency_downstream.py` | 4 | 0 | 4 | A1, two A4 |
| `latency/test_latency_delay_port.py` | 3 | 0 | 3 | A1, two A4 |
| `correctness/test_correctness_bash.py` | 5 | 0 | 5 | A1, four A4 |
| `correctness/test_correctness_compiled.py` | 5 | 1 | 4 | A4 |
| `correctness/test_correctness_modules.py` | 4 | 1 | 3 | A4 |
| `correctness/test_correctness_environment.py` | 3 | 2 | 1 | A4 |
| `correctness/test_correctness_no_code.py` | 4 | 0 | 4 | A1 |
| `correctness/test_correctness_expressions.py` | 11 | 10 | 1 | A3 |
| `correctness/test_correctness_expression_lifetime.py` | 2 | 0 | 2 | A1 ([MOD-16]) |
| `correctness/test_correctness_fanin.py` | 8 | 2 | 6 | A2 |
| **total** | **108** | **32** | **76** | |

By marker: `now` 28, `a1` 24, `a2` 6, `a3` 5, `a4` 45; `slow` 32.

The 28 tests marked `now` are the regression net — including the three
invalidation *negatives*, which are stated as "unchanged across the edit" rather
than as "still complete" so that they are discriminating today and vacuously true
in the A1–A3 limbo: pure propagation completing
in-turn, `unwired` on a missing pin, the sub-path expression behaviour that
[MOD-3] must preserve while deleting the private evaluator, the durable half of
`modules` and `environment`, and the node barrier's existing return value (which
`test_canonical_handles.py:143` already pins).  **They must stay green through
every phase**, alongside the 74 top-level tests.

Four of the 32 passing tests are marked `a1`/`a4` rather than `now`, because they
are green today for a reason the A1–A3 limbo removes: they observe a *completed*
transformer, and in limbo no transformer completes.  Expect them to go red at A1
and green again at A4.  That is the limbo working, not a regression.

Two measurements from that run are worth keeping.  Setting the last pin of a
transformer blocks the caller for a full body duration and returns `complete`
with a result checksum already installed — the defect §15 A0 is built around.
And connecting one downstream transformer to a *settled* node re-executes the
upstream body **seven more times**: `latency/test_latency_downstream.py` records
eight `cache-miss` observations for one transformation checksum where one is
correct, because `_derive_all` re-derives every node once per convergence pass,
makes up to one pass per node, and derivation is execution.  The cost of adding a
node to a settled graph is therefore quadratic in bodies run.  The log shows the
convergence passes directly, `tf` and `tail` interleaving, which the old scalar
count could not.

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

**Barriers take a `timeout`, and expiry raises.**  §27 settles what §14.2 and
[MOD-17] disagreed about: all four barrier forms take an optional `timeout`,
defaulting to `None`.  On expiry the barrier raises `TimeoutError` and never
returns, which is what keeps the underlying invariant intact — **a barrier never
returns a non-quiescent graph** — and what separates a deadline from the legacy
pump.  `quiescence-barrier/test_barrier_timeout.py` pins the four rules of §27.1
plus the predicate-accumulation consequence of §27.1.5;
`quiescence-barrier/test_barrier_no_pump.py` keeps the other half, in which
nothing calls a barrier at all and the graph settles anyway.

What legacy's `ctx.compute(0.5)` conflated is unbundled rather than dropped: the
controller thread (§4) deletes the **pump**, a plain `sleep` replaces the
**observation window**, and the **hang guard** survives as the `timeout`
parameter — in the API, because §27.2 shows it is not expressible outside it
(the sync form has no caller-side mechanism, and `asyncio.wait_for` abandons the
async form's predicate rather than withdrawing it).

**There are three ways to wait, and they are different things.**  A test waits
through `compute_or_settle(ctx)` — `ctx.compute()`, what a user would write —
unless it has one of three reasons not to.  17 tests use it, and they are barrier
coverage by construction: from A3 on, a barrier that hangs, returns early or
returns a non-quiescent graph fails them across three directories, not only in
`quiescence-barrier/`.  Until the barrier exists it falls back to polling, so
that seventeen on-target failures (the [MOD-15] bash result, the module-edit
invalidation, the block-reason degradation) are not replaced by seventeen copies
of one missing-attribute message.  Barrier *existence* is asserted once, in
`test_barrier_context.py`; barrier *behaviour* is asserted everywhere.

`compute_or_settle` is the **only** helper that stands in for a spelling a user
would write, and it is dated: at A3 the fallback becomes dead code, the body
becomes `ctx.compute()`, and the helper is deleted along with its call sites.
It survives because at *its* call sites the barrier is a precondition.  Where the
barrier is the subject — all of `quiescence-barrier/`, and every one of the 23
sites that used to go through `context_compute` / `node_compute` and their async
pair — the tests now call `ctx.compute()`, `ctx.a.compute()`,
`await ctx.computation()` and `await ctx.a.computation()` by name and are red
until those exist.  There the missing-attribute message *is* the subject.

`settle(ctx)` is the remaining 7.  It is an **oracle**, not a reference
implementation of `ctx.compute()`: it shares exactly one of the barrier's
postconditions — the graph reached quiescence — and none of the rest.  It never
enters the controller, so it cannot stand in for [MOD-17]'s "installs a
predicate, does not stall the frontier"; it returns nothing, so it cannot stand
in for §10's correlated barrier-plus-read; and polling can report a quiescence
that was never a stable state.  It is used for three reasons and no others:

1. *the barrier does not exist yet* — preconditions in `now`/`a1`/`a2` tests,
   which must be able to go green at their own phase rather than at A3;
2. *the barrier is the subject* — a test asserting something about `compute`
   cannot reach its own precondition through `compute` and stay falsifiable;
3. *the barrier would contaminate the measurement* — the tests that measure what
   ran.  `latency/test_latency_downstream.py` counts transformations through
   `seamless_transformer.observation`;
   `node-transition/test_transition_cache_hit.py` uses that count *and* the
   clock, so a barrier running a derivation pass would corrupt both of the two
   mechanisms that exist to cross-check each other.  This reason is independent
   of the phase markers and is the one to guard.

`try_settle(ctx)` is the last 6: graphs that are **not expected** to settle — the
A1–A3 limbo, and the unsatisfiable environment.  They want to give the graph
every chance to be wrong before asserting that it is not, so failing to settle is
their normal case.  `settle()` raises on expiry, which is §27.1.2 applied to the
suite's own instrument; before the split, the difference between "must settle"
and "settle if you can" was whether someone had typed `assert` in front, which is
not a difference a reader can see.

**Transition assertions are on `.state`, and `.status` no longer exists.**
§15 A0 says not to assert on it, because `_PUBLIC_STATUS` mapped `waiting` and
`computing` to the same string.  Rather than work around that, the project
removed it: `.state` and `.block_reason` carry strictly more than it did — six
states and two block reasons against five strings — and the display summary it
provided moved into `Cell.__repr__` and a new `Transformer.__repr__`, which is
where a REPL user actually looks.  Three further reasons are in the commit: the
string was never the legacy contract either (legacy had `Status: executing` with
a progress percentage, plus `preliminary`, `invalid` and `undefined`), it
collided with the live and disjoint `Transformation.status`
(`ready`/`canceled`/`exception`), and it was absent from the current API docs.
`node-transition/test_transition_eligibility.py::test_no_public_string_collapses_waiting_and_computing`
guards the removal in both directions.

`.state` and `.block_reason` are **new**, added by this suite to bound `Cell` and
bound `Transformer` next to `status`, `checksum` and `exception`.  This is the
[MOD-14] category — observability added to the product so behaviour can be
pinned — and A0's charter covers it.  Before it, the six-state vocabulary was
public only in the graph-wide shape `get_graph(runtime=True)`, and the suite read
it through a `state(ctx, path)` helper.  Two things were wrong with that.  A test
is the worked example of its API, and no reader of a test learns a spelling that
the test does not use; and the names were *traps* rather than merely absent —
`ctx.a.state` resolved to a sub-path projection of the cell's value, so
`assert ctx.a.state == "complete"` failed with no error and no meaning, and
`ctx.tf.state` raised `AttributeError`.  Claiming the names is a fix, not sugar.

A cell whose value happens to hold a `"state"` key still reaches it, as
`ctx.a["state"]` — item syntax is the escape hatch, so the legacy `.self` API has
no counterpart to earn here.  On a `Transformer` the question does not arise:
pins live under `.pins` and there is no `__getattr__` fallback at all
(`transformer_class.py:533`), so a bad name there is an `AttributeError` with
Python's own "Did you mean" suggestion.

**A `Cell` cannot report a bad name that way, so its projections do it instead.**
`Cell.item`/`Cell.slice` now return a `SubCell`, and a `SubCell` raises
`ProjectionError` on comparison, ordering, truth-testing, `len` and iteration —
every operation that treats a handle as a value.  Without it, removing a name
from `Cell` would not remove it: `ctx.a.status` would resolve to a projection of
the key `"status"`, so `== "Status: OK"` would be silently unequal, `!=` and
`assert ctx.a.status` would silently pass, and `print` would show something that
looked like a cell.  This is what makes the `.status` removal safe, and it
generalises: it catches `ctx.a.vlaue` and every future removed name, not only the
one somebody remembered to deprecate.  One gap has no fix — `x is None` compiles
to `IS_OP` and compares pointers, so `assert ctx.a.vlaue is not None` still
passes silently.  `ProjectionError` subclasses both `TypeError` (what it is: the
attribute lookup *succeeded*) and `AttributeError` (what it is about, and what
the `Transformer` raises for the same mistake).

**A helper that spells something the user would spell differently is a bug; a
helper that spells something the user cannot spell at all is an instrument.**
Seven wrappers were deleted under that rule — `state`, `block_reason`,
`result_checksum`, `context_compute`, `context_computation`, `node_compute`,
`node_computation` — and `pending_nodes` and `wait_until` became private.  What
survives is a graph-wide snapshot (simultaneity is the one thing a per-node read
cannot give, and it is open question 8 of §24.8), two oracles that poll without
entering the controller, a stopwatch, and `compute_or_settle`.

## The transformation observation log

`seamless_transformer.observation` is off-by-default product machinery that
records **one line per transformation reaching an execution decision**:

```text
<transformation checksum>	<label>	<cache-hit|cache-miss>
```

The label says whose transformation it was: a dotted node path (`tf`, `sub.tf`)
for a workflow-bound transformer, `code:<12 hex of the code checksum>` for an
unbound one — there is no transformer id in the codebase to borrow, and the code
checksum is what identifies a transformer across its own varying inputs.  Tests
switch it on through the `transformation_observations` fixture and assert on
`misses(label)` and `hits(label)`, which answer different questions: a miss is an
execution, a hit is a re-submission the cache absorbed.

`node-transition/test_transition_cache_hit.py` keeps **both** mechanisms, because
they answer different questions and can disagree: the count says whether the body
ran, the clock says whether the caller waited.  A hit that still costs a body
duration to deliver passes the count and fails the clock; a second execution that
happens to be cheap passes the clock and fails the count.  The count is the
primary assertion — §15 A4 warns that wall-clock "infers what you want rather
than measuring it" — and the clock corroborates it.

It replaced a counter that the transformer body wrote to itself, with the log
path passed as an ordinary pin.  That worked, but it put the instrument *inside*
the transformation identity, and its failure was asymmetric in the wrong
direction: an identity that accidentally differs produces a spurious
re-execution and a loud failure, while an identity that accidentally *collides*
produces a spurious cache hit, a low count, and a **passing** test.  An
instrument whose failure mode is silent success, in the same property the test is
about, is the wrong instrument however elegant.

There are two recording sites on purpose.  `transformation_cache` records what
goes through the cache.  `Context._derive_transformer` records its *direct* call
to the Python callable — which never reaches the cache at all, and is the A0
defect — so a cache-side instrument alone would report zero for everything this
suite measures.  A1 deletes that call site and its recording with it.  This is
the same category as **[MOD-14]**: observability added to the product so
behaviour can be pinned before it is removed.

## What A0 still needs that is not in this directory

This is the test-suite half of A0.  Still outstanding from §15 A0:

* the materialisation recording mode of **[MOD-14]** (`seamless.diagnostics.record_materialisation`)
  and the exact expected logs per public operation — the "green log that
  describes the current behaviour" half of A0's exit evidence;
* the one-branch fix of **[MOD-15]** (stop reporting `complete` for a node with
  no executable code).  `correctness/test_correctness_no_code.py` is its
  acceptance test and is red;
* the **[MOD-16]** fix making `Expression` claim its `input_ref`.  Its lifetime
  test is now here — `correctness/test_correctness_expression_lifetime.py`, red —
  so only the fix is outstanding;
* the semantic decisions **[MOD-2]**, **[MOD-4]**, **[MOD-7]** (**[MOD-1]** is
  already decided, and **[MOD-17]** vs §14.2 is settled by §27).

## Findings from writing these tests

Defects that this suite encodes and the body of the design does not list.  Each
has a test; none required more than reading the code the test walked into.  They
are §26.3 of the design document.

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
5. **A missing key produces a node that is `complete` with no checksum**
   (`correctness/test_correctness_expressions.py`).  The *checksum* does
   distinguish a projection of an absent key from one of a key holding `null` —
   the latter carries the checksum of `null`, the former carries none — but
   `state` and `.value` are identical (`complete`, `None`) for both, so a reader
   who checks either sees a well-formed null.  The consequence one hop down is
   worse than the cause: a transformer whose pin is fed by the typo'd
   projection reports `unwired` — *a pin is not connected* — when the pin is
   connected and it is the key behind it that does not exist.  [MOD-3] moves
   sub-path reads onto `evaluate_expression`, which is where the distinction can
   be made; the tests pin the surviving distinction as a regression net and
   require only that a node with no checksum stop calling itself `complete`.
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
8. **`Checksum.__eq__(None)` raises.**  `Checksum(None)` is a `TypeError`, and
   `__eq__` constructs before comparing, so any assertion that compares a
   checksum against a possibly-absent one errors instead of answering.  The
   suite therefore states the revoked case as `checksum is None` rather than as
   an inequality against a checksum that may itself be absent.  This is a test-writing hazard rather
   than a defect in the design, but it is a sharp one: it turns a passing
   contract into a red test that looks on-topic, which is how finding 5 was
   mis-stated on the first pass.

## Open questions for whoever implements the phases

1. **Does the node-local barrier raise or return on a `blocked`/`unwired` node?**
   §24.4 settles the Context-wide form as *returns*.  The node-local form raises
   `NodeError` today; `test_barrier_node.py` pins that so the answer is chosen
   rather than drifted into.
2. **`computation` is a name that has to be claimed.**  On a bound `Cell`,
   attribute access is sub-path projection, so `ctx.a.computation` currently
   resolves to a projection of `a`'s value and `ctx.a.computation()` builds an
   `Expression` — the same trap `state` was in before this suite claimed it.  The
   async barrier has to take the name as a real method, as `compute`/`run`
   already do.  `compute_async` (Cell) and `task` (Transformer) exist today as
   near-misses; the design's names are `compute`/`computation`, this suite uses
   those, and `quiescence-barrier/test_barrier_async.py` now fails on the
   unclaimed name directly rather than reporting it in a docstring.
3. **What should a missing-key projection do?**  See finding 5.  §27 does not
   touch it; [MOD-3] chooses the answer.
4. **How is a timed-out barrier's predicate withdrawn?**  §27.1.5 requires it;
   `test_repeated_expiry_does_not_accumulate_predicates` states the consequence
   a test can see from outside, but the withdrawal message itself is a class-1
   design detail for A3.

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
| `ctx.compute(0.5)`, across the suite | `quiescence-barrier/test_barrier_timeout.py` | the **hang guard** only: a bounded wait that raises.  The pump and the observation window are not ported — see *Decisions*, and §27.3's note that §15 A0's "do not port the timeout" still holds as a statement about the pump |

Deliberately not ported:

* **The `compute(timeout)` pump** — the parameter is back (§27), the semantics
  are not.  Legacy's timeout advanced the graph and returned early; this one
  advances nothing and raises.
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

## How this directory was assembled

Two suites were written independently against §15 A0 — `tests/codex/` and
`tests/claude/` — and three arbitrations compared them
(`consensus-gemini.md`, `consensus-gpt-5.5.md`, `consensus-sonnet.md`, kept here
as the record).  All three converged: take `claude/` as the base, on the grounds
that it reads node state through one helper rather than through `ctx._graph` in
every file (that helper has since been replaced by `.state` on the handles), carries phase markers, and counts executions through a file rather
than through wall-clock; take `codex/`'s [MOD-16] lifetime test, which `claude/`
had documented as owed rather than written, and its `make_context` cleanup
fixture; keep `claude/`'s in-place cache reset rather than `codex/`'s singleton
replacement.

The fold applied that, and departed from it in one place, on the project's
instruction: **the `timeout=` parameter is adopted** rather than rejected, and
the design document gained §27 to say so.  `codex/`'s two timeout tests were the
shape that survives — expiry raises, the computation is untouched, a later
barrier returns the result — and they are ported into
`quiescence-barrier/test_barrier_timeout.py` with the four other §27 rules.

Also applied while folding: the `Checksum`-vs-`None` comparison hazard in
`test_correctness_expressions.py` (finding 8, which changed what finding 5 says),
the `seamless_signature` prerequisite on the compiled tests, and lifting
`conftest.py` to this directory so the reset covers the pre-existing suite too —
§15 A1 requires that, and all 74 top-level tests stay green under it.

The §27 overrule then forced a second pass over the waiting instrument, which
had been a single `settle()` used at all 43 call sites.  With `timeout=` in the
API, a bounded poll that returned `False` was the suite contradicting the rule it
had just been asked to pin, and the six call sites that discarded that `False`
deliberately were indistinguishable from a typo.  Splitting it three ways —
`compute_or_settle` (17 tests), `settle` (7), `try_settle` (6) — moved the
majority onto the product API without losing a single on-target failure message:
the tally before and after the split is identical at 29 green, 74 red, and the
[MOD-15], module and block-reason failures still name their own subject.
