from nexora_api.observability import (
    AGENT_RUN_RELIABILITY_SLO,
    API_AVAILABILITY_SLO,
    OperationTimer,
    safe_attributes,
)


def test_safe_attributes_excludes_sensitive_and_unbounded_fields():
    attrs = safe_attributes(
        {
            "request_id": "req-1",
            "trace_id": "trace-1",
            "workspace_id": "ws-1",
            "prompt": "secret prompt",
            "authorization": "Bearer secret",
            "tool_arguments": '{"token":"secret"}',
        }
    )

    assert attrs == {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "workspace_id": "ws-1",
    }


def test_user_facing_slos_are_defined_before_dashboards():
    assert API_AVAILABILITY_SLO.objective == 0.999
    assert AGENT_RUN_RELIABILITY_SLO.objective == 0.99
    assert API_AVAILABILITY_SLO.window_days == AGENT_RUN_RELIABILITY_SLO.window_days == 30


def test_operation_timer_is_non_negative():
    assert OperationTimer().elapsed_seconds() >= 0
