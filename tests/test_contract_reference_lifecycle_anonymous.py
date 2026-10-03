"""Contract tests for checksum-reference-lifecycle.md §§5 and 6.

These tests check Context-held anonymous current claims for non-elided and
elided anonymous nodes. Projection and bound-handle tests isolate the lifecycle
roles from cell conversion behavior.
"""

from __future__ import annotations

import gc
import json
import re
import uuid

import pytest

from seamless import Buffer, Cell, Checksum
from seamless.caching import buffer_writer
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims
from seamless_workflow import Context

# Known cells gap, the first failure point of the handle tests: a bound
# ``as_celltype`` returns a standalone Cell snapshot (not an anonymous handle),
# and its ``.checksum`` is the source's checksum, unconverted (contrary to
# contract-clarity-rulings sub-ruling (a)).
SNAPSHOT_GAP = (
    "first failure: bound as_celltype returns a standalone Cell snapshot whose "
    ".checksum is the unconverted source checksum (known cells gap); then "
)
# Known gap, the first failure point of the Context tests: anonymous nodes do
# not exist, so binding a handle raises TypeError.
ANONYMOUS_CURRENT = re.compile(r"^anonymous:[^:]+:current$")


@pytest.fixture
def writes(monkeypatch):
    written: list[Checksum] = []
    monkeypatch.setattr(
        buffer_writer, "register", lambda buf: written.append(buf.get_checksum())
    )
    return written


def _text_source(ctx):
    payload = {"k": f"anonymous-{uuid.uuid4().hex}"}
    ctx.b = Cell("text")
    ctx.b.set(json.dumps(payload))
    ctx.compute(timeout=10)
    return payload


def _yaml_source(ctx):
    payload = {"k": f"anonymous-{uuid.uuid4().hex}"}
    # Core deliberately does not reinterpret arbitrary text as plain JSON.
    # YAML is a supported serialized source for a plain mapping conversion.
    import yaml
    ctx.b = Cell("yaml")
    ctx.b.set(yaml.safe_dump(payload))
    ctx.compute(timeout=10)
    return payload


def _handle_result_claims(checksum):
    """Claims on ``checksum`` that are not the Context's own."""
    return [
        (holder, role)
        for holder, role in collect_refholder_claims().get(checksum, [])
        if not isinstance(holder, Context)
    ]


def test_handle_holds_a_scratch_neutral_result_claim(writes):
    cache = get_buffer_cache()
    ctx = Context()
    try:
        payload = _yaml_source(ctx)
        expected = Buffer(payload, "plain").get_checksum()
        cache.mark_scratch(expected)
        handle = ctx.b.as_celltype("plain")
        pulled = handle.checksum
        assert pulled == expected, "yaml -> plain must convert"
        assert [role for _, role in _handle_result_claims(pulled)].count("result") >= 1
        assert pulled not in writes, "a handle's result claim published"
        assert cache.is_scratch_ref(pulled) is True, "a handle's claim cleared scratch"
    finally:
        ctx._release_refholds()


def test_handle_result_claim_is_released_when_the_handle_dies():
    cache = get_buffer_cache()
    ctx = Context()
    try:
        payload = _yaml_source(ctx)
        expected = Buffer(payload, "plain").get_checksum()
        handle = ctx.b.as_celltype("plain")
        pulled = handle.checksum
        assert pulled == expected, "yaml -> plain must convert"
        assert _handle_result_claims(pulled)
        del handle
        gc.collect()
        assert _handle_result_claims(pulled) == []
        context_claims = len(collect_refholder_claims([ctx]).get(pulled, []))
        assert cache.reference_snapshot().get(pulled, (0, 0, False))[0] == context_claims
    finally:
        ctx._release_refholds()


def _nested_source(ctx):
    token = f"projection-{uuid.uuid4().hex}"
    ctx.b = Cell("plain")
    ctx.b.set({"k": {"v": token}})
    ctx.compute(timeout=10)
    return Buffer({"v": token}, "plain").get_checksum()


def test_bound_handle_onto_a_named_node_owns_no_references(writes):
    ctx = Context()
    try:
        _nested_source(ctx)
        handle = ctx.b
        checksum = handle.checksum
        assert checksum is not None
        handle.value
        assert list(handle._refheld_checksums()) == []
        holders = collect_refholder_claims().get(checksum, [])
        assert holders and all(isinstance(holder, Context) for holder, _ in holders)
        count = get_buffer_cache().reference_snapshot()[checksum][0]
        assert count == len(holders), "the handle added an unattributed reference"
    finally:
        ctx._release_refholds()


PROJECTION_GAP = (
    "a bound projection handle (ctx.b['k']) pulls the projected checksum but, like "
    "a bound handle onto a named node, holds no claim at all"
)


def test_projection_handle_pull_acquires_a_neutral_result_claim(writes):
    cache = get_buffer_cache()
    ctx = Context()
    try:
        expected = _nested_source(ctx)
        cache.mark_scratch(expected)
        writes.clear()
        handle = ctx.b["k"]
        pulled = handle.checksum
        assert pulled == expected, "precondition: the projection pulls the member"
        roles = [role for _, role in _handle_result_claims(pulled)]
        assert roles == ["result"], f"expected one handle result claim, found {roles}"
        assert pulled not in writes, "a handle's result claim published"
        assert cache.is_scratch_ref(pulled) is True, "a handle's claim cleared scratch"
    finally:
        ctx._release_refholds()


def test_projection_handle_claim_is_released_when_the_handle_dies():
    cache = get_buffer_cache()
    ctx = Context()
    try:
        expected = _nested_source(ctx)
        handle = ctx.b["k"]
        assert handle.checksum == expected
        assert _handle_result_claims(expected), "precondition: the handle holds a claim"
        del handle
        gc.collect()
        assert _handle_result_claims(expected) == []
        context_claims = len(collect_refholder_claims([ctx]).get(expected, []))
        assert cache.reference_snapshot().get(expected, (0, 0, False))[0] == context_claims
    finally:
        ctx._release_refholds()


def test_context_holds_anonymous_current_for_a_non_elided_node():
    ctx = Context()
    try:
        payload = _text_source(ctx)
        # text -> plain is a reformat conversion: a fusion barrier, so the
        # intermediate anonymous node is not elided and the Context evaluates it.
        ctx.c = ctx.b.as_celltype("plain")["k"]
        ctx.compute(timeout=10)
        assert ctx.c.value == payload["k"]
        assert ctx.get_graph().get("anonymous_nodes")
        intermediate = ctx.b.checksum
        roles = [
            role
            for checksum, role in ctx._refheld_checksums()
            if ANONYMOUS_CURRENT.match(role)
        ]
        assert len(roles) == 1, roles
        assert any(
            checksum == intermediate and ANONYMOUS_CURRENT.match(role)
            for checksum, role in ctx._refheld_checksums()
        )
    finally:
        ctx._release_refholds()


def test_context_holds_no_anonymous_current_for_an_elided_node():
    ctx = Context()
    try:
        token = f"elided-{uuid.uuid4().hex}"
        ctx.m = {"k": token}  # mixed
        # mixed -> plain is a reinterpret conversion: fusible, so the
        # intermediate anonymous node is elided.
        ctx.e = ctx.m.as_celltype("plain")["k"]
        ctx.compute(timeout=10)
        assert ctx.e.value == token
        assert ctx.get_graph().get("anonymous_nodes"), "the entry stays in the graph"
        assert not [
            role for _cs, role in ctx._refheld_checksums() if role.startswith("anonymous:")
        ]
    finally:
        ctx._release_refholds()
