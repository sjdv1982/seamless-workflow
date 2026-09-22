"""Use the core witness corpus for the bound half of Cell builder tests."""
import importlib.util
import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[3] / "seamless-core/tests/helpers/expression_hashtype_cases.py"
_SPEC = importlib.util.spec_from_file_location("seamless_core_expression_cases", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

expression_case_id = _MODULE.expression_case_id
iter_expression_cases = _MODULE.iter_expression_cases
