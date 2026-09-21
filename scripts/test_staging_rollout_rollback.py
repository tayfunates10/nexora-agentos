import unittest

from staging_rollout_rollback import DrillError, run_drill, validate_image_reference


OLD_API = "ghcr.io/acme/nexora-api@sha256:" + "1" * 64
OLD_WORKER = "ghcr.io/acme/nexora-api@sha256:" + "2" * 64
OLD_WEB = "ghcr.io/acme/nexora-web@sha256:" + "3" * 64
NEW_API = "ghcr.io/acme/nexora-api@sha256:" + "a" * 64
NEW_WEB = "ghcr.io/acme/nexora-web@sha256:" + "b" * 64


class FakeCluster:
    def __init__(self):
        self.images = {
            "nexora-api": OLD_API,
            "nexora-worker": OLD_WORKER,
            "nexora-web": OLD_WEB,
        }
        self.previous = dict(self.images)
        self.commands = []
        self.undo_override = {}

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[:3] == ["kubectl", "auth", "can-i"]:
            return "yes"
        if command[:3] == ["kubectl", "get", "deployment"]:
            deployment = command[3]
            return self.images[deployment]
        if command[:3] == ["kubectl", "set", "image"]:
            deployment = command[3].split("/", 1)[1]
            assignment = command[4]
            image = assignment.split("=", 1)[1]
            if "--dry-run=server" not in command:
                self.previous[deployment] = self.images[deployment]
                self.images[deployment] = image
            return f"deployment.apps/{deployment}"
        if command[:3] == ["kubectl", "rollout", "undo"]:
            deployment = command[3].split("/", 1)[1]
            self.images[deployment] = self.undo_override.get(
                deployment, self.previous[deployment]
            )
            return f"deployment.apps/{deployment} rolled back"
        if command[:3] == ["kubectl", "rollout", "status"]:
            return "successfully rolled out"
        raise AssertionError(command)


class RollbackDrillTests(unittest.TestCase):
    def test_rejects_tags_and_accepts_digest(self):
        self.assertEqual(validate_image_reference(NEW_API), NEW_API)
        with self.assertRaises(DrillError):
            validate_image_reference("ghcr.io/acme/nexora-api:latest")

    def test_candidate_and_rollback_are_both_smoked(self):
        cluster = FakeCluster()
        snapshots = []

        def smoke():
            snapshots.append(dict(cluster.images))

        previous = run_drill(NEW_API, NEW_WEB, runner=cluster, smoke=smoke)

        self.assertEqual(previous.api, OLD_API)
        self.assertEqual(previous.worker, OLD_WORKER)
        self.assertEqual(previous.web, OLD_WEB)
        self.assertEqual(
            snapshots[0],
            {
                "nexora-api": NEW_API,
                "nexora-worker": NEW_API,
                "nexora-web": NEW_WEB,
            },
        )
        self.assertEqual(
            snapshots[1],
            {
                "nexora-api": OLD_API,
                "nexora-worker": OLD_WORKER,
                "nexora-web": OLD_WEB,
            },
        )
        self.assertEqual(cluster.images, snapshots[1])
        self.assertEqual(
            len([c for c in cluster.commands if "--dry-run=server" in c]),
            3,
        )

    def test_failed_candidate_smoke_restores_exact_images(self):
        cluster = FakeCluster()

        def smoke():
            raise DrillError("candidate unhealthy")

        with self.assertRaisesRegex(DrillError, "candidate unhealthy"):
            run_drill(NEW_API, NEW_WEB, runner=cluster, smoke=smoke)

        self.assertEqual(
            cluster.images,
            {
                "nexora-api": OLD_API,
                "nexora-worker": OLD_WORKER,
                "nexora-web": OLD_WEB,
            },
        )

    def test_wrong_rollout_undo_is_detected_and_emergency_restored(self):
        cluster = FakeCluster()
        cluster.undo_override["nexora-worker"] = NEW_API
        smoke_calls = 0

        def smoke():
            nonlocal smoke_calls
            smoke_calls += 1

        with self.assertRaisesRegex(DrillError, "rollback did not restore exact"):
            run_drill(NEW_API, NEW_WEB, runner=cluster, smoke=smoke)

        self.assertEqual(smoke_calls, 1)
        self.assertEqual(
            cluster.images,
            {
                "nexora-api": OLD_API,
                "nexora-worker": OLD_WORKER,
                "nexora-web": OLD_WEB,
            },
        )


if __name__ == "__main__":
    unittest.main()
