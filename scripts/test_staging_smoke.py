import os
import unittest
from unittest.mock import patch

from staging_smoke import (
    Config,
    SmokeError,
    metric_sum,
    validate_events,
    validate_release_evidence,
)


class StagingSmokeContractTests(unittest.TestCase):
    def test_metric_sum_aggregates_labeled_series(self):
        text = """# HELP nexora_model_calls_total calls
# TYPE nexora_model_calls_total counter
nexora_model_calls_total{provider="openai",outcome="success"} 2
nexora_model_calls_total{provider="openai",outcome="error"} 1
other_metric 50
"""
        self.assertEqual(metric_sum(text, "nexora_model_calls_total"), 3.0)

    def test_validate_events_requires_contiguous_terminal_flow(self):
        events = [
            {"event_no": 1, "event_type": "run.queued"},
            {"event_no": 2, "event_type": "run.started"},
            {"event_no": 3, "event_type": "run.succeeded"},
        ]
        self.assertEqual(
            validate_events(events, approval_expected=False),
            ["run.queued", "run.started", "run.succeeded"],
        )
        with self.assertRaises(SmokeError):
            validate_events(
                [
                    {"event_no": 1, "event_type": "run.queued"},
                    {"event_no": 3, "event_type": "run.succeeded"},
                ],
                approval_expected=False,
            )

    def test_validate_events_requires_approval_resume(self):
        valid = [
            {"event_no": 1, "event_type": "run.queued"},
            {"event_no": 2, "event_type": "run.started"},
            {"event_no": 3, "event_type": "tool.approval_requested"},
            {"event_no": 4, "event_type": "run.waiting_for_approval"},
            {"event_no": 5, "event_type": "run.started"},
            {"event_no": 6, "event_type": "run.succeeded"},
        ]
        validate_events(valid, approval_expected=True)
        with self.assertRaises(SmokeError):
            validate_events(valid[:-2] + [{"event_no": 5, "event_type": "run.succeeded"}], approval_expected=True)

    def test_release_evidence_requires_expected_action_and_verified_task(self):
        actions = [
            {
                "action_name": "wordpress.posts.create",
                "status": "succeeded",
                "side_effect": "write",
            },
            {
                "action_name": "nexora.tasks.verify",
                "status": "succeeded",
                "side_effect": "write",
            },
        ]
        tasks = [
            {"kind": "goal", "status": "succeeded", "verification_state": "not_required", "evidence": []},
            {
                "kind": "follow_up",
                "status": "succeeded",
                "verification_state": "verified",
                "action_tool_name": "wordpress.posts.create",
                "evidence": [{"satisfied": True}],
            },
        ]
        summary = validate_release_evidence(
            actions,
            tasks,
            expected_tools=("wordpress.posts.create",),
            require_task_graph=True,
            require_verified_task=True,
            expected_verified_action_tool="wordpress.posts.create",
            forbid_external_mutations=False,
        )
        self.assertEqual(summary["verified_task_count"], 1)

        with self.assertRaises(SmokeError):
            validate_release_evidence(
                actions,
                tasks,
                expected_tools=("instagram.comments.reply",),
                require_task_graph=True,
                require_verified_task=True,
                expected_verified_action_tool="wordpress.posts.create",
                forbid_external_mutations=False,
            )

    def test_read_only_release_evidence_rejects_external_mutations(self):
        actions = [
            {"action_name": "nexora.tasks.create", "status": "succeeded", "side_effect": "write"},
            {"action_name": "google-analytics.reports.read", "status": "succeeded", "side_effect": "read"},
        ]
        tasks = [
            {"kind": "goal", "status": "succeeded", "verification_state": "not_required", "evidence": []},
            {
                "kind": "follow_up",
                "status": "planned",
                "verification_state": "not_required",
                "evidence": [],
            },
        ]
        validate_release_evidence(
            actions,
            tasks,
            expected_tools=("nexora.tasks.create",),
            require_task_graph=True,
            require_verified_task=False,
            expected_verified_action_tool=None,
            forbid_external_mutations=True,
        )
        actions.append(
            {"action_name": "wordpress.posts.create", "status": "succeeded", "side_effect": "write"}
        )
        with self.assertRaises(SmokeError):
            validate_release_evidence(
                actions,
                tasks,
                expected_tools=("nexora.tasks.create",),
                require_task_graph=True,
                require_verified_task=False,
                expected_verified_action_tool=None,
                forbid_external_mutations=True,
            )

    def test_config_requires_https_by_default(self):
        env = {
            "NEXORA_STAGING_API_URL": "http://staging.example",
            "NEXORA_STAGING_ACCESS_TOKEN": "test-token",
            "NEXORA_STAGING_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "NEXORA_STAGING_AGENT_ID": "22222222-2222-4222-8222-222222222222",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SmokeError):
                Config.from_env()

    def test_config_accepts_bounded_https_fixture(self):
        env = {
            "NEXORA_STAGING_API_URL": "https://staging.example",
            "NEXORA_STAGING_ACCESS_TOKEN": "test-token",
            "NEXORA_STAGING_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "NEXORA_STAGING_AGENT_ID": "22222222-2222-4222-8222-222222222222",
            "NEXORA_STAGING_AGENT_KIND": "standard",
            "NEXORA_STAGING_EXPECT_TOOLS": "browser.page.inspect,nexora.tasks.create",
            "NEXORA_STAGING_REQUIRE_TASK_GRAPH": "true",
            "NEXORA_STAGING_REQUIRE_RETRIEVAL": "false",
            "NEXORA_STAGING_TIMEOUT_SECONDS": "90",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.api_url, "https://staging.example")
        self.assertEqual(config.timeout_seconds, 90.0)
        self.assertEqual(config.agent_kind, "standard")
        self.assertEqual(
            config.expected_tools,
            ("browser.page.inspect", "nexora.tasks.create"),
        )
        self.assertTrue(config.require_task_graph)
        self.assertFalse(config.require_retrieval)


if __name__ == "__main__":
    unittest.main()
