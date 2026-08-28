from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import collect_refholder_claims


_HELPER_PATH = Path(__file__).resolve().parents[2] / "helpers" / "reference_lifecycle.py"
_SPEC = importlib.util.spec_from_file_location("codex_lifecycle_helper", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HELPER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _HELPER
_SPEC.loader.exec_module(_HELPER)
force_expiry = _HELPER.force_expiry


def test_expression_owns_its_input_after_the_context_moves_on(make_context):
    """The escaped Expression, not temporal coincidence, keeps input resolvable."""

    context = make_context()
    original_value = {"token": f"original-{uuid4().hex}"}
    context.value = original_value
    original_checksum = context.value.checksum
    expression = context.value.build()

    for index in range(5):
        context.value = {"token": f"replacement-{index}-{uuid4().hex}"}
    context.prune()

    assert expression.input_ref == original_checksum
    claims = collect_refholder_claims([expression])
    assert [role for _holder, role in claims[original_checksum]] == ["input"]
    assert get_buffer_cache().reference_snapshot()[original_checksum][0] == 1

    force_expiry(original_checksum)
    assert expression.run() == original_value

