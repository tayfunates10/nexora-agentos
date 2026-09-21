import unittest

from deploy_staging import DeployError, deploy, validate_image_reference

OLD_API = "ghcr.io/acme/nexora-api@sha256:" + "1" * 64
OLD_WEB = "ghcr.io/acme/nexora-web@sha256:" + "3" * 64
NEW_API = "ghcr.io/acme/nexora-api@sha256:" + "a" * 64
NEW_WEB = "ghcr.io/acme/nexora-web@sha256:" + "b" * 64
OVERLAY = "infra/k8s/overlays/staging"
MIGRATE = "infra/k8s/overlays/staging/migrate"


def rendered(images):
    return "\n".join(f"        image: {image}" for image in images)


class FakeCluster:
    """A staging namespace that answers the exact kubectl calls the deploy makes."""

    def __init__(self, *, deployed=True, pinned=None, migrate_pinned=None):
        self.images = (
            {"nexora-api": OLD_API, "nexora-worker": OLD_API, "nexora-web": OLD_WEB}
            if deployed
            else {}
        )
        self.commands = []
        self.pinned = pinned if pinned is not None else [NEW_API, NEW_WEB]
        self.migrate_pinned = (
            migrate_pinned if migrate_pinned is not None else [NEW_API]
        )
        self.fail_rollout = False
        self.fail_migration = False
        self.applied = False
        self.migrated = False

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[:2] == ["kubectl", "kustomize"]:
            return rendered(
                self.migrate_pinned if command[2] == MIGRATE else self.pinned
            )
        if command[:3] == ["kubectl", "auth", "can-i"]:
            return "yes"
        if command[:3] == ["kubectl", "get", "deployment"]:
            return self.images.get(command[3], "")
        if command[:3] == ["kubectl", "delete", "job"]:
            return "deleted"
        if command[:2] == ["kubectl", "apply"]:
            if "--dry-run=server" in command:
                return "deployment.apps/nexora-api"
            if command[3] == MIGRATE:
                self.migrated = True
                return "job.batch/nexora-migrate created"
            self.applied = True
            for deployment in ("nexora-api", "nexora-worker", "nexora-web"):
                self.images[deployment] = (
                    NEW_WEB if deployment == "nexora-web" else NEW_API
                )
            return "configured"
        if command[:2] == ["kubectl", "wait"]:
            if self.fail_migration:
                raise DeployError("timed out waiting for condition")
            return "job.batch/nexora-migrate condition met"
        if command[:3] == ["kubectl", "rollout", "status"]:
            if self.fail_rollout:
                raise DeployError("deployment exceeded its progress deadline")
            return "successfully rolled out"
        if command[:3] == ["kubectl", "set", "image"]:
            # Restoring a release that was running a moment ago rolls out normally.
            self.fail_rollout = False
            deployment = command[3].split("/", 1)[1]
            self.images[deployment] = command[4].split("=", 1)[1]
            return f"deployment.apps/{deployment}"
        raise AssertionError(command)

    def verbs(self):
        return [" ".join(command[1:3]) for command in self.commands]


class StagingDeployTests(unittest.TestCase):
    def test_rejects_a_tag_and_accepts_a_digest(self):
        self.assertEqual(validate_image_reference(NEW_API), NEW_API)
        with self.assertRaises(DeployError):
            validate_image_reference("ghcr.io/acme/nexora-api:latest")

    def test_deploys_and_proves_the_requested_release_is_running(self):
        cluster = FakeCluster()
        running = deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertEqual(running.api, NEW_API)
        self.assertEqual(running.worker, NEW_API)
        self.assertEqual(running.web, NEW_WEB)

    def test_migration_runs_before_any_workload_is_applied(self):
        cluster = FakeCluster()
        deploy(NEW_API, NEW_WEB, runner=cluster)
        joined = [" ".join(command) for command in cluster.commands]
        waited = next(index for index, text in enumerate(joined) if "wait job/" in text)
        applied = next(
            index
            for index, text in enumerate(joined)
            if text.endswith("apply -k " + OVERLAY)
        )
        self.assertLess(waited, applied)
        self.assertTrue(cluster.migrated)

    def test_a_failed_migration_stops_before_the_rollout(self):
        cluster = FakeCluster()
        cluster.fail_migration = True
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn("no workload was rolled out", str(caught.exception))
        self.assertFalse(cluster.applied)
        self.assertEqual(cluster.images["nexora-api"], OLD_API)

    def test_a_failed_rollout_restores_the_previous_release(self):
        cluster = FakeCluster()
        cluster.fail_rollout = True
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn("previous release was restored", str(caught.exception))
        self.assertEqual(cluster.images["nexora-api"], OLD_API)
        self.assertEqual(cluster.images["nexora-web"], OLD_WEB)

    def test_a_first_deployment_has_nothing_to_restore_and_says_so(self):
        cluster = FakeCluster(deployed=False)
        cluster.fail_rollout = True
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn("no previous release to restore", str(caught.exception))

    def test_refuses_a_release_the_overlay_does_not_pin(self):
        cluster = FakeCluster(pinned=[OLD_API, OLD_WEB])
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn("promote the overlay", str(caught.exception))
        self.assertFalse(cluster.applied)

    def test_refuses_a_migration_job_pinned_to_another_build(self):
        cluster = FakeCluster(migrate_pinned=[OLD_API])
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn(MIGRATE, str(caught.exception))
        self.assertFalse(cluster.applied)

    def test_skipping_migrations_never_touches_the_job(self):
        cluster = FakeCluster()
        deploy(NEW_API, NEW_WEB, migrate=False, runner=cluster)
        self.assertNotIn("delete job", cluster.verbs())
        self.assertTrue(cluster.applied)

    def test_refuses_an_identity_without_the_permissions_it_needs(self):
        class Denied(FakeCluster):
            def __call__(self, command):
                if list(command)[:3] == ["kubectl", "auth", "can-i"]:
                    self.commands.append(list(command))
                    return "no"
                return super().__call__(command)

        cluster = Denied()
        with self.assertRaises(DeployError) as caught:
            deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertIn("cannot get deployments.apps", str(caught.exception))
        self.assertFalse(cluster.applied)

    def test_redeploying_the_same_release_is_allowed(self):
        # Unlike the rollback drill, a deploy that changes nothing is a valid no-op:
        # it reconciles configuration and proves what is running.
        cluster = FakeCluster()
        cluster.images = {
            "nexora-api": NEW_API,
            "nexora-worker": NEW_API,
            "nexora-web": NEW_WEB,
        }
        running = deploy(NEW_API, NEW_WEB, runner=cluster)
        self.assertEqual(running.web, NEW_WEB)


if __name__ == "__main__":
    unittest.main()
