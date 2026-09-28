"""An escaped ``Expression`` does not own its input (supersedes [MOD-16]).

[MOD-16] in ``seamless/attachments-and-mount-design.md`` asked ``Expression`` to
claim its ``input_ref``.  The checksum reference lifecycle contract rules the
opposite (``contracts/internal/checksum-reference-lifecycle.md`` §6/§7): an
Expression's inputs are tempref-only, and an owner claims a dependency only when
it resolves it.  An Expression built off a bound Cell is a dependency on the
Context's state, and the Context is the owner.

So an Expression that has *escaped* the Context stays resolvable only while the
Context (or the hashserver) keeps its input.  The two tests pin both sides:

1. once the Context has moved on and been pruned, nothing claims the escaped
   Expression's input, and with the buffer forced out it cannot be run;
2. the supported way to keep the value is to capture its checksum into an owner
   (a standalone ``Cell``), which claims it.

Forcing expiry, with remote reads blocked, is what makes the negative an
assertion about claims rather than about what happens to still be resident.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from seamless import CacheMissError, Cell
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "helpers"))
from reference_lifecycle import force_expiry  # noqa: E402


def _guard_remote(monkeypatch):
    try:
        import seamless_remote.buffer_remote as buffer_remote
    except ImportError:
        return

    async def missing(checksum):
        return None

    monkeypatch.setattr(buffer_remote, "get_buffer", missing)


@pytest.mark.a1
def test_an_escaped_expression_does_not_own_its_input(make_context, monkeypatch):
    """The Context, not the escaped Expression, owns the input."""

    ctx = make_context()
    original_value = {"token": f"original-{uuid4().hex}"}
    ctx.value = original_value
    original_checksum = ctx.value.checksum

    expression = ctx.value.build()

    for index in range(5):
        ctx.value = {"token": f"replacement-{index}-{uuid4().hex}"}
    ctx.prune()

    assert expression._input_ref == original_checksum

    claims = collect_refholder_claims([expression])
    roles = [role for _holder, role in claims.get(original_checksum, [])]
    assert roles == [], (
        f"the Expression claims {roles} on its own input checksum; "
        "§6/§7 make an Expression's inputs tempref-only"
    )
    assert get_buffer_cache().reference_snapshot().get(original_checksum, (0,))[0] == 0, (
        "the Context released its claims on prune(), and nothing else may claim "
        "this checksum"
    )

    _guard_remote(monkeypatch)
    force_expiry(original_checksum)
    with pytest.raises(CacheMissError):
        original_checksum.resolve()
    with pytest.raises(CacheMissError):
        expression.run()


@pytest.mark.a1
def test_capturing_the_checksum_into_a_cell_keeps_the_value(make_context, monkeypatch):
    """The supported escape route: an owner captures the checksum directly."""

    ctx = make_context()
    original_value = {"token": f"original-{uuid4().hex}"}
    ctx.value = original_value
    original_checksum = ctx.value.checksum

    captured = Cell(ctx.value.celltype, checksum=original_checksum)

    for index in range(5):
        ctx.value = {"token": f"replacement-{index}-{uuid4().hex}"}
    ctx.prune()

    claims = collect_refholder_claims([captured])
    roles = [role for _holder, role in claims.get(original_checksum, [])]
    assert roles == ["input"], f"the captured Cell claims {roles}"
    assert get_buffer_cache().reference_snapshot().get(original_checksum, (0,))[0] == 1

    _guard_remote(monkeypatch)
    force_expiry(original_checksum)
    assert captured.run() == original_value
