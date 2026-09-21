#!/usr/bin/env python3
"""Deploy a published release to the staging Kubernetes environment.

Unlike the rollout/rollback drill, this leaves staging on the deployed release. It is
still not a promotion: the digests it applies are the ones the reviewed staging overlay
already pins, and the caller must name the same ones, so what runs in staging is always
a merged, reviewable state rather than an argument someone typed once.

Migrations run first and gate the rollout: a workload never starts against a schema it
has not migrated. If the rollout itself fails, the previous images are restored, which
is the state the expand-contract migration rule is written for.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
RENDERED_IMAGE = re.compile(r"^\s+image:\s*(\S+)\s*$", re.MULTILINE)
DEPLOYMENTS = (
    ("nexora-api", "api", "api"),
    ("nexora-worker", "worker", "worker"),
    ("nexora-web", "web", "web"),
)
MIGRATION_JOB = "nexora-migrate"


class DeployError(RuntimeError):
    """A staging deployment that cannot be completed or proven."""


@dataclass(frozen=True)
class Images:
    api: str
    worker: str
    web: str


CommandRunner = Callable[[Sequence[str]], str]


def validate_image_reference(image: str, *, label: str = "candidate") -> str:
    if not DIGEST_IMAGE.fullmatch(image):
        raise DeployError(
            f"{label} images must be immutable registry references ending in "
            "@sha256:<64 lowercase hex characters>"
        )
    return image


def _subprocess_runner(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"required executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise DeployError(f"command failed ({' '.join(command)}){suffix}") from exc
    return completed.stdout.strip()


def rendered_images(runner: CommandRunner, directory: str) -> set[str]:
    rendered = runner(["kubectl", "kustomize", directory])
    return {match.group(1) for match in RENDERED_IMAGE.finditer(rendered)}


def assert_overlay_pins(
    runner: CommandRunner,
    directory: str,
    expected: set[str],
) -> None:
    """The overlay in git decides what runs; the caller only confirms it."""
    actual = rendered_images(runner, directory)
    if actual != expected:
        raise DeployError(
            f"{directory} pins {sorted(actual) or ['nothing']} but this deployment was "
            f"asked for {sorted(expected)}; promote the overlay and merge that change first"
        )


def _get_image(
    runner: CommandRunner,
    namespace: str,
    deployment: str,
    container: str,
) -> str | None:
    output = runner(
        [
            "kubectl",
            "get",
            "deployment",
            deployment,
            "-n",
            namespace,
            "--ignore-not-found",
            "-o",
            (
                "jsonpath={.spec.template.spec.containers[?"
                f"(@.name=='{container}')].image}}"
            ),
        ]
    ).strip()
    return output or None


def snapshot_images(runner: CommandRunner, namespace: str) -> Images | None:
    """The images to restore if the rollout fails, or None on a first deployment."""
    values: dict[str, str | None] = {}
    for deployment, container, key in DEPLOYMENTS:
        values[key] = _get_image(runner, namespace, deployment, container)
    if any(value is None for value in values.values()):
        return None
    return Images(api=values["api"], worker=values["worker"], web=values["web"])


def _preflight(runner: CommandRunner, namespace: str, directory: str) -> None:
    permissions = (
        ("get", "deployments.apps"),
        ("patch", "deployments.apps"),
        ("create", "deployments.apps"),
        ("get", "jobs.batch"),
        ("create", "jobs.batch"),
        ("delete", "jobs.batch"),
    )
    for verb, resource in permissions:
        allowed = (
            runner(["kubectl", "auth", "can-i", verb, resource, "-n", namespace])
            .strip()
            .lower()
        )
        if allowed != "yes":
            raise DeployError(
                f"staging identity cannot {verb} {resource} in namespace {namespace}"
            )
    # Server-side dry run: the API server validates admission, schema and quota
    # against this identity before anything is mutated.
    runner(["kubectl", "apply", "-k", directory, "--dry-run=server", "-o", "name"])


def run_migration(
    runner: CommandRunner,
    namespace: str,
    directory: str,
    *,
    timeout_seconds: int,
) -> None:
    # A Job's spec is immutable, so each release deletes and recreates it.
    runner(
        [
            "kubectl",
            "delete",
            "job",
            MIGRATION_JOB,
            "-n",
            namespace,
            "--ignore-not-found",
            "--wait=true",
        ]
    )
    runner(["kubectl", "apply", "-k", directory])
    try:
        runner(
            [
                "kubectl",
                "wait",
                f"job/{MIGRATION_JOB}",
                "-n",
                namespace,
                "--for=condition=complete",
                f"--timeout={timeout_seconds}s",
            ]
        )
    except DeployError as exc:
        raise DeployError(
            f"schema migration did not complete, so no workload was rolled out: {exc}"
        ) from exc


def _apply(runner: CommandRunner, namespace: str, directory: str) -> None:
    runner(["kubectl", "apply", "-k", directory])
    for deployment, _, _ in DEPLOYMENTS:
        runner(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                namespace,
                "--timeout=300s",
            ]
        )


def _restore_exact(runner: CommandRunner, namespace: str, previous: Images) -> None:
    errors: list[str] = []
    for deployment, container, key in DEPLOYMENTS:
        try:
            runner(
                [
                    "kubectl",
                    "set",
                    "image",
                    f"deployment/{deployment}",
                    f"{container}={getattr(previous, key)}",
                    "-n",
                    namespace,
                ]
            )
        except DeployError as exc:
            errors.append(str(exc))
    for deployment, _, _ in DEPLOYMENTS:
        try:
            runner(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/{deployment}",
                    "-n",
                    namespace,
                    "--timeout=300s",
                ]
            )
        except DeployError as exc:
            errors.append(str(exc))
    if errors:
        raise DeployError(
            "restore after a failed rollout was incomplete: " + "; ".join(errors)
        )


def assert_deployed(
    runner: CommandRunner,
    namespace: str,
    api_image: str,
    web_image: str,
) -> Images:
    expected = {"api": api_image, "worker": api_image, "web": web_image}
    running: dict[str, str] = {}
    mismatches = []
    for deployment, container, key in DEPLOYMENTS:
        actual = _get_image(runner, namespace, deployment, container)
        if actual != expected[key]:
            mismatches.append(f"{deployment}: expected {expected[key]}, got {actual}")
        running[key] = actual or ""
    if mismatches:
        raise DeployError(
            "staging is not running the requested release: " + "; ".join(mismatches)
        )
    return Images(api=running["api"], worker=running["worker"], web=running["web"])


def deploy(
    api_image: str,
    web_image: str,
    *,
    namespace: str = "nexora-staging",
    overlay: str = "infra/k8s/overlays/staging",
    migration_overlay: str = "infra/k8s/overlays/staging/migrate",
    migrate: bool = True,
    migration_timeout_seconds: int = 300,
    runner: CommandRunner = _subprocess_runner,
) -> Images:
    api_image = validate_image_reference(api_image)
    web_image = validate_image_reference(web_image)

    assert_overlay_pins(runner, overlay, {api_image, web_image})
    if migrate:
        # The worker and the migration Job run the API image; a migration on a
        # different build than the code it is about to serve is not a release.
        assert_overlay_pins(runner, migration_overlay, {api_image})

    _preflight(runner, namespace, overlay)
    previous = snapshot_images(runner, namespace)

    if migrate:
        run_migration(
            runner,
            namespace,
            migration_overlay,
            timeout_seconds=migration_timeout_seconds,
        )

    try:
        _apply(runner, namespace, overlay)
        return assert_deployed(runner, namespace, api_image, web_image)
    except Exception as exc:
        if previous is None:
            raise DeployError(
                f"first staging deployment failed and there is no previous release to "
                f"restore: {exc}"
            ) from exc
        try:
            _restore_exact(runner, namespace, previous)
        except DeployError as restore_exc:
            raise DeployError(
                f"deployment failed ({exc}); restoring the previous release also failed "
                f"({restore_exc})"
            ) from exc
        raise DeployError(
            f"deployment failed and the previous release was restored: {exc}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--namespace", default="nexora-staging")
    parser.add_argument("--overlay", default="infra/k8s/overlays/staging")
    parser.add_argument(
        "--migration-overlay",
        default="infra/k8s/overlays/staging/migrate",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Roll out without running the migration Job. Only safe when the schema "
        "this release needs is already applied.",
    )
    parser.add_argument("--migration-timeout-seconds", type=int, default=300)
    args = parser.parse_args()

    try:
        running = deploy(
            args.api_image,
            args.web_image,
            namespace=args.namespace,
            overlay=args.overlay,
            migration_overlay=args.migration_overlay,
            migrate=not args.skip_migrations,
            migration_timeout_seconds=args.migration_timeout_seconds,
        )
    except DeployError as exc:
        print(f"STAGING DEPLOY FAILED: {exc}", file=sys.stderr)
        return 1

    print("STAGING DEPLOY PASSED")
    print(f"api: {running.api}")
    print(f"worker: {running.worker}")
    print(f"web: {running.web}")
    print("Run the deployed staging smoke to prove the environment end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
