import tempfile
import unittest
from pathlib import Path

from promote_release import (
    OVERLAY,
    STAGING_MIGRATE_OVERLAY,
    STAGING_OVERLAY,
    PromotionError,
    pin_digest,
    promotion_targets,
)


class PromotionContractTests(unittest.TestCase):
    def test_staging_api_promotion_updates_workload_and_migration(self):
        self.assertEqual(
            promotion_targets("nexora/api", environment="staging", overlay=None),
            [STAGING_OVERLAY, STAGING_MIGRATE_OVERLAY],
        )
        self.assertEqual(
            promotion_targets("nexora/web", environment="staging", overlay=None),
            [STAGING_OVERLAY],
        )

    def test_production_and_explicit_overlay_remain_single_target(self):
        self.assertEqual(
            promotion_targets("nexora/api", environment="production", overlay=None),
            [OVERLAY],
        )
        custom = Path("/tmp/custom-kustomization.yaml")
        self.assertEqual(
            promotion_targets("nexora/api", environment=None, overlay=custom),
            [custom],
        )

    def test_pin_digest_rejects_tags_and_malformed_digests(self):
        document = """images:
  - name: nexora/api
    newName: example/api
    digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""
        digest = "sha256:" + ("b" * 64)
        promoted = pin_digest(document, "nexora/api", digest, "ghcr.io/example/nexora-api")
        self.assertIn(f"digest: {digest}", promoted)
        self.assertIn("newName: ghcr.io/example/nexora-api", promoted)

        with self.assertRaises(PromotionError):
            pin_digest(document, "nexora/api", "latest")

        tagged = document.replace(
            "    digest:",
            "    newTag: latest\n    digest:",
        )
        with self.assertRaises(PromotionError):
            pin_digest(tagged, "nexora/api", digest)

    def test_staging_validation_can_happen_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.yaml"
            second = Path(directory) / "second.yaml"
            valid = """images:
  - name: nexora/api
    newName: example/api
    digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""
            first.write_text(valid)
            second.write_text("images: []\n")
            digest = "sha256:" + ("c" * 64)

            rendered = []
            with self.assertRaises(PromotionError):
                for path in (first, second):
                    rendered.append(
                        (path, pin_digest(path.read_text(), "nexora/api", digest))
                    )

            self.assertEqual(first.read_text(), valid)
            self.assertEqual(second.read_text(), "images: []\n")


if __name__ == "__main__":
    unittest.main()
