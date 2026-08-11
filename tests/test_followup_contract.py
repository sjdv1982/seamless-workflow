from seamless import BoundStateError
from seamless_workflow.errors import (
    ReadOnlyEndpointError,
    StaleWorkflowHandleError,
    ValueUnavailableError,
)


def test_followup_error_contract_is_exported():
    assert issubclass(BoundStateError, AttributeError)
    assert issubclass(ReadOnlyEndpointError, Exception)
    assert issubclass(StaleWorkflowHandleError, Exception)
    assert issubclass(ValueUnavailableError, Exception)
