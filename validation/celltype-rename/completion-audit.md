# Completion audit

Implementation follows the accompanying plan, with conflicting checklist clauses
resolved as documented in README.md. Per-phase JSON files compare the file-level
gates against baseline; the README records failures, corrections and reruns.

| Phase | Objective and evidence |
| --- | --- |
| 0 | Prerequisite commits and clean implementation branches; baseline.json records 145 files and the pre-existing Expression ownership assertion failure. |
| 1 | Retired-name guards and keyword-only Expression arguments; positional calls ported; six guard tests and phase1.json. |
| 2 | Input-side naming across core, evaluator, workflow and distributed payloads; phase2.json. |
| 3 | Output-side naming and graph 0.4 compatibility; DB/wire lockstep; phase3.json. Real remote Expression execution checks SQLite columns, row contents and a second-evaluation cache hit in transformer tests/test_expression_remote_schema.py. |
| 4 | Converted Cell output, live/declared input types, source/checksum split, six writes, null and mounts; core tests/test_cell_semantics.py, workflow tests/test_celltype_changes.py and mount contracts; phase4.json. |
| 5 | Typed pin conversion precedes transformation identity; optional null removal precedes conversion; required/result boundary rules; transformer and dask tests/test_pin_conversion.py; phase5.json. |
| 6 | CellBase and sister Pin, fresh/unwired handles, reference/type storage, ownership, compiled pins and snapshot recipes; core tests/test_cell_base.py and transformer tests/test_pin_handles.py; phase6.json. |
| 7 | Bound Pin runtime state and pre-execution conversion, six writes, clearing/deletion, source rejection, linked types and compiled aliases; workflow tests/test_pin_handles.py, 40-case test_pin_celltype_changes.py, test_pin_probe.py and test_pin_probe2.py; phase7.json. |
| 8 | Four package READMEs, type_bits_design, both mount designs, pass3/followup and optional-pin rules updated; six historical plans carry supersession notes. Naming and pin-conversion memories updated, with exact committed copies in memory/. Full final gate in phase8.json. |

Additional probe assertions are in celltype_probes.py. The original pin probes
are part of the workflow gate. The remote schema/cache test launches local
hashserver/database/jobserver services on a fresh database; dask tests run actual
local distributed workers, including converted pin checksum identity.

Static audit: public retired names are guarded; remaining target_celltype source
occurrences are conversion helper argument pairs, the retired-name registry,
legacy graph validation, and an unrelated local code-deserialization variable.
There are no old expression wire/DB keys. Compatibility tests and historical
quoted observations deliberately retain old spellings. Standalone pin mutations
store reference/type pairs and clones preserve checksum claims; tests check both
storage and lifetime, rather than relying solely on text search.

The corrected baseline ownership test asserts the intentionally retained input
claim while continuing to prohibit a retained Expression result claim. This is
the only original baseline failure and is green from Phase 6 onward.

Null is valid Cell storage in every type. Function boundaries accept required
null only for plain/mixed/bytes and drop optional null for all types. Files read
missing/empty/canonical-null as null without rewriting; null writes empty files,
including compressed mounts. Directory absence is null, but an empty directory
is an empty mapping. These choices implement the plan's explicit decided rules
where older checklist text conflicts.

Memory originals live under
/home/agent/.claude/projects/-home-agent-seamless1/memory/; the two names match
this directory's memory/ copies. The memory index also reflects implementation.
Unrelated edits in the design repository were preserved and excluded from commits.
