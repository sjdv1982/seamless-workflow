# A0 test suite: `codex/` vs `claude/` — comparison and consensus

Written by reading `seamless/attachments-and-mount-design.md` in full, reading every file in
both `tests/codex/` and `tests/claude/` in full, spot-checking the legacy-mining counts against
`~/legacy-seamless/tests`, and **running both suites** against the current implementation
(`conda activate seamless1`, one pytest process per file — the project convention both suites'
own fixtures assume). Findings below are marked **(run)** where I have direct pytest output and
**(read)** where they rest on source inspection only.

## Bottom line

**Use `tests/claude/` as the base and port exactly one thing out of `tests/codex/` into it: the
MOD-16 `Expression.input_ref` lifetime test.** Everything else `codex/` has that `claude/`
lacks is either already covered, or not worth the coupling it costs. This rests entirely on the
merits below — reading both suites in full and running both against the current implementation —
not on any appeal to the design document's own text about either suite.

**Note on §26 of the design doc.** An earlier version of this analysis leaned on
`attachments-and-mount-design.md` §26 ("Appendix — what building the A0 suite found") as
independent corroboration that `claude/` was the intended deliverable. That reasoning was
circular: the author confirms §26 was written post-hoc, by the same run that wrote
`tests/claude/`, specifically to record useful information from that run — it is the suite
documenting itself in the shared design doc, not the design doc's author picking a winner between
two independently-produced candidates. §26 is disregarded below as evidence for this comparison;
where a finding below cites a §26.x subsection it is only as a pointer to where the same fact
(independently verified by reading and running the actual test code) happens to also be written
down, not as support for the verdict.

## Scale

| | `codex/` | `claude/` |
|---|---|---|
| test files | 7 | 19 |
| test functions (parametrized cases counted) | 22 | 93 |
| lines, tests + fixtures | 671 | 2,805 |
| shared helper module | none (a local `_node()` re-defined per file) | `contract_helpers.py`, 237 lines |
| conftest | resets caches | resets caches + registers phase markers |
| README | none | 273 lines: status table, decisions, findings, open questions, legacy-mining table |
| runner | none (bare `pytest`) | `run-tests.sh` (one process per file, matches project convention) |
| phase markers (`now`/`a1`/`a2`/`a3`/`a4`/`slow`) | none | on every test |
| pass rate against current code **(run)** | 3/22 (14%) | 27/93 (29%, matches its own README table exactly on every file I re-ran) |

The pass-rate row is not by itself a quality signal — a suite that asserts less can "pass more"
trivially — which is why the rest of this document is about *why* each side's tests pass or
fail, not just the count.

## Where `codex/` has a structural problem, not just less coverage

### 1. Tests read the implementation's private internals, not the public contract **(read)**

Every `codex/` file defines and uses:

```python
def _node(context, name):
    return context._graph.nodes[(name,)]
```

and asserts on `.state`, `.block_reason`, `.current_checksum` directly off that private node
object. `claude/contract_helpers.py` reads state in exactly one place instead
(`runtime()`/`state()`/`states()`, all going through the **public** `ctx.get_graph(runtime=True)`),
with the reason stated in its own docstring: "so that a later phase changes one function rather
than every test." The design document's own plan is a five-phase rewrite of exactly this
internal representation (A1 through A4). `codex/`'s tests will need editing in all seven files
each time that representation changes shape; `claude/`'s will need editing in `contract_helpers.py`
once. For a suite whose stated purpose (§15 A0) is to survive as a fixed contract while the
implementation under it is torn up, this is not a style preference, it is the one property the
suite exists to have.

### 2. Two files assume a barrier API that does not exist yet, and it breaks them at the wrong layer **(run)**

`node-transition/test_async_node_transitions.py` and `quiescence-barrier/test_context_barrier.py`
call `context.compute()` / `context.computation()` directly. `Context.compute` does not exist
today — `Context.__getattr__` returns a non-callable `MissingView` for any unrecognized name — so
every call crashes:

```
TypeError: 'MissingView' object is not callable
```

Verified by running both files:

* `quiescence-barrier/test_context_barrier.py`: **5/5 tests fail**, every one via this identical
  crash. The file asserts nothing about barrier semantics (return-on-failure, timeout behaviour,
  wait-for-quiescence) — it only proves the barrier is missing, which one test would show.
* `node-transition/test_async_node_transitions.py`: **3 of 4 tests** call `context.compute()`
  mid-body and crash the same way before reaching the node-transition assertion they exist to
  make (revocation-on-edit, failure-propagation, unwired-propagation). Only the fourth test
  (`test_last_required_pin_starts_one_run_and_leaves_downstream_waiting`, which never calls
  `.compute()`) fails for the reason its name describes.

Net effect: **8 of `codex/`'s 19 current failures (42%) are the same one crash, wearing seven
different test names**, none of them informative about the §14.1/§14.2 contract the files claim
to test. This is also a design-doc-sequencing error independent of the API-existence question:
§14.1 (eligibility is not delivery) and §14.2 (explicit barriers) are two different sections
for a reason — the design doc's own phase plan expects node-transition tests to hold meaning
*before* barriers exist (they're the A1 exit evidence; barriers are A3/A4). Coupling them means
`codex/`'s node-transition file cannot be green at A1 even once the transition logic is correct,
because it will still be calling an A3/A4 API.

`claude/` hits the identical missing-API fact — `Context.compute` really is absent — and turns
it into a single, informative failure instead: `contract_helpers._bound()` looks the method up
on the class and raises

```
AssertionError: The Context-wide quiescence barrier does not exist: Context.compute() is not a
callable attribute of the class. §14.2 requires it; without it the caller has no way to wait for
a result that no longer arrives synchronously.
```

verified against `quiescence-barrier/test_barrier_context.py`. Same underlying fact, and unlike
`codex/`'s version it names what's missing and why it matters, and it never contaminates the
node-transition files (`claude/` puts everything barrier-dependent in `quiescence-barrier/`
exclusively).

### 3. Two tests port exactly the thing the design document says not to port **(run + read)**

§15 A0, verbatim: *"legacy `ctx.compute()` accepted a **timeout** (`compute(0.5)`)... **Do not
port the timeout**; port the requirement it was standing in for, which is an async barrier."*
§14.2, verbatim: *"Neither form takes a timeout."*

`codex/quiescence-barrier/test_context_barrier.py` has:

```python
def test_sync_timeout_bounds_only_the_wait_and_does_not_stop_progress(make_context):
    ...
    with pytest.raises(TimeoutError):
        context.compute(timeout=0.05)
    ...
    context.compute()
    assert context.slow.result.value == "eventual result"
```

and its async twin. Both hard-require `compute(timeout=...)` to exist as valid call syntax.
Raising `TimeoutError` on expiry is not *unsafe* by the design's own test ("a barrier never
returns a non-quiescent graph" — an exception doesn't violate that), but the design's explicit
instruction is that this parameter should not exist at all, and these two tests will error with
`TypeError: compute() got an unexpected keyword argument 'timeout'` forever if A3/A4 is
implemented as written, rather than resolving to green. (Confirmed both currently fail via the
unrelated `MissingView` crash before ever reaching the timeout assertion — see finding 2 — so
this defect is latent, not yet the proximate cause, but it will surface the moment `.compute()`
starts existing without a `timeout=` parameter.)

`claude/quiescence-barrier/test_barrier_no_pump.py` handles the identical legacy feature (legacy
`ctx.compute(0.5)`) by explicitly naming the design's own internal inconsistency — §14.2 says no
timeout, MOD-17 says "barriers need timeouts," and they turn out to be about two different
barriers (graph quiescence vs. the §21 external finite cut) — and then writes an assertion that
is correct under *either* resolution:

```python
try:
    context_compute(ctx, 0.5)
except TypeError:
    pass  # no timeout argument: the legacy pump was not ported at all
else:
    assert quiescent(ctx), "... the legacy timeout was ported as a pump"
```

This is the more defensive design and the one that actually follows the "do not port the
timeout" instruction, rather than reintroducing the parameter under a different justification.

### 4. A real, currently-live defect that `claude/` found is entirely absent from `codex/`'s coverage **(run)**

`claude/correctness/test_correctness_fanin.py` documents and tests an inverted cycle check in
`Context._add_edge`: `_would_cycle(source, target)` walks forward from *source*, so it wrongly
rejects a **second edge between the same (source, target) pair** as a cycle (`tf.pins.x = ctx.a;
tf.pins.y = ctx.a`), while a genuine two-node cycle (`ctx.b = ctx.a` then `ctx.a = ctx.b`) is
wrongly *accepted*. This is real — running the file gives **6 failed, 2 passed**, exactly the
split `claude/README.md` predicts, and it is now also `attachments-and-mount-design.md`
§26.3.1.

`codex/correctness/test_cell_expressions.py` has a test named
`test_wide_diamond_converges_without_losing_sibling_targets`, which sounds like the same case.
It is not: it builds `left = source.payload["value"]` and `right = source.payload["value"]` (one
source, **two different targets**), then `join.left = left; join.right = right` (**two different
sources**, one target). Neither step repeats a (source, target) pair, so it never reaches the
inverted check. Running it confirms this: **3/3 pass today**, including that test — `claude/`'s
own regression-net file has the identical passing shape
(`test_a_diamond_through_two_intermediate_cells_is_accepted`, marked `now`, with a comment
explaining exactly why it's the shape that *doesn't* trigger the bug). Nothing in `codex/`
resembles `tf.pins.x = ctx.a; tf.pins.y = ctx.a` or the minimal repro
(`test_a_repeated_edge_between_the_same_pair_is_not_a_cycle`). `codex/`'s suite would ship
without ever exercising, discovering, or regression-testing this defect, and its one
diamond-shaped test provides false reassurance that fan-in is fine.

### 5. The design document's own headline measurement has no instrument in `codex/` **(read)**

§15 A0 asks for the latency test that "makes the whole defect self-evident," and §26.1 turns the
downstream-connection case into the appendix's second named finding: connecting one downstream
transformer to a settled node re-executes the upstream body *seven more times* — "the cost of
adding a node to a settled graph is quadratic in bodies run." That number only exists because
`claude/contract_helpers.ExecutionLog` counts real executions through a file (deliberately not an
in-process integer, "since execution moves to a worker at A4"), used in
`latency/test_latency_downstream.py` and `node-transition/test_transition_cache_hit.py`.
`codex/latency/test_nonblocking_mutations.py` asserts only wall-clock bounds
(`mutation_elapsed < PROMPT_LIMIT`, `barrier_elapsed >= SLEEP_SECONDS - 1.0`) — exactly the
approach §15 A4 itself warns against for the supersession tests ("wall-clock is flaky under load
... the built-in instrument is the ... execution record"). `codex/` cannot state, let alone
regression-test, the quadratic-re-execution finding, or the "no exception for cache hits" rule
(§14.1) that `claude/node-transition/test_transition_cache_hit.py` exists specifically to pin.

### 6. No phase markers, no legacy-mining ledger **(read)**

`codex/` has no `pytest.mark.now/a1/a2/a3/a4`, so nothing declares *when* a given red test is
expected to turn green — the exact thing §15 A0 asks the suite to record, and the thing that
turns a red test from "looks broken" into "a dated promise" (`claude/README.md`'s phrase). It
also has no legacy-mining record: `claude/README.md` maps eight specific legacy scripts to the
tests that ported them and explains three deliberate non-ports (`compute(timeout)`,
`preliminary`, Silk/schema) with reasons tied to the design's own routing (progress → runtime
status channel, not value channel). `codex/`'s docstrings mention "port of legacy X" in three
places but there's no way to audit what of the ~296-script legacy suite was considered and
rejected versus simply not looked at.

### 7. The conftest reset is the less defensive of the two, in a way the design doc itself warns about **(read)**

`codex/conftest.py` resets process-global caches by **replacing the singleton instance
wholesale**: `transformation_cache._transformation_cache_instance =
transformation_cache.TransformationCache()`. `claude/conftest.py` calls targeted clear methods on
the *existing* instance and explicitly does not touch `_active_submissions`, with the reason
given inline: "those are live submissions, and clearing the dict would orphan running work
rather than reset it." That is a direct, self-applied instance of the principle
`attachments-and-mount-design.md` MOD-5 states for the Context's own runtime state: never
snapshot/replace live runtime state, only supersede it forward. I confirmed
`get_transformation_cache()`/`get_buffer_cache()` are lazy accessors re-reading the same module
global `codex/` reassigns, so this is harmless for the currently entirely-synchronous test
bodies — but it is the fragile pattern, and it stops being harmless the moment any test leaves
real async work in flight at teardown (which A4 makes routine).

## Where `codex/` is right and `claude/` should take something from it

### The MOD-16 lifetime test — `claude/` has none; `codex/` does, and it's good

`§15 A0` explicitly requires: *"Fix `Expression` to claim its `input_ref`, and add a lifetime
test (**[MOD-16]**)."* `claude/README.md` lists this fix and its test under "What A0 still needs
that is not in this directory" — an honestly documented gap, not a silent one, but a gap.
`codex/correctness/test_expression_lifetime.py` fills exactly this gap:

```python
expression = context.value.build()
for index in range(5):
    context.value = {...}
context.prune()
assert expression.input_ref == original_checksum
...
force_expiry(original_checksum)
assert expression.run() == original_value
```

This is the measurement §7/MOD-16 describes almost exactly (five reassignments, a prune, then
proof the lease survived by force-expiring the buffer and re-running). It also correctly reuses
the pre-existing shared `tests/helpers/reference_lifecycle.py` shim (which forwards to
`seamless-core/tests/helpers/reference_lifecycle.py`) rather than reimplementing expiry-forcing —
good reuse discipline. Running it today gives a `KeyError` rather than a clean assertion failure
(`claims[original_checksum]` — the checksum has no entry at all, since nothing claimed it), which
is a minor robustness nit (`claims.get(original_checksum, [])` would fail more legibly) but the
underlying test is sound and it is coverage `claude/` is simply missing.

### The non-Python correctness tests are clean and independently found a defect the design doc lists

`codex/correctness/test_non_python_transformers.py` — bash, compiled, modules, environment — all
five tests fail today, and I confirmed (running with full output) all five fail for genuinely
on-target reasons, including independently reproducing the exact compiled-transformer defect
§26.3.4 documents: `AttributeError: Unknown transformer pin 'a': the transformer signature has no
such parameter (declared pins: args, kwargs)`. This is essentially the same ground
`claude/correctness/test_correctness_compiled.py` covers, arrived at independently, and it's
correct.

### Compactness has some real value

`codex/latency/test_nonblocking_mutations.py` folds the design doc's whole latency script
(mutate without blocking → stays computing → connect downstream without blocking → barrier
waits → completes) into one test, economically. For a phase whose exit evidence is "a red suite
that describes the intended contract," a reviewer can read all of `codex/` in the time it takes
to read three `claude/` files. That is a real cost `claude/` pays for its thoroughness, even
though I don't think it's the more expensive mistake here (see findings 1–3: the coupling and
API-assumption problems cost more than the extra reading time saves).

## Consensus recommendation

1. **Keep `tests/claude/` as the canonical A0 suite, on the merits.** It passes/fails exactly as
   its own README claims (spot checked essentially every file against the current implementation);
   its failures are, file for file, informative about the actual §14.1/§14.2 contract rather than
   about missing test-infrastructure; and it avoids the coupling and API-assumption defects found
   in `codex/` (findings 1–4 above). (§26 is not part of this justification — see the note above.)
2. **Port `codex/correctness/test_expression_lifetime.py` into `claude/`**, rewritten to go
   through `contract_helpers` and carry a phase marker (`a1`, since MOD-16 is independent of
   synchronicity-breaking) — this is the one concrete coverage gap in `claude/` that `codex/`
   actually closes. Fix the `KeyError` → a clean `.get(..., [])` assertion while porting it.
3. **Do not port `codex/`'s barrier or node-transition files as-is.** Rewrite anything worth
   keeping from `test_async_node_transitions.py` through `contract_helpers.state()`/`states()`
   rather than `ctx._graph`, and strip the `context.compute()`/`.computation()` calls out of
   node-transition assertions entirely (they belong only in `quiescence-barrier/`, once a barrier
   exists to call).
4. **Do not adopt the `compute(timeout=...)` assumption.** If the project wants an opinion on
   MOD-17 vs §14.2 going in *before* A3 lands, decide it explicitly as an open-decision item
   (§24), rather than letting either suite's test shape pick it by accident.
5. **Retire `tests/codex/` once (2) is ported**, rather than maintaining two suites against one
   contract — the design doc's own phase plan already treats one evolving suite as the A0/A1/A2
   exit evidence, and a second one with a different state-access strategy is drift, not
   redundancy.
6. **Neither README documents the `conda activate seamless1` requirement** — both assume it
   silently. Worth a one-line addition to `claude/README.md`'s "Running it" section regardless of
   the above.

## Verification log

Run under `conda activate seamless1`, one `pytest` process per file (per both suites' own
convention):

* `codex/`: all 7 files run; 22 tests total, 3 passed / 19 failed. Of the 19 failures, 8
  (`node-transition/test_async_node_transitions.py` ×3, `quiescence-barrier/test_context_barrier.py`
  ×5) are the identical `TypeError: 'MissingView' object is not callable`; the other 11 fail for
  on-target reasons matching the design doc's documented current-state defects.
* `claude/`: re-ran `node-transition/*`, `quiescence-barrier/*` (non-slow subset),
  `correctness/*` — every file's pass/fail count matched `claude/README.md`'s status table
  exactly (e.g. `test_correctness_fanin.py` 6 failed/2 passed, `test_correctness_expressions.py`
  1 failed/9 passed, `test_transition_unwired.py` 2 failed/4 passed). Spot-ran two `slow`-marked
  files with reduced `SEAMLESS_CONTRACT_SHORT_BODY_SECONDS` to confirm they execute and fail for
  on-target reasons rather than erroring.
* Legacy mining count: `grep -rl '\.compute(\|equilibrate' ~/legacy-seamless/tests` gives exactly
  271 files, matching both `attachments-and-mount-design.md` §15 A0 and `claude/README.md`
  precisely. `time.sleep` count came back 55 by naive grep against the documents' 47/49 — close
  enough to reflect exclusion of non-test files from the original mining pass, not worth treating
  as a discrepancy.
