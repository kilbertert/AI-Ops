from aiops_diagnostics.gateway_runtime import _internal_error_message


def test_internal_error_message_is_redacted_and_bounded() -> None:
    """The message stored in the RUN RECORD is redacted and bounded.

    It is no longer the message the API returns — the response carries a
    bounded coded sentence (`diagnosis_error_message`) — so this check is
    about what an engineer reads in the record, not what a user reads.
    """
    error = RuntimeError("provider token=sk-1234567890abcdef password=hunter2 " + "x" * 1500)
    message = _internal_error_message(error, "ORDER-1")
    assert "sk-1234567890abcdef" not in message
    assert "hunter2" not in message
    assert len(message) == 1000
