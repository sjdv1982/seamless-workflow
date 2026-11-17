"""Patch 07: a poisoned controller is logged and visible via Context.controller_failure."""

import logging

import pytest

from seamless_workflow import Context
from seamless_workflow.errors import ControllerFailedError


def test_poison_is_logged_and_exposed_as_controller_failure(caplog):
    ctx = Context()
    try:
        ctx.a = 1
        ctx.compute(timeout=10)
        assert ctx.controller_failure is None
        with pytest.raises(AttributeError):
            ctx.controller_failure = 5  # reserved name: cannot be shadowed by a node

        def boom():
            raise RuntimeError("synthetic derive failure")

        ctx._derive_graph = boom  # next derivation fails: controller-internal error
        with caplog.at_level(logging.ERROR):
            # the caller sees the original error, or ControllerFailedError if a later
            # turn of the same update was already refused
            with pytest.raises((RuntimeError, ControllerFailedError), match="synthetic derive failure"):
                ctx.b = 2
        failure = ctx.controller_failure
        assert isinstance(failure, ControllerFailedError)
        assert "_derive_all" in str(failure) and "synthetic derive failure" in str(failure)
        records = [r for r in caplog.records if "poisoned" in r.getMessage()]
        assert records and records[0].levelno == logging.ERROR and records[0].exc_info
        with pytest.raises(ControllerFailedError):
            ctx.c = 3  # later ingress is refused
    finally:
        try:
            ctx.close()
        except Exception:
            pass
