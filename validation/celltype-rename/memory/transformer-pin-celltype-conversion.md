---
name: transformer-pin-celltype-conversion
description: "Implemented standalone and bound Pin handles, conversion before execution, linked output types, checksum storage, null boundary rules"
metadata: 
  node_type: memory
  type: project
  originSessionId: 171ace37-f5af-4b44-9249-5f58a30f6cc9
  modified: 2026-09-14T00:00:00.000Z
---

Implemented through Phases 5–7 of `/home/agent/.claude/plans/ok-please-write-a-curried-toucan.md` (2026-09-14). The former reinterpretation and reactive/snapshot divergence are fixed.

Pin and Cell are sister subclasses of CellBase. `tf.pins.x` (also args) returns a fresh Pin handle in standalone, compiled and bound modes, including declared-but-unwired pins. Undeclared pins raise AttributeError. Pin output celltype and tf.celltypes.x are linked both ways; input_celltype is read-only and follows the source or stored declaration. Pins expose source/checksum/buffer/value/state/exception/build/compute, but no Cell projection, validator, mount, or source protocol. A Pin cannot feed a Cell, Expression, another pin or a call; connect pin.source instead.

Standalone argument storage holds (input reference, input celltype), never raw values. Assignment serializes literals immediately and retains checksum ownership; typed inputs retain their type. Snapshots convert typed dependencies to the pin type before execution and before transformation identity is constructed. Bound reactive pins use the same conversion; failed conversion gives pin.exception, blocks the transformer by that pin, and builds no current transformation. Reactive run() and snapshot () .run() agree, including checksum identity.

Value/buffer/checksum attribute writes detach a connection; set/set_buffer/set_checksum require ownership. Value None stores null; checksum/buffer None clears the input while retaining the declaration. del tf.pins.x deletes a declaration only for signatureless code and raises for a fixed signature. Compiled aliases preserve this API after binding.

Optional null inputs are dropped before conversion/deserialization for any pin type. A missing connected checksum still blocks. Required null pins are allowed only for plain/mixed/bytes (bytes resolves b""); other required types raise a pin-naming error. None results are allowed only for plain/mixed/bytes, using the canonical null checksum; other result types fail. Empty bytes, including bare legacy empty-byte checksums, normalize to null before these checks. Cells themselves admit null in every type.

Verification includes a 40-case bound source/retype matrix, both original pin probes, standalone and bound handle contracts, compiled aliases, checksum identity, live Cell source snapshots, real dask worker conversion, and a remote Expression DB-schema/cache-hit test. Assigning a standalone Cell/Expression directly to a bound pin and using a Pin as a source remain out of scope.
