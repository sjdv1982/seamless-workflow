from __future__ import annotations

import gc
import importlib

import pytest


def _reset_process_caches() -> None:
    """Give every contract test independent content-addressed process state.

    A1 deliberately leaves many existing workflow tests unable to finish their
    transformations.  A failed test must therefore not leave transformation
    results or refholder registrations for the next test to observe.
    """

    buffer_cache = importlib.import_module("seamless.caching.buffer_cache")
    transformation_cache = importlib.import_module(
        "seamless_transformer.transformation_cache"
    )
    expression_cache = importlib.import_module("seamless.checksum.expression")
    checksum_cache = importlib.import_module(
        "seamless.checksum.cached_calculate_checksum"
    )
    reference_lifecycle = importlib.import_module("seamless.reference_lifecycle")

    buffer_cache._cache_instance = buffer_cache.BufferCache()
    transformation_cache._transformation_cache_instance = (
        transformation_cache.TransformationCache()
    )
    expression_cache.get_expression_cache().clear()
    getattr(expression_cache, "_active_expressions", {}).clear()
    getattr(expression_cache, "_expression_result_buffers", {}).clear()
    checksum_cache.checksum_cache.clear()
    reference_lifecycle.clear_refholder_registry_for_tests()


@pytest.fixture(autouse=True)
def isolated_seamless_process_state():
    """Prevent a red A0 contract test from contaminating later audit tests."""

    gc.collect()
    _reset_process_caches()
    yield
    gc.collect()
    _reset_process_caches()


@pytest.fixture
def make_context(isolated_seamless_process_state):
    """Create Contexts and close/release them even when an A0 assertion fails."""

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
