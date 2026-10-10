"""Stage 0 golden bytes/errors for the evaluator being replaced.

The JSON is a recorded standalone probe, not recomputed expectations. Stage 1
can reuse its supported-key cases without importing workflow code.
"""

import builtins
import json
from pathlib import Path

import numpy as np
import pytest

from seamless import Buffer, Checksum
from seamless_workflow.sidework import evaluate_cell


GOLDEN_CASES = json.loads(
    (Path(__file__).parent / "data/celljoin_sidework_golden.json").read_text()
)
DEEP_TYPES = {"deepcell", "deepfolder", "folder"}


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case["name"])
def test_sidework_join_golden(case, monkeypatch):
    target = case["target"]
    held_buffers = []

    def checksum(value, celltype):
        buffer = Buffer(value, celltype)
        buffer.tempref()
        held_buffers.append(buffer)
        return buffer.get_checksum()

    root_value = case["root"]
    if case.get("root_kind") == "structured":
        root_value = np.array(
            (1, 2), dtype=np.dtype([("a", "i8"), ("b", "i8")], align=True)
        )[()]
    elif case.get("root_kind") == "ndarray":
        root_value = np.array([1, 2, 3])
    root = checksum(root_value, target) if case["root_present"] else None
    inputs = []
    for key, value in case["members"]:
        member_type = (
            "mixed" if target == "deepcell"
            else "bytes" if target in {"deepfolder", "folder"}
            else target
        )
        member = Checksum(value) if target in DEEP_TYPES else checksum(value, target)
        inputs.append(((key,), member, member_type))

    if target in DEEP_TYPES:
        member_checksums = {member for _, member, _ in inputs}
        original_resolve = Checksum.resolve

        def guarded_resolve(self, *args, **kwargs):
            assert self not in member_checksums, "deep joins must not resolve members"
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Checksum, "resolve", guarded_resolve)

    if "error_type" in case:
        with pytest.raises(getattr(builtins, case["error_type"])) as exc_info:
            evaluate_cell(root, target, inputs, target)
        assert str(exc_info.value) == case["error_message"]
    else:
        result = evaluate_cell(root, target, inputs, target)
        expected_bytes = bytes.fromhex(case["result_hex"])
        assert result.resolve().content == expected_bytes
        assert result == Buffer(expected_bytes).get_checksum()
