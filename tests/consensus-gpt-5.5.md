# Consensus review: phase A0 contract suites

I compared the two generated suites against
`seamless/attachments-and-mount-design.md`, especially sections 14 and 15, and
the appendix that records what the first A0 suite found. The generated trees are
in `/home/agent/seamless1/seamless-workflow/tests/{codex,claude}`. The requested
`/home/agent/seamless-workflow` path now resolves to that workflow tree.

## Executive recommendation

Use `tests/claude` as the baseline for the consensus suite. It is much closer to
the design document: it has a shared contract helper layer, phase markers,
legacy-mining notes, a runner, explicit current-vs-future expectations, and much
broader coverage of the A0 surface.

Do not discard `tests/codex`. It contains one important missing test:
`correctness/test_expression_lifetime.py`, which directly exercises [MOD-16]
(`Expression` must claim its `input_ref`). Claude's README lists that test as
still owed. Codex also has a useful `make_context` cleanup fixture shape. Fold
those ideas into the Claude baseline, but keep Claude's safer cache reset logic.

In short:

1. Start from Claude.
2. Add Codex's [MOD-16] expression lifetime test.
3. Add a context cleanup fixture inspired by Codex, without replacing live cache
   singleton objects.
4. Fix a few Claude sharp edges called out below.
5. Drop Codex's graph-barrier timeout tests and direct private-state style.

## Design obligations from A0

A0 is not meant to make the implementation green. Its test-writing half should
produce a red contract suite plus current-regression coverage. The important
coverage obligations are:

- Node transitions from section 14.1: eligibility is not delivery; newly
  eligible transformer work becomes pending, with no result checksum; invalidation
  revokes the downstream cone; unwired and failed nodes propagate the right block
  reasons; cache hits are still not allowed to complete in-turn.
- Quiescence barriers from section 14.2: `ctx.compute()`, `await
  ctx.computation()`, `ctx.a.compute()`, and `await ctx.a.computation()` are
  barriers, not pumps. Context-wide barriers return on terminal `blocked` or
  `failed` graphs. Node-local barriers must be explicitly specified.
- Latency: a sleeping transformer must not block the public mutation that makes
  it eligible; mid-computation state must be observable without calling a barrier;
  adding downstream or unrelated graph structure must not re-execute already
  settled upstream work.
- Correctness: non-Python execution, modules, environment, compiled
  representation/execution, Cell-only expression regressions, fan-in/diamond
  graphs, no-code false completion, and the [MOD-16] expression lifetime defect.
- Legacy mining: stdout/status scripts should be converted into assertions, but
  the legacy `compute(timeout)` pump and `preliminary` value-channel semantics
  should not be ported as-is.

## Evidence gathered

With the sibling repos on `PYTHONPATH`:

- Codex collects 22 tests.
- Claude collects 93 tests.
- Claude's `now` marker passes: 24 passed, 69 deselected.
- Claude's `not slow` run is intentionally red: 26 passed, 41 failed, 26
  deselected.
- Codex full run is also intentionally red: 3 passed, 19 failed.
- Codex's expression lifetime test currently fails with the expected missing
  `input_ref` claim, so it is a real [MOD-16] acceptance test.

The generated `__pycache__` directories should not be carried into any consensus
commit.

## Codex suite assessment

Codex's suite is compact and readable. It covers all four requested directories,
has useful high-level scenarios, and includes tests for node transitions,
barriers, latency, non-Python transformers, modules, environment, compiled
transformers, Cell expressions, and [MOD-16] expression lifetime.

Its best contribution is
`codex/correctness/test_expression_lifetime.py`: it builds an `Expression`,
moves the Context on, prunes, checks that the escaped expression owns the
original input checksum, forces expiry, and then runs the expression. That is
exactly the lifetime defect the design calls out and Claude has not yet encoded.

The `make_context` fixture is also useful because it gives tests a single place
to release or close Contexts. That should be retained in spirit.

The main weaknesses are structural:

- No README, legacy-mining table, phase markers, or run script. A red failure
  does not tell the reader whether it is red for A1, A3, A4, or because the test
  itself is wrong.
- Tests repeatedly reach into `context._graph.nodes`. The design needs internal
  node state, but Claude's `get_graph(runtime=True)` helper is a better seam.
- Several tests combine A1, A3, and A4 expectations in one function. That makes
  failures less diagnostic during limbo.
- The barrier timeout tests assume `context.compute(timeout=...)` and
  `context.computation(timeout=...)`. That conflicts with the design's decision
  not to port legacy graph-barrier timeouts. The right assertion is Claude's:
  a barrier must never return a non-quiescent graph; caller-side timeouts belong
  outside the API, for example with `asyncio.wait_for`.
- The cache reset fixture replaces process-global singleton objects, including
  the transformation cache object. That risks orphaning live submissions. Claude
  deliberately clears cache contents but does not clear active submissions or
  swap the singleton out.
- The environment test depends on a specific `seamless1` conda environment and
  `CONDA_DEFAULT_ENV` behavior. Claude's positive/negative `which` tests are
  less machine-specific and more discriminating.
- The expression coverage misses the fan-in/diamond defect. Codex's "wide
  diamond" uses two intermediate source nodes, which is the case that already
  passes; it does not catch the repeated-edge/inverted-cycle bug.
- It has no cache-hit transition test and no file-backed execution counter for
  the downstream re-execution defect.

## Claude suite assessment

Claude's suite is the stronger A0 artifact. It has 93 tests across the exact
requested directories, plus `contract_helpers.py`, `conftest.py`, `README.md`,
and `run-tests.sh`.

The biggest strengths:

- `contract_helpers.py` centralizes the awkward contract readouts: internal
  state through `get_graph(runtime=True)`, barrier lookup that avoids
  `MissingView` confusion, no-pump observation helpers, timing, and a file-backed
  execution log.
- Phase markers (`now`, `a1`, `a2`, `a3`, `a4`, `slow`) make the red suite
  navigable through the staged migration.
- The README records the legacy mining, phase expectations, open questions, and
  defects found while writing the suite.
- Node-transition coverage is split by eligibility, invalidation, unwired,
  failure, and cache-hit behavior.
- Barrier coverage separates context-wide, node-local, async, and no-pump
  semantics. This is much better than a single `compute()` smoke test.
- Latency coverage follows the design's flagship script and adds the sharper
  file-backed execution count showing that adding downstream work re-executes
  upstream bodies.
- Correctness coverage is broad: bash file and directory results, pin edits,
  context-vs-standalone checks, compiled representation before execution,
  modules, environment positive/negative controls, expressions, fan-in, cycles,
  and no-code false completion.

The main issues to fix before calling it consensus:

- Add the [MOD-16] expression lifetime test from Codex. Claude currently lists
  it as outstanding rather than implementing it.
- Add a `make_context` or context-manager fixture inspired by Codex so Context
  cleanup is centralized. Keep Claude's cache reset approach; do not replace live
  cache singletons.
- Fix
  `correctness/test_correctness_expressions.py::test_a_missing_key_is_distinguishable_from_a_key_holding_null`.
  The current tuple comparison can raise `TypeError` by comparing a `Checksum`
  object with `None`. Use normalized checksum strings, preferably
  `result_checksum(ctx, path)`, in the tuple.
- Strengthen the compiled prerequisite skip. `gcc` is not sufficient; the test
  also needs `seamless_signature` importable. Without the local sibling repo on
  `PYTHONPATH`, the `now` marker fails for an environmental reason.
- Document or encode the sibling-repo `PYTHONPATH` in the runner. In this checkout
  collection only worked after adding local `seamless`, `seamless-core`,
  `seamless-transformer`, `seamless-config`, `seamless-database`,
  `seamless-signature`, and `seamless-workflow`.
- Revisit ignored `settle(...)` calls in tests marked `a1`. Some invalidation
  tests intentionally pass vacuously during A1 limbo, but an ignored 30-second
  wait is a poor failure mode. Split non-vacuous stale-checksum tests to `a4`,
  and make A1 tests assert only the part that is observable in limbo.
- Decide whether node-local barriers should raise or return on `unwired` and
  `blocked`. Claude pins today's raising behavior but correctly calls it an
  open question. The consensus suite should either keep it as "current baseline"
  or move it out of the architectural contract until the decision is made.
- Consider moving some long explanatory prose from module docstrings into the
  README once the suite stabilizes. The prose is valuable now, but the tests
  should remain easy to scan during implementation.

## Category-by-category consensus

### Node transition

Use Claude's five-file split:

- `test_transition_eligibility.py`
- `test_transition_invalidation.py`
- `test_transition_unwired.py`
- `test_transition_failure.py`
- `test_transition_cache_hit.py`

Codex's node-transition file is a useful smoke version, but it combines too many
contracts and lacks phase markers. Do not keep it as-is. If a compact smoke test
is desired later, add it after the granular Claude tests.

Make sure invalidation tests distinguish:

- A1-observable revocation/pending behavior.
- A4-only stale-checksum/value behavior after real execution returns.

### Quiescence barrier

Use Claude's split:

- context-wide sync barrier
- node-local sync barrier
- async barriers
- no-pump legacy timeout decision

Drop Codex's `compute(timeout=...)` and `computation(timeout=...)` expectations.
They encode the wrong consensus for graph quiescence. Caller-side timeout tests
can use `asyncio.wait_for` around `context_computation(ctx)` without adding a
timeout parameter to the API.

Keep Claude's helper that looks up barrier methods on the class. It gives a much
better failure than `TypeError: 'MissingView' object is not callable`.

### Latency

Use Claude's latency tests. They are closer to section 15 and separate:

- setting the last pin returns promptly;
- the node remains pending while the body runs;
- reads during computation do not block;
- the result arrives in about one body duration;
- connecting downstream returns promptly;
- connecting downstream or adding unrelated nodes does not re-execute upstream
  bodies;
- the direct legacy `delay.py` shape is ported without importing the pump.

Retain the file-backed execution log. It will keep working after A4 moves
execution to workers, while a module-level counter would stop measuring the
thing the test claims to measure.

### Correctness

Use Claude's correctness suite as the base, with one addition from Codex:

- Add `test_correctness_expression_lifetime.py` from Codex, adapted to Claude's
  helper style and marker scheme.

Keep Claude's separate files for bash, compiled, modules, environment,
expressions, fan-in, and no-code. They are more diagnostic than Codex's combined
`test_non_python_transformers.py`.

Prefer Claude's environment tests over Codex's conda-env test. The negative
`which` case proves that an unavailable declared requirement cannot silently
compute anyway; Codex's `CONDA_DEFAULT_ENV` case is too machine-dependent.

Keep Claude's fan-in/cycle tests. They are important because they found a real
topology defect that Codex's expression tests miss.

## Proposed consensus tree

The consensus suite should look like this:

```text
tests/consensus/
  README.md
  conftest.py
  contract_helpers.py
  run-tests.sh
  node-transition/
    test_transition_eligibility.py
    test_transition_invalidation.py
    test_transition_unwired.py
    test_transition_failure.py
    test_transition_cache_hit.py
  quiescence-barrier/
    test_barrier_context.py
    test_barrier_node.py
    test_barrier_async.py
    test_barrier_no_pump.py
  latency/
    test_latency_last_pin.py
    test_latency_downstream.py
    test_latency_delay_port.py
  correctness/
    test_correctness_bash.py
    test_correctness_compiled.py
    test_correctness_environment.py
    test_correctness_modules.py
    test_correctness_expressions.py
    test_correctness_fanin.py
    test_correctness_no_code.py
    test_correctness_expression_lifetime.py
```

Whether the directory is named `consensus`, `gpt-5.5`, or promoted into the main
suite is a project choice. The important point is that the consensus is not an
average of the two generated suites. It is Claude's contract suite with Codex's
missing [MOD-16] test and a small amount of fixture hygiene.

## Remaining A0 gaps

Neither generated directory implements the materialisation recording mode or the
exact expected materialisation logs from [MOD-14]. That may be outside the
specific "write these four test directories" request, but it is still the green
half of A0 exit evidence.

After adopting the consensus suite, A0 still needs:

- materialisation recording and expected-log tests;
- the one-branch [MOD-15] fix for no executable code;
- the [MOD-16] `Expression.input_ref` fix;
- settled decisions for [MOD-2], [MOD-4], and [MOD-7];
- no generated bytecode checked in.
