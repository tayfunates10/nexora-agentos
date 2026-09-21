import os
import unittest
from unittest.mock import patch

from staging_smoke import Config, SmokeError, metric_sum, validate_events


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

    def test_retrieval_requires_source_and_output_sentinel(self):
        env = {
            "NEXORA_STAGING_API_URL": "https://staging.example",
            "NEXORA_STAGING_ACCESS_TOKEN": "test-token",
            "NEXORA_STAGING_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "NEXORA_STAGING_AGENT_ID": "22222222-2222-4222-8222-222222222222",
            "NEXORA_STAGING_REQUIRE_RETRIEVAL": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SmokeError):
                Config.from_env()

        env["NEXORA_STAGING_EXPECT_SOURCE_KEY"] = "staging-smoke"
        env["NEXORA_STAGING_EXPECT_TEXT"] = "NEXORA-STAGING-SENTINEL"
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertTrue(config.require_retrieval)
        self.assertFalse(config.require_observability)
        self.assertIsNone(config.worker_admin_url)

    def test_config_accepts_bounded_https_fixture(self):
        env = {
            "NEXORA_STAGING_API_URL": "https://staging.example",
            "NEXORA_STAGING_ACCESS_TOKEN": "test-token",
            "NEXORA_STAGING_WORKSPACE_ID": "11111111-1111-4111-8111-111111111111",
            "NEXORA_STAGING_AGENT_ID": "22222222-2222-4222-8222-222222222222",
            "NEXORA_STAGING_REQUIRE_RETRIEVAL": "false",
            "NEXORA_STAGING_TIMEOUT_SECONDS": "90",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.api_url, "https://staging.example")
        self.assertEqual(config.timeout_seconds, 90.0)
        self.assertFalse(config.require_retrieval)


if __name__ == "__main__":
    unittest.main()
