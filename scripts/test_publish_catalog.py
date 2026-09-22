import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_catalog
from publish_catalog import (
    PublishError,
    agent_packages,
    connector_packages,
    publish_agent,
    publish_connector,
    validate,
)


def agent(**overrides) -> dict:
    document = json.loads(
        (publish_catalog.AGENTS / "social-media" / "manifest.json").read_text(encoding="utf-8")
    )
    document.update(overrides)
    return document


def connector(name: str = "instagram", **overrides) -> dict:
    document = json.loads(
        (publish_catalog.CONNECTORS / name / "connector.json").read_text(encoding="utf-8")
    )
    document.update(overrides)
    return document


class Packages(unittest.TestCase):
    def test_every_shipped_package_validates_against_the_published_contract(self):
        self.assertEqual(validate(agent_packages(), connector_packages()), [])

    def test_the_repository_ships_the_agents_the_platform_promises(self):
        slugs = set(agent_packages())
        self.assertLessEqual(
            {
                "social-media", "reporting", "advertising", "seo",
                "customer-support", "sales-crm", "documents", "operations",
            },
            slugs,
        )

    def test_selecting_a_package_publishes_only_that_one(self):
        self.assertEqual(list(agent_packages(["seo"])), ["seo"])
        self.assertEqual(set(agent_packages(["*"])), set(agent_packages()))
        with self.assertRaises(PublishError):
            agent_packages(["an-agent-that-does-not-exist"])

    def test_a_directory_without_a_manifest_is_a_broken_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "halfway").mkdir()
            with mock.patch.object(publish_catalog, "AGENTS", root):
                with self.assertRaises(PublishError):
                    agent_packages()


class Validation(unittest.TestCase):
    def test_a_manifest_in_the_wrong_directory_is_reported(self):
        problems = validate({"seo": agent()}, {})
        self.assertTrue(any("slug is social-media" in problem for problem in problems))

    def test_an_agent_needing_an_unpublished_connector_is_refused(self):
        problems = validate(
            {"social-media": agent(required_integrations=["a-service-nobody-wrote"])}, {}
        )
        # An agent that names a connector nobody ships can never become ready, so the
        # release stops here rather than at a customer's readiness screen.
        self.assertTrue(any("unpublished connectors" in problem for problem in problems))

    def test_an_invalid_manifest_is_reported_rather_than_raised(self):
        problems = validate({"social-media": agent(version="1.0")}, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("agents/social-media", problems[0])

    def test_a_connector_id_must_match_its_directory(self):
        problems = validate({}, {"meta": connector()})
        self.assertTrue(any("id is instagram" in problem for problem in problems))


class Publishing(unittest.TestCase):
    def test_a_new_agent_creates_its_catalog_entry_then_its_version(self):
        calls = []

        def fake(base_url, token, method, path, body):
            calls.append((method, path))
            if method == "GET":
                return 404, {}
            return (201, {}) if path.endswith("/versions") or path.endswith("/agents") else (200, {})

        with mock.patch.object(publish_catalog, "_request", fake):
            result = publish_agent("https://api.test", "token", agent())
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/platform/agents/social-media"),
                ("POST", "/api/v1/platform/agents"),
                ("POST", "/api/v1/platform/agents/social-media/versions"),
            ],
        )
        self.assertIn(agent()["version"], result)

    def test_an_existing_agent_only_publishes_the_new_version(self):
        calls = []

        def fake(base_url, token, method, path, body):
            calls.append((method, path))
            return (200, {}) if method == "GET" else (201, {})

        with mock.patch.object(publish_catalog, "_request", fake):
            publish_agent("https://api.test", "token", agent())
        self.assertEqual([method for method, _ in calls], ["GET", "POST"])

    def test_republishing_a_version_is_a_no_op_not_an_overwrite(self):
        def fake(base_url, token, method, path, body):
            return (200, {}) if method == "GET" else (409, {})

        with mock.patch.object(publish_catalog, "_request", fake):
            result = publish_agent("https://api.test", "token", agent())
        # The platform refuses to change a published version, and the release accepts
        # that answer rather than trying to force it.
        self.assertIn("already published", result)

    def test_a_refused_version_fails_the_release(self):
        def fake(base_url, token, method, path, body):
            return (200, {}) if method == "GET" else (422, {"body": "invalid"})

        with mock.patch.object(publish_catalog, "_request", fake):
            with self.assertRaises(PublishError):
                publish_agent("https://api.test", "token", agent())

    def test_a_connector_is_published_by_its_own_identifier(self):
        seen = {}

        def fake(base_url, token, method, path, body):
            seen["method"], seen["path"] = method, path
            return 200, {}

        with mock.patch.object(publish_catalog, "_request", fake):
            publish_connector("https://api.test", "token", connector())
        self.assertEqual(seen, {"method": "PUT", "path": "/api/v1/platform/integrations/instagram"})

    def test_a_failed_connector_publish_stops_the_release(self):
        with mock.patch.object(publish_catalog, "_request", lambda *_: (403, {})):
            with self.assertRaises(PublishError):
                publish_connector("https://api.test", "token", connector())


if __name__ == "__main__":
    unittest.main()
