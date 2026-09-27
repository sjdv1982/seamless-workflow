"""Contract tests: contracts/internal/checksum-reference-lifecycle.md (feature 9),
Context claims and scratch.

Ruling (contract-clarity-rulings.md, "Rulings requested by coverage subagents"):
"Claims held for scratch nodes must not publish." That covers every claim the
Context holds for a scratch node: its current and superseded results, a copied
node's result, a scratch cell's literal, and a scratch transformer's pin, code
and module claims.

§1 *Neutral claim*: a Context's snapshot and in-flight leases are neutral claims
(protect, never publish, never change scratch status); a Context node's claim on
its current result follows the node's scratch policy. §8: only a non-scratch
owner publishes, and persistence is a property of who holds.

§10 gaps pinned here (xfail): pin/code claims and module claims of a scratch node.
The ruling also covers
``anonymous:<symbol>:current``, which cannot be exercised until anonymous nodes
exist (see test_contract_reference_lifecycle_anonymous.py).
"""

from __future__ import annotations

import uuid

import pytest

from seamless import Buffer, Cell, Checksum
from seamless.caching import buffer_writer
from seamless.caching.buffer_cache import get_buffer_cache
from seamless_workflow import Context

DOC = "checksum-reference-lifecycle.md"
RULING = "(ruling: claims held for scratch nodes must not publish)"


@pytest.fixture
def writes(monkeypatch):
    written: list[Checksum] = []
    monkeypatch.setattr(
        buffer_writer, "register", lambda buf: written.append(buf.get_checksum())
    )
    return written


def _unique_text() -> str:
    return f"scratch-node-{uuid.uuid4().hex}"


def _derived(ctx, value, *, scratch):
    # text -> str changes the buffer, so the node result never coincides with
    # the literal (whose producer role is always non-scratch). The policy is
    # set before the value arrives, so no earlier non-scratch run exists.
    ctx.src = Cell("text")
    ctx.dst = ctx.src
    ctx.dst.celltype = "str"
    ctx.dst.scratch = scratch
    ctx.src.set(value)
    ctx.compute(timeout=10)
    return ctx.dst.checksum


def test_snapshot_lease_is_a_neutral_claim(writes):
    from seamless_workflow.sidework import Lease

    cache = get_buffer_cache()
    buf = Buffer(f"lease-{uuid.uuid4().hex}".encode())
    checksum = buf.get_checksum()
    cache.tempref(checksum, buffer=buf)
    cache.mark_scratch(checksum)

    lease = Lease(checksum)
    try:
        assert cache.reference_snapshot()[checksum][0] == 1
        assert cache.is_scratch_ref(checksum) is True
        assert checksum not in writes
    finally:
        lease._release_refholds()
    assert cache.reference_snapshot().get(checksum, (0, 0, False))[0] == 0


@pytest.mark.parametrize("scratch", [False, True], ids=["non-scratch", "scratch"])
def test_node_current_claim_follows_the_cell_scratch_policy(writes, scratch):
    cache = get_buffer_cache()
    ctx = Context()
    try:
        checksum = _derived(ctx, _unique_text(), scratch=scratch)
        assert checksum is not None
        assert cache.is_scratch_ref(checksum) is scratch
        assert (checksum in writes) is (not scratch)
    finally:
        ctx._release_refholds()


def test_superseded_hold_on_a_scratch_node_neither_publishes_nor_clears_scratch(writes):
    cache = get_buffer_cache()
    ctx = Context()
    try:
        first = _derived(ctx, _unique_text(), scratch=True)
        assert first not in writes
        ctx.src.set(_unique_text())
        ctx.compute(timeout=10)
        held = [
            record.result_checksum
            for record in ctx._runtime.superseded_runs.get(("dst",), ())
        ]
        assert first in held, "precondition: the old result is under a superseded hold"
        assert cache.reference_snapshot()[first][0] >= 1
        assert first not in writes, "superseded hold published a scratch result"
        assert cache.is_scratch_ref(first) is True
    finally:
        ctx._release_refholds()


def test_subcontext_copy_of_a_scratch_node_does_not_publish(writes):
    ctx = Context()
    try:
        ctx.sub = Context()
        ctx.sub.src = Cell("text")
        ctx.sub.dst = ctx.sub.src
        ctx.sub.dst.celltype = "str"
        ctx.sub.dst.scratch = True
        ctx.sub.src.set(_unique_text())
        ctx.compute(timeout=10)
        checksum = ctx.sub.dst.checksum
        assert checksum not in writes
        ctx.sub2 = ctx.sub
        ctx.compute(timeout=10)
        assert ctx._graph.nodes[("sub2", "dst")].cell_config.scratch is True
        assert checksum not in writes, "copying a scratch node published its result"
    finally:
        ctx._release_refholds()


def test_a_scratch_cell_literal_claim_does_not_publish(writes):
    ctx = Context()
    try:
        ctx.a = Cell("text")
        ctx.a.scratch = True
        ctx.a.set(_unique_text())
        ctx.compute(timeout=10)
        checksum = ctx.a.checksum
        assert checksum is not None
        assert checksum not in writes, "a scratch cell's literal claim published"
    finally:
        ctx._release_refholds()


def _suffix(word):
    return word + "-out"


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} §1/§6 {RULING}: contract ahead of code: the transformer:<path>:pin "
    "and transformer:<path>:code roles are acquired with incref_refholder() "
    "(scratch=False) regardless of the transformer's scratch, so they publish",
)
def test_a_scratch_transformer_pin_and_code_claims_do_not_publish(writes):
    from seamless.transformer import delayed

    builder = delayed(_suffix)
    builder.local = True
    ctx = Context()
    try:
        ctx.tf = builder
        ctx.tf.scratch = True
        ctx.tf.pins.word = _unique_text()
        ctx.compute(timeout=20)
        claims = {
            role: checksum
            for checksum, role in ctx._refheld_checksums()
            if role.startswith("transformer:tf:")
        }
        assert {"transformer:tf:pin:word", "transformer:tf:code"} <= set(claims)
        published = [role for role, checksum in claims.items() if checksum in writes]
        assert published == [], f"claims for a scratch transformer published: {published}"
        result = ctx._graph.nodes[("tf",)].current_checksum
        assert result is not None and result not in writes
    finally:
        ctx._release_refholds()


@pytest.mark.xfail(
    strict=False,
    reason=f"{DOC} §1/§6/§10 {RULING}: contract ahead of code: the "
    "transformer:<path>:module:<name> role is acquired with incref_refholder() "
    "(scratch=False, context.py:637) regardless of the transformer's scratch, so a "
    "locally present module buffer is published",
)
def test_a_scratch_transformer_module_claim_does_not_publish(writes):
    from seamless.transformer import delayed

    cache = get_buffer_cache()
    builder = delayed(_suffix)
    builder.local = True
    module = Buffer(f"# scratch-module-{uuid.uuid4().hex}\n".encode())
    checksum = module.get_checksum()
    cache.tempref(checksum, buffer=module)  # the buffer is here to be written
    ctx = Context()
    try:
        ctx.tf = builder
        ctx.tf.scratch = True
        ctx.tf.modules.example = checksum
        assert (checksum, "transformer:tf:module:example") in tuple(
            ctx._refheld_checksums()
        ), "precondition: the Context holds the module claim"
        assert checksum not in writes, "a scratch transformer's module claim published"
    finally:
        ctx._release_refholds()
