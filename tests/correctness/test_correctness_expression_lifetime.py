"""[MOD-16] — an ``Expression`` must claim its ``input_ref``.

§15 A0: *"Fix ``Expression`` to claim its ``input_ref``, and add a lifetime test
(**[MOD-16]**)."*  This is that test.

§7 states the rule the fix has to satisfy: a checksum stays resolvable for as
long as some holder claims it, and every holder claims in the turn that
acquires it.  A ``Cell`` claims its input checksum with the ``"input"`` role
today; an ``Expression`` built off that Cell does not.  So an Expression that
has *escaped* the Context — the caller holds it, the Context has moved on and
been pruned — keeps working only by temporal coincidence: while some transient
buffer still happens to be resident.  When the buffer expires, ``run()`` raises,
and the failure is a cache miss far away from the assignment that caused it.

The measurement is therefore deliberately three-part, because any one part alone
can pass for the wrong reason:

1. the Expression still names the original checksum (``input_ref``) — cheap, and
   true even today;
2. the registry attributes exactly one claim, with the ``"input"`` role, to the
   Expression, and the buffer cache's reference count for that checksum is 1 —
   this is the part that fails today, and it is what "claims" means;
3. after *forcing* expiry of the buffer, ``run()`` still returns the original
   value — the end-to-end consequence, which is the only part a user would ever
   notice.

Forcing expiry is what makes (3) an assertion rather than a race: without it the
buffer is simply still resident and the test passes whether or not anything
claimed anything.

The fix is A0 work, so this test is expected green as soon as [MOD-16] lands —
at the latest by A1, which is the earliest marker the suite has.  It is
independent of synchronicity: nothing here executes a transformer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "helpers"))
from reference_lifecycle import force_expiry  # noqa: E402


@pytest.mark.a1
def test_an_expression_owns_its_input_after_the_context_moves_on(make_context):
    """The escaped Expression, not temporal coincidence, keeps its input resolvable."""

    ctx = make_context()
    original_value = {"token": f"original-{uuid4().hex}"}
    ctx.value = original_value
    original_checksum = ctx.value.checksum

    expression = ctx.value.build()

    for index in range(5):
        ctx.value = {"token": f"replacement-{index}-{uuid4().hex}"}
    ctx.prune()

    assert expression.input_ref == original_checksum

    claims = collect_refholder_claims([expression])
    roles = [role for _holder, role in claims.get(original_checksum, [])]
    assert roles == ["input"], (
        f"[MOD-16]: the Expression claims {roles} on its own input checksum; "
        f"§7 requires exactly one 'input' claim, acquired in the turn that built it"
    )

    snapshot = get_buffer_cache().reference_snapshot()
    assert snapshot.get(original_checksum, (0,))[0] == 1, (
        "the Context released its own claim on prune(), so the Expression's claim "
        "is the only thing keeping this checksum resolvable"
    )

    force_expiry(original_checksum)
    assert expression.run() == original_value


@pytest.mark.a1
def test_a_dropped_expression_stops_holding_its_input(make_context):
    """The other half of a lease: it has to end.

    A claim that is never released is not a lifetime fix, it is a leak.  The
    same checksum must fall out of the accounting once nothing holds it — which
    is what makes the count of 1 above a claim rather than a coincidence.
    """

    ctx = make_context()
    original_value = {"token": f"original-{uuid4().hex}"}
    ctx.value = original_value
    original_checksum = ctx.value.checksum

    expression = ctx.value.build()
    ctx.value = {"token": f"replacement-{uuid4().hex}"}
    ctx.prune()

    assert get_buffer_cache().reference_snapshot().get(original_checksum, (0,))[0] == 1

    del expression

    import gc

    gc.collect()

    assert get_buffer_cache().reference_snapshot().get(original_checksum, (0,))[0] == 0, (
        "the Expression's claim outlived the Expression"
    )
