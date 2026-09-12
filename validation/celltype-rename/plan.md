# Plan: celltype rename + Transformer pin redesign

## Context

### The rename
Today `celltype` on a Cell/Expression is the *input* interpretation and `target_celltype` the *output*. The workflow Context already treats a cell's `celltype` as the type of its value, so core and Context disagree. Verified by probes:
- a standalone retype doesn't convert;
- standalone chaining reinterprets bytes (`'"5"'`) where the Context converts (`'5'`);
- an authoritative Context cell ignores `target_celltype`;
- a connected Context cell stores its checksum in `target_celltype`, but `.value` and downstream nodes read it as `celltype`;
- setting a standalone Cell's input side reinterprets: `Cell("str").set("hello")`, then `.celltype = "text"`, gives `'"hello"'`.

`test_celltype_changes.py` is currently 0/20. Even `same-type` fails: the downstream cell goes `blocked` with no exception after the retype.

### The pins
Transformer pins reinterpret; they never convert to `Transformer.celltypes[pin]`. Verified by probes (bound unless noted):
- A str Cell `"hello"` connected to a text pin gives the function `'"hello"'`. str→int looks correct only because the int deserializer accepts a string. The transformation then holds the str buffer's checksum under an int pin, so the same value gets different identities.
- A constant retyped from str to text: `ctx.tf.run()` reinterprets (`reactive.py:36-51` passes the raw producer checksum), while `ctx.tf().run()` converts (`runtime_api.py:84-87`). Connected pins don't convert in either path (`_build_source_expression`, `context.py:1301-1308`).
- An unconvertible retype fails inside the transformation, so the error is blamed on execution, not on the pin.
- In seamless-transformer too: `_prepare_pin_value` (`pretransformation.py:160-207`) passes a Transformation's or Expression's result checksum through unchanged. `_to_checksum` only checks that it deserializes.
- Pins hold values, not checksums: the standalone `ArgsWrapper` stores the Python object, and a bound pin read resolves in the celltype it was stored in, which is stale after a retype.

### Why one plan
- A Pin's `celltype` has to mean the output side, which only holds after the rename.
- Pins and Cells share the read-only input-celltype rule and the conversion machinery. Pins need `_projection` to report its error, which is also the Cell bug.

Cells, Expressions and Context pins are unreleased (not in RELEASE-NOTES 1.3/1.4), so DB columns and wire keys are renamed in lockstep now, with no migration code.

## Decided semantics

### Cells and Expressions
- **`celltype`** is the type of the produced value (checksum, `.value`, `run()`).
  - At creation it's either explicit or copied once from a typed input's `celltype`, else it's `"mixed"`. `ctx.a = ctx.b` creating a new `a` copies `b.celltype` once.
  - On an existing cell, only an explicit `celltype` assignment changes it (a conversion). Rewiring and assigning values never change it.
  - Assigning a builder over an existing cell still replaces its config.
- **`input_celltype`** says how the input is read. It is **read-only, and comes from the input:**
  1. A typed input (Cell, Expression, Transformation, upstream node) gives its `celltype`, followed live on a Cell. An Expression resolves it once, at construction.
  2. A value given with `set()` gives the celltype it was serialized in, which is the cell's `celltype` at that moment.
  3. A bare Checksum gives the celltype declared along with it: `Cell(celltype, checksum=cs, input_celltype=...)`, `set_checksum(cs, input_celltype=...)`, or `Expression(cs, ..., input_celltype=...)`. The default is `celltype` at that moment. To correct it, assign the checksum again.
  4. With no input, it's `None`.

  Setting it raises, standalone and bound. Passing `input_celltype=` together with a typed input raises unless it matches. Legitimate reinterpretation is a `celltype` conversion: `conversion.py` has "trivial" and "reinterpret" pairs (text→python, bytes→text, mixed→plain) that keep the checksum and validate the bytes. After `ctx.a = ctx.b`, `ctx.a.input_celltype == ctx.b.celltype`.
- **One celltype in config.** `CellConfig` keeps only `celltype`. The input celltype is stored with the input: `cell_root_producer.celltype`, or the source's celltype. The graph already writes a constant's celltype as `value.celltype` (`context.py:1389`). Expression, DB and wire keep both fields, because an Expression's input is a bare checksum.
- **Retired:**
  - `target_celltype`, blocked with a clear error (see Safety measures);
  - `Edge.source_celltype`/`target_celltype`, which are never set;
  - the follow rule in `configuration.py`.
- **Not renamed:**
  - generic conversion helpers with an explicit pair: `validate_expression(source_celltype=, target_celltype=)` (`hash_type_validation.py`) and `buffer_info.py`;
  - `Transformer.celltypes`, which already holds each pin's output type and matches `Pin.celltype` after the rename.
- **The configured input is public as `.source` + `.checksum`; `input_ref` goes private.** followup-design:346-349 already says a bound handle must report its *configured* input, and the implementation contradicts it: `BoundCellBackend.input_ref` returns `_get_checksum(node_path, local_path)`, the current and already converted value. The fix splits the union rather than renaming it (see *`input_ref` is split, not renamed* below):
  - a root fed by an edge reports that source through `.source`;
  - a root with a constant reports `None` from `.source`, and the value from `.checksum`;
  - a projection reports what feeds *that path*: the one-level edge targeting it, else the nearest enclosing source, else `None`.

  Internally `_input_ref` stays the Expression recipe, so `(_input_ref, input_celltype, celltype)` is a real recipe in both modes, which is what a Cell is.

### Pins
- **`tf.pins.x` returns a `Pin`, in both modes.** It's a fresh handle each time (handle identity carries no meaning). An unset pin reads as an unwired Pin, not `None`. An undeclared name raises AttributeError, as now.
- **`Pin` is a sister class of `Cell`,** sharing a base `CellBase` in seamless-core; `Pin` lives in seamless-transformer. This was my recommendation; a subclass was the alternative. The reason: with a subclass, `isinstance(pin, Cell)` is true, so every existing source check accepts pins unless it's patched. The subclass would also have to disable projection, the `_derive` family, validators and `mount`.
  - Base: `celltype`, `input_celltype`, `checksum`, `source`, `buffer`, `value`, `state`, `exception`, `build()`/`compute()`/`run()`, repr, refhold plumbing.
  - `Cell` adds: navigation/projection, validators, `mount`, the `_derive` family, source hooks (`_workflow_endpoint`, `_workflow_capture_source`), `prune`/`clear_exception`, augmented updates.
  - `Pin` adds: `set()`/`set_checksum()`, routed to its Transformer.
- **Pins hold checksums, never values.** `tf.pins.x = v` is the same as `tf.pins.x.set(v)`:
  - a value is serialized now with `celltypes[x]`;
  - an invalid value raises, as `Cell.set` does;
  - a bound source connects;
  - `.checksum = None` clears; `del tf.pins.x` deletes the declaration, and only for signature-less code.
- **`pin.celltype` is `tf.celltypes[x]`.** The Transformer owns it, the Pin stores none, and it can be set from either side. The setter validates and normalizes like `tf.celltypes.x` (`update_config` already maps `int` to `"int"`).
- **`pin.input_celltype`** follows the Cell rule above. A bare Checksum is read in the pin's celltype at assignment unless declared otherwise.
- **Conversion:** the transformation receives the input converted to `celltypes[x]`, the same conversion as for Cells, skipped when the types match. A retype converts again from the stored input. This applies to prebound pins and to call-time arguments `tf(x=...)` alike.
- **Failure:** a failed conversion gives `pin.state == "failed"` and `pin.exception`. The transformer becomes `blocked` / `blocked-by-error`, with `x` in `tf.block_reason`, and no transformation is built.
- **A Pin can't be a source.** It isn't accepted as a Cell/Expression input, an edge source, a Context assignment, or a call argument. The error points to `pin.source`. Bound pin endpoints get `can_source=False`.
- **Whole pins only.** No sub-pin targets or projection (§Pins), no validators, no mount.
- **Optional pins** are unchanged: JSON null means absence, and the set stays on `Transformer.optional_pins`.

- **A pin reports its input the same way a cell does**: `pin.source` for a connection, `pin.checksum` for a constant. A pin has exactly one input, so the ambiguous case a Cell can hit (a root literal plus one-level edges) doesn't arise.

### `None` (already decided; the implementation is split)
The normative rule is context-internals-followup-design.md:236-271: `None` is an ordinary literal value, at the root and at a path, and assignment of `None` is never deletion sugar. `del` deletes. This reverses context-workflow-internals-implementation-handoff-plan.md:594-617, which made `ctx.a = None` and `ctx.tf.x = None` deletion sugar. The one exception stays: on a *connected optional* pin, JSON null means that pin's absence (pass3:470-505), and the "no result" value is the canonical checksum of null, never `result_checksum is None`.

Three behaviours have to stay apart, and each needs its own spelling:

1. **Delete the node.** `del ctx.a`; `del tf.pins.x` removes a pin declaration, in both modes, and only for signature-less code.
2. **Clear the value, keep the node** — checksum `None`, state `unwired`. Spelled `.checksum = None`, for cells and pins alike (see *Clearing* below).
3. **Store the null value** — a real checksum (`38e0b9de…` for `plain`/`mixed`). Spelled `= None` / `.set(None)`.

**Behaviour 3 is required, and it stays.** optional-pins-implementation-plan.md:30 says "a required pin whose value is JSON `null` stays present; required-null and optional-null have different identities", and :52 refuses a second absence sentinel. pass3:474-483 makes the null checksum the "no result" value, "not `result_checksum is None`, which already means 'not computed'". Removing it would merge "no value" with "the value None", which is the conflation those docs exist to prevent.

Today it's half-implemented. A function that returns `None` fails with `RuntimeError("Result is empty")` (run.py:259), and a connected optional pin fed by it goes `blocked` rather than being dropped, so the only way to reach behaviour 3 is `tf.pins.x = None` on a bound pin. Cell roots clear on both `ctx.a = None` and `.set(None)`, and standalone `Cell.set(None)` clears too, because `None` is a valid `input_ref`.

**Where `None` is allowed: strict at the function boundary, union in storage.** A cell is a slot; a pin and a result are the function's declared interface.

| Position | Rule |
|---|---|
| **Any cell**, whatever its celltype | `None` allowed. Every cell is in effect `T \| None`. |
| **Required pin**, `plain`/`mixed`/`bytes` | `None` allowed, as an ordinary value — for `bytes` it is the empty byte string |
| **Required pin**, any other celltype | `None` rejected — a literal raises at assignment, and one arriving from upstream is a type error reported on the pin |
| **Optional pin**, any celltype | `None` means absence: compared, then dropped before conversion, never decoded |
| **Result** | `None` allowed only when the result celltype is `plain`/`mixed`; it is the function's declared output, so it follows the pin rule, not the cell rule |

Rule 2 is also what protects compiled transformers, whose pins are never `plain`/`mixed` and which have no representation for `None` at all.

**One representation, celltype-independent:** the canonical JSON-null checksum (`38e0b9de…`, `b"null\n"`). A null cell's checksum is therefore the same whatever its celltype, so retyping it is a no-op. In seamless-core that means:
- `Buffer(None, ct)` returns the null buffer for every `ct`, instead of `str(None)` for `text`/`str`/`bytes` and raising for `int`/`binary`;
- resolving the null checksum with any celltype gives `None` — except `bytes`, where it gives `b""` (see *`bytes` has no empty buffer* below);
- `validate_deserializable_as(null, ct)` is true for every `ct` (hash_type_validation.py, plus a line in `type_bits_design.md`);
- conversion maps null → null for every pair, leaving the checksum untouched;
- `b"null\n"` joins `TRIVIAL_CHECKSUMS` (calculate_checksum.py:42), so a null value always resolves without depending on the buffer cache.

Today's standalone pin path is the same idea done wrong: it serializes `None` as plain but then passes the checksum off as the pin's celltype (`buffer_celltype = "plain" if value is None else celltype`, pretransformation.py:251), so an `int` pin fails late with a HashType error instead of being rejected by rule 2.

**Absence is celltype-independent.** optional-pins-implementation-plan.md:127-149 restricts absence to `plain`/`mixed` pins; that restriction is lifted, which also removes pass3's admitted restriction 2 (optional pins collapsing into required ones for `binary` and deep celltypes).

**No checksum is a blocker, and it propagates.** The counterpart of the union rule: a null *value* flows, while the *absence* of a checksum gates every dependent, and `optional` never relaxes that. An unconnected optional pin is simply not part of the transformation, but a connected one whose source has no checksum — unwired, blocked, or still computing — blocks the transformer exactly like a required pin (pass3:451-453, "optional means may be absent **or** `null`, but if connected, always computed"). The implementation already does this: `_derive_transformer` skips a pin with no producer only when it is optional (reactive.py:47-50), and treats any non-complete source as pending (reactive.py:38-43). What changes is that this is now the *only* meaning of a missing checksum, because `None` no longer has to borrow it.

### Mounts and null
1. **Pins are never mounted.** `Pin` has no `mount`, and the base class doesn't carry one.
2. **A mounted cell never has "no checksum".** Clearing a mounted cell (`.checksum = None`) is refused; unmount first. This is about the *deliberate* state: a mounted cell fed by a still-computing upstream is transiently without a checksum, and there the mount simply doesn't write, leaving the file at its last content.
3. **A missing file and an empty file both read as the canonical null**, whatever the celltype. This is what makes null a value of every celltype worth having: an `int` cell over a missing file is null, not an error.
4. **Null is written as an empty file.** The null buffer is never written, and files are never deleted.
5. **A null that came from the file doesn't rewrite it.** All three forms — missing, empty, containing `null\n` — yield the same checksum, so a mount that writes only when the cell's checksum differs from the one the file last yielded satisfies this with no extra mechanism. It also means a file holding `null\n` keeps it.

**`bytes` has no empty buffer; empty *is* null.** `bytes` is the only celltype whose canonical serialization of a legitimate value is empty — `Buffer(b"", "bytes")` is `b""`, checksum `e3b0c442…`, which is also the empty file. Rather than special-case mounts, the rule is blanket and applies wherever a `bytes` value is produced: serialization, expression evaluation with output celltype `bytes`, transformation results, and mount reads. **An empty buffer under celltype `bytes` canonicalizes to the null checksum.**

It has to run **both ways** to stay lossless: resolving the null checksum as `bytes` gives `b""`. So `bytes` is the one celltype where null is not `None`, and:
- `ctx.a = b""` on a `bytes` cell stores null and reads back `b""`;
- a required `bytes` pin accepts null, because for that celltype null *is* a value — so the "null rejected on required pins" rule reads `plain`/`mixed`/`bytes`;
- mounts need no special case at all: empty file, missing file, `b""` and null are one state, with no canonicalization at write time and nothing to refuse.

One-directional aliasing would be worse than the mount-only rule it replaces: `b""` would silently become "no value", and every downstream required `bytes` pin would then reject it, breaking pipelines that legitimately pass empty data.

The cost is the same one optional pins already carry for `plain`/`mixed`: a *connected optional* `bytes` pin fed `b""` is dropped as absent, so it cannot receive an empty byte string.

No other celltype is affected, because none of them serializes to zero bytes: `text ""` is `b"\n"`, `str ""` is `b'""\n'`, `plain {}` is `b"{}\n"`. What does change for them is *reading* a hand-made empty file: it currently reads as `""` for `text` and raises `HashTypeValidationError` for `plain`, `int` and `mixed`, and under rule 3 all of them read as null — an improvement for the three that used to error.

**Directory mounts.** Rules 3-5 are file-shaped, so for `folder`/`deepfolder` the split is by existence rather than emptiness: a *missing* directory reads as null, an *empty* directory as the empty folder (`{}` → `b"{}\n"`, which is not an empty file, so there is no collision to resolve).

(The `text`/`str`/`bytes` question is settled by the one-representation rule: `Buffer(None, "text")` currently gives `b"None\n"`, and converting null from `plain` to `text` gives the same; both become the null buffer instead.)

### Two verb families: declare the input, or write what you own
Every write either *defines* a node's input or *writes under* an existing one, and they differ in whether authority is checked. Today the split is accidental: `ctx.a = 7` over a connected cell silently drops the edge, while `ctx.tf.pins.x = 7` over a connected pin raises `AuthorityError` — same spelling, opposite answers.

| Family | Spellings | On a connected target |
|---|---|---|
| **Declare the input** | `ctx.a = X`, `ctx.tf.pins.x = X` (value or source), `X.checksum = cs` (including `= None`) | **make it so**: replaces whatever input exists, edge or producer |
| **Write what you own** | `.set()`, `.set_checksum()`, sub-path assignment, `+=` | **check**: `AuthorityError`, since an upstream edge would overwrite the write on the next recompute |

Pins join cells in the first row, so `tf.pins.x = 7` over a connected pin detaches instead of raising. Sub-path writes keep checking, which is already the rule and is required by followup-design:277-293.

### The 3×2 matrix at the root
Three forms — value, buffer, checksum — each in both families, on cells and pins alike. All three are readable (the output side) and all three are writable:

| Form | Declare the input (detaches) | Write what you own (checks) |
|---|---|---|
| value | `ctx.a.value = v`; `ctx.a = v` and `ctx.tf.pins.x = v` are the idiomatic sugar | `ctx.a.set(v)` |
| buffer | `ctx.a.buffer = buf` | `ctx.a.set_buffer(buf)` |
| checksum | `ctx.a.checksum = cs` | `ctx.a.set_checksum(cs, input_celltype=…)` |

Everything reduces to setting a checksum; the rows differ only in what they do first, and in what they can guarantee:

- **value** — serialize with the node's `celltype`, which *validates* (`Buffer("abc", "int")` raises: this is where an invalid literal is rejected); deposit the buffer; record `input_celltype = celltype`.
- **buffer** — bytes already in hand, so no serialization, but the bytes *can* still be checked against the celltype, since `validate_deserializable_as(cs, celltype, buffer=buf)` takes the buffer; deposit it; a Buffer carries no celltype of its own (`__slots__` is `_checksum`, `_content`), so `input_celltype = celltype`.
- **checksum** — an id only: nothing to deposit, so the buffer must already be resolvable or the node fails later with `CacheMissError`; validation is limited to the hash-type bits; `input_celltype` defaults to `celltype` and is declarable.

`None` in this matrix follows the value/reference split: `.value = None` and `.set(None)` store the **null value**, while `.checksum = None` (canonical), `.buffer = None` and `.set_checksum(None)` mean **no input** and clear the node.

Standalone builders accept all six writes. Of the reads, `.checksum` gives the input checksum when the input is a bare checksum and `None` otherwise (`.source` covers that case), while `.value` and `.buffer` need a computation and so stay bound-only — use `.run()` or `build()`.

Sub-path writes are one layer further out: `ctx.a["b"] = 5` resolves the current root value, mutates a detached copy, re-serializes and sets the *root* checksum, so it also needs the current value to be materializable (`ValueUnavailableError` otherwise).

### Clearing: `checksum = None`; `del` is reserved for deletion
`del` means the named thing is gone, and never "empty it":

| Spelling | Meaning |
|---|---|
| `ctx.a.checksum = None` | clear a cell: remove whichever input exists, node stays, state `unwired` |
| `ctx.tf.pins.x.checksum = None` | clear a pin: producer or edge gone, declaration stays, transformer blocked |
| `ctx.a.checksum = cs` | declare a literal input (detaches a connected cell) |
| `del ctx.a` | delete the cell node |
| `del ctx.tf.pins.x` | delete the pin *declaration* — legal only for signature-less code, raises when the code has a signature |
| `del ctx.a["b"]` | delete a key from the value (or the edge targeting that exact path) |

- `.checksum` as a write is the declare-the-input family; `.set_checksum(cs)` stays its checking sibling, exactly as `ctx.a = 5` is to `.set(5)`.
- It doesn't collide with `ctx.a = None`, which stores the null *value*: `.checksum` carries a checksum or nothing.
- A sensing mount still refuses with "unmount first", since the mount is the producer.
- `.checksum` is bound-only today; as the write spelling it becomes available standalone too, where read and write agree. On a *connected* bound cell the read reports the derived output while the write declares a new input — the same asymmetry `.value` and `ctx.a = 5` already have, to be documented rather than discovered.

**`input_ref` is split, not renamed.** It was awkward because it held two different things: the value a node holds, or the thing it derives from. Each arm gets its own public name, and `_input_ref` stays internal as the Expression recipe:

| Public | Meaning |
|---|---|
| `.checksum` | the value arm — read the current checksum, write to declare a literal, `None` to clear |
| `.source` | the derivation arm — **read-only**: the upstream handle (bound), or the Cell/Expression/Transformation a standalone builder is built on; `None` when the node holds its own value |

The mutual exclusivity belongs to the *inputs*, not to this public pair: a node either derives from a source or holds a literal, which is what the union expressed. `.checksum` reports the node's **current value**, so it is populated for a connected node too — and is `None` whenever the node is not `complete`:

| State | `.source` | `.checksum` |
|---|---|---|
| unwired | `None` | `None` |
| literal, complete | `None` | the literal, converted if its celltype differs from the node's |
| connected, complete, no conversion | handle | `== source.checksum` |
| connected, complete, converting | handle | the converted checksum — equal to the source's only when the conversion preserves bytes (the `trivial`/`reinterpret` classes, e.g. `plain`→`mixed`), different when it reformats (`str`→`text`, `int`→`float`) |
| connected, upstream waiting/blocked/failed | handle | `None` |
| own conversion or validator failed | handle | `None` |

So `.source is None` means "nothing upstream feeds this", and `.checksum is None` means "not complete" — the blocker rule, read off the handle. `source` rather than `input` because it names the relationship, matches the vocabulary already in the code (`_public_source`, `can_source`, "bound source handle"), and reads correctly in both modes — `ctx.b.source is ctx.a` — whereas "input" collides with a transformer's inputs.

**On a projection, `.source` answers for that path.** `ctx.a.b.source` is the one-level edge targeting `b` if there is one, otherwise it falls back to `ctx.a.source`; deeper paths, which can never carry an edge, walk up the same way. So a cell with a literal root and a connection at `b` reports `ctx.a.source is None` and `ctx.a.b.source` as the upstream — the only way sub-path edges were otherwise visible was `get_graph()`. This changes followup-design:348, which made a projection's `input_ref` its *owning root endpoint*; that meaning belonged to the Expression recipe, which is now the internal `_input_ref`.

Connecting stays assignment (`ctx.a = ctx.b`, `ctx.tf.pins.x = ctx.b`), so `.source` needs no setter and there is exactly one clear spelling. For standalone builders the constructor keyword splits the same way — `Cell("int", checksum=cs)` and `Cell("int", source=other)` replace `input_ref=` — while `with_input(x)` stays as the polymorphic builder method. The README and 58 test call sites use `input_ref=`; the change is mechanical.

**You may delete what you declared, never what the code declares.** With a Python signature the pin set is fixed, so `del ctx.tf.pins.x` raises; for signature-less code (text, bash) the user declared the pin and may delete it, which removes it from `pins`, `celltypes` and `optional_pins`. Clearing a pin's *input* is `pin.checksum = None` in both cases.

This **reverses followup-design A.5**, which recorded bound `del ctx.tf.pins.x` as clearing the *producer* while standalone removed the *declaration*, and called the bound meaning the specified one. The standalone meaning wins, and both modes now behave the same way, including the refusal on a fixed signature. A.5 goes on the Phase 8 rewrite list.

## Safety measures
1. **Blocking the retired name.** Attribute access falls through to structural projection and would swallow a removed name:
   - `Expression.__getattr__` (expression_class.py:315) would turn `expr.target_celltype` into a sub-path Expression;
   - `Cell.__getattr__` would return a SubCell;
   - `ctx.a.target_celltype = X` on a bound cell would write into the value (cell_class.py:420-426).

   Fix: a retired-names set checked in `Cell.__getattr__`/`__setattr__`/`__delattr__` and `Expression.__getattr__`, before projection, raising an AttributeError that names the replacement. Keep it permanently for `target_celltype` **and for `input_ref`**, which becomes private in Phase 4a and would otherwise project into a silent `SubCell`. A value key stays reachable as `cell["input_ref"]`, the same convention as `state`. `Pin` has no projection, so it doesn't need this.
2. **Two-step rename** for the standalone Cell/Expression surface, with the suites green between steps: first `celltype` → `input_celltype` (with `celltype` blocked), then `target_celltype` → `celltype`. The bound surface changes only in Phase 4.
3. **Keyword-only Expression fields.** The celltype and validator fields of `Expression` become keyword-only (`KW_ONLY`); the 18 positional call sites, all in tests, get rewritten. Internal functions that take the four values keep their order (input side, output side): `ExpressionKey`, `_cache_key`, `identity_key`, `database_key`, `evaluate_expression*`, `cancel_expression`, and the `database_remote`/`jobserver_remote` functions.
4. **Pins can't be sources because of their type, not because of per-site checks.** `Pin` doesn't subclass `Cell` and defines no `_workflow_endpoint`, `_workflow_capture_source` or `_compute_dependency`, so the existing checks already refuse it. The explicit Pin checks at entry points only improve the error message.
5. **Pin tests assert on checksums, not just values.** Value checks with int/str/float hide reinterpretation, because those deserializers coerce.

## Phases
Branching: seamless-workflow is on `main`, so work goes on a new `celltype-rename` branch there. The other repos continue on `cells-and-expressions`. One commit per phase per repo, with your OK.

### Phase 0: Baseline
- **Commit your uncommitted work first**, so the rename diffs start clean:
  - seamless-core: `cell_class.py` plus 5 tests;
  - seamless-workflow: README, `builder_state.py`, `configuration.py`, `context.py`, 3 tests, and the untracked `test_celltype_changes.py`.
- Record per-file pass/fail for every affected repo, seamless-transformer included.

### Phase 1: Guardrails (seamless-core, behaviour-preserving)
- Add the retired-names check, reusing the `_class_attribute` pattern in `cell_class.py`.
- Add `KW_ONLY` to `Expression`; rewrite the positional test call sites to keywords (old names).
- New tests: a retired name raises on a standalone Cell, a SubCell, a bound Cell (read and write), and an Expression; `cell["x"]` still projects.

### Phase 2: Input side, `celltype` → `input_celltype` (behaviour-preserving)
- **Core:**
  - the Expression field (its `celltype` is temporarily retired), `ExpressionKey`, and the parameter names of the evaluator/cancel/remote functions;
  - the Cell slot, property and keyword argument. It's still settable in this phase; Phase 4 makes it read-only. A standalone `Cell.celltype` raises; a bound one still delegates to the backend.
- **Every site that reads a builder's or Expression's input side:**
  - seamless-workflow `ingress.py` (PreparedCell);
  - `context.py`: `_create_cell_from_builder`, `_replace_cell_from_builder` and producer retention, plus the Expression constructions in `_build_cell_expression`, `_build_source_expression` and `_capture_endpoint`;
  - `builder_state.derive`, `runtime_api.py` (the pin Expression), `sidework.evaluate_projection`;
  - the seamless-dask payload builder (Python side only).
- **Unchanged:** wire keys, DB columns, `CellConfig`.
- **Gate:** suites match the baseline, which proves no stale `celltype` use remains on these objects.

### Phase 3: Output side, `target_celltype` → `celltype` (behaviour-preserving)
- **Core:**
  - Expression and standalone Cell rename `target_celltype` → `celltype`.
  - Defaults are symmetric: if one side is given, the other copies it; if neither, `"mixed"`. An old call that passed only `target_celltype` gets `input_celltype="mixed"`.
  - `as_celltype(x)` sets `celltype`; the repr is updated.
  - `target_celltype` is blocked on standalone cells (bound cells still delegate until Phase 4); the temporary block on `Expression.celltype` is removed.
- **Wire and DB, in one lockstep change.** The JSON keys `{celltype, target_celltype}` become `{input_celltype, celltype}` in:
  - seamless-remote `database_client.py` and `jobserver_client.py`;
  - `seamless-dask/seamless_dask/client.py`;
  - `seamless-jobserver/jobserver.py`;
  - `seamless-database/database.py`;
  - `seamless-core/seamless/checksum_class.py` (fingertip reverse lookup).

  The DB `Expression` model's columns and composite primary key change too (`database_models.py:111-125`). A mismatched client and server fails loudly (KeyError, or "no such column").
- **Dev DBs:** run `ALTER TABLE expression RENAME COLUMN celltype TO input_celltype;` then `ALTER TABLE expression RENAME COLUMN target_celltype TO celltype;` (in this order), or drop the table, since it's a cache. Documented in the commit message.
- **Workflow:**
  - Builder ingestion maps `CellConfig(celltype=builder.input_celltype, target_celltype=builder.celltype)`, which is the old behaviour and an internal detail until Phase 4.
  - Delete `Edge.source_celltype`/`target_celltype` (`graph.py`, and the copy in `context.py` subcontext copying).
- **Gate:** suites match the baseline.

### Phase 4: Cell semantics (behaviour changes, each with a test)
- **4a, core** (`cell_class.py`, `expression_class.py`):
  - `input_celltype` becomes read-only on Cell and comes from the input as described above. It's live for typed inputs, recorded by `set()` (serialized with `celltype`), and declared with a bare checksum through `checksum=` or `set_checksum(cs, input_celltype=)`. It's `None` with no input.
  - An Expression resolves it at construction; an explicit value that disagrees with a typed input raises.
  - `celltype` is copied once at construction.
  - **`input_ref` goes private, split into `.checksum` and `.source`** (see the `None` section). `.checksum` gains a setter in both modes and becomes readable standalone; `.source` is a new read-only property; `_input_ref` stays internal as the Expression recipe. The constructor takes `checksum=` or `source=` instead of `input_ref=`, and `with_input()` stays polymorphic. No deleter anywhere: `del` stays deletion. `set(None)` stores null rather than clearing. Tests: the two arms are mutually exclusive; a connected cell reports `.source` and a literal one reports `.checksum`; a projection reports the edge at its own path, else the nearest enclosing source.
  - **The 3×2 matrix** (see that section): `.value` and `.buffer` gain setters alongside `.checksum`, and `set_buffer()` joins `set()` and `set_checksum()`. The buffer forms deposit the buffer and validate its bytes against the celltype; the checksum forms do neither. Tests: each of the six writes lands the same checksum; the three `set_*` raise `AuthorityError` on a connected node while the three attribute writes detach; `.value = None` stores null where `.buffer = None` and `.checksum = None` clear.
  - **`None` gets one celltype-independent representation** (see the `None` section): `Buffer(None, ct)` returns the null buffer for every `ct`; resolving null with any celltype gives `None`; `validate_deserializable_as` accepts null everywhere (hash_type_validation.py, with a line in `type_bits_design.md`); conversion maps null → null for every pair; `b"null\n"` joins `TRIVIAL_CHECKSUMS` (calculate_checksum.py:42). Tests: an `int` cell holding null round-trips, retyping it doesn't change the checksum, and a null value resolves with an empty buffer cache.
- **4b, workflow:**
  - `CellConfig(celltype, validator, validator_language)`: `target_celltype` goes, and there's no input field.
  - New `_effective_input_celltype(path)` in `context.py`: the root edge's `_node_celltype(source)`, else `cell_root_producer.celltype`, else `None`.
  - `_derive_cell`: the root edge and the producer both project from the effective input celltype to `cfg.celltype` through `_projection`. This fixes the connected-cell bug. Sub-path merges are unchanged.
  - **`_projection` returns its error** (`context.py:1126-1133` drops it today). A cell whose own conversion fails becomes `failed` with `.exception`, not `blocked` with nothing. Phase 7 needs this for pins.
  - `_assign`: a new cell created from a bound source (including the path that replaces a transformer node) copies `self._node_celltype(source_node)` once.
  - Ingestion: `CellConfig(celltype=cell.celltype)`. A bare checksum goes to `_retain_producer(checksum, declared input_celltype or cfg.celltype)`; `set()` and assigning a value serialize with `cfg.celltype`.
  - `builder_state.py`:
    - the `celltype` getter/setter, without the follow rule;
    - an `input_celltype` getter returning `_effective_input_celltype`, with no setter (setting raises);
    - `set_checksum(cs, input_celltype=None)`;
    - remove `target_celltype`;
    - then block `target_celltype` on bound cells too.
  - **`.source` reports what feeds the handle** (the design doc's `input_ref` rule, lines 346-349, split per the `None` section and with the projection rule changed): the reconstructed bound source for a root edge (`_public_source`, which `_get_pin` already uses), `None` for a literal root, and for a projection the one-level edge targeting that path, else the nearest enclosing source. `.checksum` keeps reporting the current value.
  - **`_build_cell_expression` builds from that input**, with `input_celltype` and `celltype`, so `cell.build().run()` equals `cell.value`. It currently starts from the already converted current checksum, which converts twice when the two sides differ. Where there's no single input, it falls back to the current checksum with `input_celltype = celltype`.
  - `derive` and `with_input` follow the same recipe.
  - **Clearing.** `BoundCellBackend` gains a `checksum` setter, backed by a controller operation that removes the root edge or the root producer and re-derives; a sensing mount refuses. Tests: clearing a connected cell removes the edge and re-blocks downstream; clearing a literal cell drops the producer; `ctx.a.set(7)` on a connected cell still raises `AuthorityError`.
  - **Root `None` stores null.** `ctx.a = None` and a bound `.set(None)` store the canonical null checksum for `cfg.celltype`, matching what sub-paths and pins already do (`ingress.py:60` returns `None` unchanged today, so the root clears). Standalone `Cell.set(None)` follows.
  - `configuration.py`: drop the follow rule and the `target_celltype` field handling.
  - Mounts: `attachments/api.py:131` and `attachments/runtime.py:53` use `cfg.celltype`; the mounted-cell guard compares `celltype` only. The null rules above land here too: missing and empty files read as null, null writes as an empty file, nothing is deleted, a null read from the file is not written back, and clearing a mounted cell is refused. Tests: each of the three null file states survives a read on an `int` and a `plain` cell; a cell that becomes null truncates its file; a file holding `null\n` is left alone.
  - Graph format: write version 0.4 with no `target_celltype`. The input celltype of a constant is the existing `value.celltype`. The loader accepts 0.2/0.3 when `target_celltype` is absent or equal to `celltype`, and raises PathError otherwise.
- **Updated tests:**
  - `test_controller.py:40-44` becomes a test that setting `input_celltype` raises.
  - The downstream checks in `test_celltype_changes.py`: the downstream now keeps the old type. After the retype, `ctx.downstream.value == value` with the old type; then `next_value`.
- **New tests:**
  - The probe cases: a standalone retype converts; a chained Cell/Expression converts; `Cell("str").set_checksum(Buffer(5, "int").get_checksum(), input_celltype="int")` gives `"5"` both standalone and in a Context.
  - `input_celltype` is read-only (standalone and bound). `input_celltype=` with a typed input that disagrees raises. A text→python retype keeps the checksum.
  - A connected cell whose celltype differs from its upstream: checksum, `.value`, downstream and mount all agree.
  - `ctx.a = ctx.b` copies `b.celltype` once, and `a.input_celltype` follows `b`.
  - An existing cell's `celltype` is unchanged by rewiring.
  - A literal that fails to convert on retype: `failed`, with `.exception` set.
  - A graph round-trip with a declared bare checksum (producer celltype ≠ `celltype`).
  - `None` at a root and at a path stores the canonical null checksum for a cell of any celltype, `.value` reads back `None`, and retyping that cell leaves the checksum untouched; `del` is the only deletion. A mounted non-`plain`/`mixed` cell holding null reports a cell exception.
  - `.source` in each of its cases: a connected root gives the source handle (the same endpoint as `ctx.b`'s source), a literal root gives `None` while `.checksum` gives the value, a projection over a connection at `b` gives that edge's source, and a projection with no edge of its own falls back to the root's. `cell.build().run() == cell.value` when the two celltypes differ.

### Phase 5: Pin conversion in seamless-transformer
This depends only on Phase 3 and can run alongside Phase 4.
- In `_build_from_snapshot`, wrap each typed argument (Transformation, Expression) whose `.celltype` ≠ `celltypes[pin]` in `Expression(arg, input_celltype=arg.celltype, celltype=celltypes[pin])`.
  - An Expression already accepts a Transformation input (`_compute_dependency`), so the existing dependency paths, including dask's Expression inputs, resolve it without new code.
  - A bare Checksum is taken as already being in the pin celltype.
- **`None` at a pin follows the boundary rule, not the cell rule.** `_to_checksum` (pretransformation.py:251) stops passing the null checksum off as the pin's celltype. A required pin that isn't `plain`/`mixed` rejects it with a targeted error naming the pin and its celltype; `plain`/`mixed` pins take it as an ordinary value.
- **The optional-pin null drop happens before the pin conversion.** Otherwise an optional `int` pin fed a null fails conversion instead of being dropped. Absence compares checksums and never deserializes, so lift the `plain`/`mixed` restriction on the *pin's* celltype (optional-pins-implementation-plan.md:127-149).
- **A `None` return becomes a null result when the result celltype is `plain`, `mixed` or `bytes`** (for `bytes` it reads back as `b""`). Serialize it to the canonical null checksum instead of raising at run.py:259 (Python) and run.py:231 (compiled); keep the error for every other result celltype. Leave `transformation_class.py:710` and `:763` alone — those fire when the result *checksum* is missing, which is "not computed" and must stay an error (pass3:478-480).
- **Tests:** a `plain`-result function returning `None` completes with `38e0b9de…`; a `binary`-result one still errors; an optional `binary` pin fed null is dropped; a required `int` pin fed null is rejected with an error naming the pin, while an `int` *cell* holding null is fine.
- **Tests (standalone):**
  - a str-result Transformation into a text pin gives `'hello'`;
  - `Cell("str").build()` into a text pin gives `'hello'`;
  - an int pin fed by a str dependency holds int 42's checksum;
  - the same through dask (`seamless-dask/tests/test_expression_inputs.py` style).

### Phase 6: The Pin class, and standalone pins (seamless-core + seamless-transformer)
- **seamless-core:**
  - Extract `CellBase` from `Cell` without changing Cell's behaviour.
  - `_check_input_ref` (`cell_class.py:585-594`) rejects a `CellBase` that isn't a `Cell` with "a Pin can't be a source; connect `pin.source` instead". This works without core importing seamless-transformer.
- **seamless-transformer** (new `pin_class.py`): `Pin(CellBase)`, which in standalone mode refers to `(transformer, name)`.
  - `ArgsWrapper` stores `(input, input_celltype)` per pin, the input being a checksum or a typed reference. A value is serialized at assignment with `celltypes[x]` (tempref, then refhold), a bare Checksum is declared, and typed inputs are stored as they are. `__getitem__` returns a Pin.
  - Delete the dead `old_arg` block (`transformer_class.py:877-879`).
  - `_bind_snapshot_arguments`/`_build_from_snapshot`: stored pins become their Pin's `build()` Expression; call-time values serialize with the pin celltype (as now); typed inputs convert (Phase 5); Pins are rejected.
  - `_clone_transformer_builder` and `_refheld_checksums`/`_release_refholds` hold the stored inputs.
  - **Compiled transformers** get the same Pin surface. This also fixes `compiled_transformer.py:522`, which calls `ArgsWrapper` without `owner`, so `.args`/`.pins` on a compiled transformer raise TypeError today.
- **Tests:**
  - `tf.pins.x = 10` stores `Buffer(10, celltype)`'s checksum, not `10`;
  - `pin.celltype` and `tf.celltypes.x` stay linked both ways;
  - `input_celltype` is read-only;
  - a retype converts at call time;
  - an invalid value raises at assignment (standalone now matches bound);
  - a Pin is rejected as a Cell input, an Expression input and a call argument;
  - an unset pin reads as an unwired Pin;
  - a compiled transformer's `.pins` works.

### Phase 7: Bound pins (seamless-workflow)
- `BoundPinBackend(context, node_path, pin)`. `WorkflowTransformerPins.__getitem__` returns a Pin; delete `_get_pin` (`builder_state.py:464-474`). `pin.celltype` goes through `_set_node_config(path, "celltypes", value, key=pin)`.
- `_transformer_endpoint(node_path, pin)` (`context.py:834-835`) gets `can_source=False`.
- `_derive_transformer` (`reactive.py:36-51`), for each pin:
  - take `(checksum, input celltype)` from the producer (`producer.celltype`) or the edge (`_node_celltype(source)`);
  - if the input celltype ≠ `cfg.celltypes[pin]`, call `_projection`;
  - pending means waiting on that pin; failed means the pin is `failed` and the transformer is `blocked-by-error` on it.
  - Keep per-pin runtime state (`node.pin_states`, not durable) for the backend's `state`/`checksum`/`value`/`exception`.
- `_snapshot_transformer` (`runtime_api.py:80-87`): connected pins get the converting Expression (`input_celltype=` the source's celltype, `celltype=celltypes[pin]`); constants already convert.
- The `_set_transformer_pin` preparation (`ingress.py:180-188`): reject Pins with the clear error; support `pin.set_checksum(cs, input_celltype=)`. Serializing values is unchanged.
- **Pins join the declare-the-input family**: assigning a value to a connected pin detaches instead of raising, so `_set_transformer_pin` drops its `_check_authority` call (context.py:733) and removes the edge the way the source path already does. `pin.set()` keeps the check.
- **`del ctx.tf.pins.x` deletes the declaration for signature-less code and raises when the signature is fixed** — today's standalone rule, now bound as well. `_delete_transformer_pin` therefore also drops the pin from `cfg.pins`, `cfg.celltypes` and `optional_pins`, and gains a sibling that only clears the producer or edge, behind `pin.checksum = None`.
- **Tests:**
  - port `test_celltype_changes.py` to pins: 4 source kinds × retypes, through both `tf.celltypes.x` and `pin.celltype`;
  - a checksum-level identity assertion;
  - `ctx.tf.run()` and `ctx.tf().run()` agree after a retype;
  - a failed conversion gives `pin.exception` and a `blocked-by-error` transformer, and no transformation is built;
  - these are all refused: `ctx.b = ctx.tf.pins.x`, `ctx.tf2.pins.y = ctx.tf.pins.x`, `Cell(source=ctx.tf.pins.x)`;
  - `ctx.tf2.pins.y = ctx.tf.pins.x.source` works;
  - an unset pin reads as an unwired Pin;
  - the celltype link works in both directions.

### Phase 8: Docs and memory
- **Rewrite:**
  - READMEs: seamless-core, seamless-transformer (pins), seamless-database, seamless-workflow;
  - `seamless-core/type_bits_design.md`;
  - `seamless/attachments-and-mount-design.md`, `mount-design.md`, `context-internals-design-pass3.md`;
  - `optional-pins-implementation-plan.md`: the JSON-Null Celltype Rule (127-149) now applies to results, not to the pin's celltype; and pass3's admitted restriction 2 (496-503) is gone for pins;
  - `context-internals-followup-design.md`: Appendix A.5 (bound `del ctx.tf.pins.x` now deletes the declaration, not the producer); §Pins lines 479-482 (the Pin read form, not-a-source, the celltype link, holding checksums) and decision 10 ("no … Transformer-input target wrapper is public").
- **One-line rename note at the top of the historical plans:**
  - `cells-and-expressions-implementation-plan.md`, `cells-and-expression-handoff-ready-implementation-plan.md`;
  - `context-internals-design-pass2.md`, `context-workflow-internals-implementation-plan.md`, `context-workflow-internals-implementation-handoff-plan.md` (its `None`-as-deletion-sugar rule, lines 594-617, also needs a superseded note pointing at followup:270-271);
  - `cancellation-improvement-plan.md`.
- **Memory:** update the celltype-naming and pin-conversion memories with the final semantics.

## Verification
- **Tests** (conda env `seamless1`):
  - seamless-workflow: `tests/run-tests.sh`;
  - other repos: one pytest process per file: `for f in tests/test_*.py; do python -m pytest -q "$f"; done`.
  - Compare against the Phase 0 baseline after every phase. Phases 1–3 must match exactly, apart from the added blocking tests. `test_celltype_changes.py` goes green in Phase 4.
- **Static checks:**
  - after Phase 4, `grep -rnw target_celltype` hits only the conversion helpers and the retired-name set;
  - wire and DB code shows only the new keys;
  - after Phase 6, no pin storage holds a non-reference value.
- **Probes:**
  - Port the rename probe scripts (`celltype_probe*.py`) to the new names. All three probe cases must agree between standalone and Context, and a connected cell's checksum and `.value` must agree.
  - Rerun the pin probes (`test_pin_probe.py`, `test_pin_probe2.py` in scratchpad `/tmp/claude-1001/-home-agent-seamless1/171ace37-f5af-4b44-9249-5f58a30f6cc9/scratchpad/`, copied into `seamless-workflow/tests/` to run). Every reinterpretation row must now show conversion.
- **Remote end-to-end:** start the local hashserver, database and jobserver (seamless-remote-debugging skill). Evaluate an Expression with `execution="remote"`; the DB row must use the new columns, and a second evaluation must be a cache hit. Repeat through dask (`seamless-dask/tests/test_expression_inputs.py`), including a transformation with a converted pin.

## Out of scope
- A Pin as a source. It could come later as a new graph source kind for the pin's converted value.
- Assigning a standalone Cell or Expression to a bound pin (it fails today). A pin has one producer slot, which can't hold a deferred expression.
- Per-pin settings beyond `celltype` (legacy `PinWrapper`-style `io`/`as_`); `optional_pins` stays on the Transformer.
- The rest of pass3 Part II beyond the null-result relaxation in Phase 5 (the future-wired shape changes when an optional pin is dropped mid-flight).
