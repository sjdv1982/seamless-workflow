"""Fixtures for the whole ``seamless-workflow`` test set.

Design reference: ``seamless/attachments-and-mount-design.md`` §15 (A0/A1).

This is the root conftest: it covers the pre-existing top-level ``test_*.py``
files *and* the phase-A0 contract suite in ``node-transition/``,
``quiescence-barrier/``, ``latency/`` and ``correctness/``.  §15 A1 requires the
reset below to apply to the whole suite, not only to the contract directories,
because pollution crosses files in either direction.

Three jobs.

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
The top-level ``test_*.py`` files carry no marker: they predate the phases and
are unconditional.

**Context lifetime.**  ``make_context`` gives Contexts an explicit release even
when the test that built them fails.

**Transformation observation.**  ``transformation_observations`` switches on
``seamless_transformer.observation`` for one test.
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
def reset_caches_and_refholders(monkeypatch):
    """Reset before and after every test, so neither direction leaks."""

    import weakref
    from seamless_workflow import Context
    contexts = []
    initialize = Context.__init__
    def tracked(context, *args, **kwargs):
        initialize(context, *args, **kwargs)
        contexts.append(weakref.ref(context))
    monkeypatch.setattr(Context, "__init__", tracked)
    _reset_process_caches()
    yield
    for reference in contexts:
        context = reference()
        if context is not None: context.close()
    _reset_process_caches()


@pytest.fixture
def transformation_observations(tmp_path):
    """Record every transformation that reaches an execution decision.

    ``seamless_transformer.observation`` is off by default; this turns it on for
    the duration of one test and yields the reader.  Nothing is passed to the
    transformer, so the observation cannot perturb the transformation identity —
    which is the whole reason it replaced the earlier pin-based counter.
    """

    from seamless_transformer import observation

    with observation.observing(tmp_path / "transformations.log") as observations:
        yield observations


@pytest.fixture
def make_context():
    """Create Contexts and release them even when an assertion fails.

    A test that fails mid-graph leaves its Context alive in the failing frame's
    traceback, past the reset above, which is what produces ``Refholder decref
    ignored ... already zero`` on the next collection.  Creating Contexts through
    this fixture instead gives the release an explicit, ordered home.  It is not
    mandatory — a test that builds a Context directly is fine — but it is the
    right default for anything that leaves work in flight.
    """

    from seamless_workflow import Context

    contexts = []

    def make(*args, **kwargs):
        context = Context(*args, **kwargs)
        contexts.append(context)
        return context

    yield make

    for context in reversed(contexts):
        close = getattr(type(context), "close", None)
        if close is not None:
            close(context)
        else:
            context._release_refholds()
