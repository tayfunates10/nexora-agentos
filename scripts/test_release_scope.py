import unittest

from release_scope import units


class ReleaseScope(unittest.TestCase):
    def test_an_agent_change_releases_only_that_agent(self):
        scope = units(["agents/social-media/manifest.json"])
        self.assertEqual(scope["agents"], ["social-media"])
        # The point of the separation: no image is rebuilt, so the console is untouched.
        self.assertEqual(scope["images"], [])
        self.assertEqual(scope["connectors"], [])

    def test_two_agents_changing_release_both_and_nothing_else(self):
        scope = units([
            "agents/social-media/manifest.json",
            "agents/seo/manifest.json",
        ])
        self.assertEqual(scope["agents"], ["seo", "social-media"])
        self.assertEqual(scope["images"], [])

    def test_a_console_change_never_republishes_an_agent(self):
        scope = units(["apps/web/app/workspaces/[id]/catalog/page.tsx"])
        self.assertEqual(scope["images"], ["nexora-web"])
        self.assertEqual(scope["agents"], [])
        self.assertEqual(scope["connectors"], [])

    def test_an_api_change_releases_only_the_api_image(self):
        scope = units(["apps/api/src/nexora_api/agent_catalog_repository.py"])
        self.assertEqual(scope["images"], ["nexora-api"])
        self.assertEqual(scope["agents"], [])

    def test_a_connector_change_releases_only_that_connector(self):
        scope = units(["connectors/instagram/connector.json"])
        self.assertEqual(scope["connectors"], ["instagram"])
        self.assertEqual(scope["images"], [])
        self.assertEqual(scope["agents"], [])

    def test_a_manifest_contract_change_revalidates_every_package(self):
        scope = units(["apps/api/src/nexora_api/agent_manifest.py"])
        self.assertEqual(scope["agents"], ["*"])
        self.assertEqual(scope["connectors"], ["*"])
        self.assertEqual(scope["images"], ["nexora-api"])

    def test_shared_infrastructure_rebuilds_both_images(self):
        for path in ("infra/k8s/base/api.yaml", "compose.yaml", "package-lock.json"):
            with self.subTest(path=path):
                self.assertEqual(units([path])["images"], ["nexora-api", "nexora-web"])

    def test_a_mixed_change_releases_each_affected_unit_once(self):
        scope = units([
            "apps/web/app/page.tsx",
            "apps/api/src/nexora_api/main.py",
            "agents/reporting/manifest.json",
            "agents/reporting/README.md",
            "connectors/github/connector.json",
            "docs/architecture/0044-standard-agents.md",
        ])
        self.assertEqual(scope["images"], ["nexora-api", "nexora-web"])
        self.assertEqual(scope["agents"], ["reporting"])
        self.assertEqual(scope["connectors"], ["github"])

    def test_documentation_alone_releases_nothing(self):
        scope = units(["README.md", "docs/architecture/0001-foundation.md", "skills/README.md"])
        self.assertEqual(scope, {"images": [], "agents": [], "connectors": []})

    def test_a_loose_file_in_a_package_directory_is_not_a_package(self):
        # agents/README.md describes the directory; it is not an agent named README.md.
        self.assertEqual(units(["agents/README.md"])["agents"], [])
        self.assertEqual(units(["connectors/README.md"])["connectors"], [])

    def test_paths_are_normalised_before_they_are_matched(self):
        self.assertEqual(units(["./agents/seo/manifest.json"])["agents"], ["seo"])
        self.assertEqual(units(["agents\\seo\\manifest.json"])["agents"], ["seo"])
        self.assertEqual(units([""]), {"images": [], "agents": [], "connectors": []})


if __name__ == "__main__":
    unittest.main()
