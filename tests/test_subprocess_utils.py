from app.services.subprocess_utils import tool_failure_message


def test_sigterm_message_is_valid_for_initial_pipeline_steps():
    message = tool_failure_message("MAFFT", -15)

    assert "interrupted before it completed" in message
    assert "Retry the job" in message
    assert "previous tree" not in message.lower()
    assert "recompute" not in message.lower()
