"""Fixtures for the phase-A0 contract suite.

Design reference: ``seamless/attachments-and-mount-design.md`` §15 (A0/A1).

Two jobs.

**A per-test cache and refholder-registry reset.**  §15 A1 records why this is
not optional: one genuine failure manufactures spurious audit failures in later
tests through process-global cache state — ``test_failed_transformer_replacement_restores_graph_and_roles``
fails with *"Checksum ... has refholder count 1 but only 0 live claims"* only
when it runs after another failing test, and passes in isolation.  A suite whose
whole point is to be red would otherwise poison itself, and an ``xfail``ed test
still runs and still pollutes.  A0 owns this fixture and A1 depends on it.

**Marker registration.**  Nothing here is ``xfail``-marked: A0's exit evidence is
*a red suite that describes the intended contract*, and an ``xfail`` would hide
exactly the signal the phase exists to produce.  ``xfail(strict=True)`` arrives
at A1, for the suites the design names there.  Until then each test carries the
phase at which it is expected to turn green, as a selectable marker:

===========  =============================================================
``now``      must pass against the current implementation (regression net)
``a1``       expected green from A1 (synchronicity broken, state machine correct)
``a2``       expected green from A2 (topology through class-2 messages)
``a3``       expected green from A3 (barriers, reads, writes, expressions)
``a4``       expected green from A4 (real execution; limbo ends)
``slow``     runs a sleeping transformer body; seconds, not milliseconds
===========  =============================================================

``pytest -m a1`` selects one phase; ``pytest -m "not slow"`` skips the sleepers.
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


PHASE_MARKERS = {
    "now": "must pass against the current implementation (regression net)",
    "a1": "expected to pass from phase A1 onwards",
    "a2": "expected to pass from phase A2 onwards",
    "a3": "expected to pass from phase A3 onwards",
    "a4": "expected to pass from phase A4 onwards",
    "slow": "runs a sleeping transformer body",
}


def pytest_configure(config):
    for name, description in PHASE_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def _reset_process_caches() -> None:
    """Drop process-global state that would otherwise leak between tests.

    Deliberately does **not** touch ``TransformationCache._active_submissions``:
    those are live submissions, and clearing the dict would orphan running work
    rather than reset it.

    Clearing the accounting can make a later collection log ``Refholder decref
    ignored ... already zero``, when a failed test's frame keeps a Context alive
    past the reset that cleared its counts.  That message is an artifact of the
    reset, not a finding.
    """

    from seamless.caching.buffer_cache import get_buffer_cache
    from seamless.checksum.expression import get_expression_cache
    from seamless.reference_lifecycle import clear_refholder_registry_for_tests
    from seamless_transformer.transformation_cache import get_transformation_cache

    gc.collect()
    clear_refholder_registry_for_tests()
    get_buffer_cache().force_clear_reference_accounting()
    get_expression_cache().clear()
    cache = get_transformation_cache()
    cache._transformation_cache.clear()
    cache._rev_transformation_cache.clear()
    cache._transformation_dunder_cache.clear()


@pytest.fixture(autouse=True)
def reset_caches_and_refholders():
    """Reset before and after every test, so neither direction leaks."""

    _reset_process_caches()
    yield
    _reset_process_caches()


@pytest.fixture
def execution_log(tmp_path):
    """A file-backed execution counter (see :class:`contract_helpers.ExecutionLog`)."""

    from contract_helpers import ExecutionLog

    return ExecutionLog(tmp_path / "executions.log")
