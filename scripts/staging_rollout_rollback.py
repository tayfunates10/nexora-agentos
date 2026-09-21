#!/usr/bin/env python3
"""Exercise a staging Kubernetes rollout and prove the documented rollback path.

The drill intentionally leaves staging on the release that was present before the test.
It changes workload images only; schema migrations must already have been applied by the
operator so the rollback smoke proves that the previous code tolerates the current schema.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
DEPLOYMENTS = (
    ("nexora-api", "api", "api"),
    ("nexora-worker", "worker", "worker"),
    ("nexora-web", "web", "web"),
)


class DrillError(RuntimeError):
    """A rollout/rollback drill that cannot prove a safe result."""


@dataclass(frozen=True)
class Images:
    api: str
    worker: str
    web: str


CommandRunner = Callable[[Sequence[str]], str]
SmokeRunner = Callable[[], None]


def validate_image_reference(image: str, *, label: str = "candidate") -> str:
    if not DIGEST_IMAGE.fullmatch(image):
        raise DrillError(
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
        raise DrillError(f"required executable not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise DrillError(f"command failed ({' '.join(command)}){suffix}") from exc
    return completed.stdout.strip()


def _smoke_subprocess(script: Path) -> None:
    try:
        subprocess.run([sys.executable, str(script)], check=True)
    except subprocess.CalledProcessError as exc:
        raise DrillError("deployed staging smoke failed") from exc


def _get_image(
    runner: CommandRunner,
    namespace: str,
    deployment: str,
    container: str,
) -> str:
    output = runner(
        [
            "kubectl",
            "get",
            "deployment",
            deployment,
            "-n",
            namespace,
            "-o",
            (
                "jsonpath={.spec.template.spec.containers[?"
                f"(@.name=='{container}')].image}}"
            ),
        ]
    ).strip()
    if not output:
        raise DrillError(f"{deployment}/{container} has no image")
    return output


def snapshot_images(runner: CommandRunner, namespace: str) -> Images:
    values: dict[str, str] = {}
    for deployment, container, key in DEPLOYMENTS:
        values[key] = _get_image(runner, namespace, deployment, container)
    return Images(api=values["api"], worker=values["worker"], web=values["web"])


def _candidate_for(key: str, api_image: str, web_image: str) -> str:
    return web_image if key == "web" else api_image


def _previous_for(previous: Images, key: str) -> str:
    return getattr(previous, key)


def _set_image(
    runner: CommandRunner,
    namespace: str,
    deployment: str,
    container: str,
    image: str,
    *,
    server_dry_run: bool,
) -> None:
    command = [
        "kubectl",
        "set",
        "image",
        f"deployment/{deployment}",
        f"{container}={image}",
        "-n",
        namespace,
    ]
    if server_dry_run:
        command.extend(["--dry-run=server", "-o", "name"])
    runner(command)


def _wait(runner: CommandRunner, namespace: str, deployment: str) -> None:
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


def _preflight(
    runner: CommandRunner,
    namespace: str,
    api_image: str,
    web_image: str,
) -> None:
    permissions = (
        ("get", "deployments.apps"),
        ("watch", "deployments.apps"),
        ("patch", "deployments.apps"),
        ("get", "replicasets.apps"),
        ("list", "replicasets.apps"),
    )
    for verb, resource in permissions:
        allowed = runner(
            [
                "kubectl",
                "auth",
                "can-i",
                verb,
                resource,
                "-n",
                namespace,
            ]
        ).strip().lower()
        if allowed != "yes":
            raise DrillError(
                f"staging identity cannot {verb} {resource} in namespace {namespace}"
            )
    for deployment, container, key in DEPLOYMENTS:
        _set_image(
            runner,
            namespace,
            deployment,
            container,
            _candidate_for(key, api_image, web_image),
            server_dry_run=True,
        )


def _apply_candidate(
    runner: CommandRunner,
    namespace: str,
    api_image: str,
    web_image: str,
) -> None:
    for deployment, container, key in DEPLOYMENTS:
        _set_image(
            runner,
            namespace,
            deployment,
            container,
            _candidate_for(key, api_image, web_image),
            server_dry_run=False,
        )
    for deployment, _, _ in DEPLOYMENTS:
        _wait(runner, namespace, deployment)


def _rollback(runner: CommandRunner, namespace: str) -> None:
    for deployment, _, _ in DEPLOYMENTS:
        runner(
            [
                "kubectl",
                "rollout",
                "undo",
                f"deployment/{deployment}",
                "-n",
                namespace,
            ]
        )
    for deployment, _, _ in DEPLOYMENTS:
        _wait(runner, namespace, deployment)


def _restore_exact(
    runner: CommandRunner,
    namespace: str,
    previous: Images,
) -> None:
    errors: list[str] = []
    for deployment, container, key in DEPLOYMENTS:
        try:
            _set_image(
                runner,
                namespace,
                deployment,
                container,
                _previous_for(previous, key),
                server_dry_run=False,
            )
        except DrillError as exc:
            errors.append(str(exc))
    for deployment, _, _ in DEPLOYMENTS:
        try:
            _wait(runner, namespace, deployment)
        except DrillError as exc:
            errors.append(str(exc))
    if errors:
        raise DrillError("emergency restore was incomplete: " + "; ".join(errors))


def assert_restored(
    runner: CommandRunner,
    namespace: str,
    previous: Images,
) -> None:
    mismatches = []
    for deployment, container, key in DEPLOYMENTS:
        actual = _get_image(runner, namespace, deployment, container)
        expected = _previous_for(previous, key)
        if actual != expected:
            mismatches.append(f"{deployment}: expected {expected}, got {actual}")
    if mismatches:
        raise DrillError("rollback did not restore exact previous images: " + "; ".join(mismatches))


def run_drill(
    api_image: str,
    web_image: str,
    *,
    namespace: str = "nexora",
    runner: CommandRunner = _subprocess_runner,
    smoke: SmokeRunner,
) -> Images:
    api_image = validate_image_reference(api_image)
    web_image = validate_image_reference(web_image)
    previous = snapshot_images(runner, namespace)
    for current in (previous.api, previous.worker, previous.web):
        validate_image_reference(current, label="currently deployed")
    if previous.api == api_image and previous.worker == api_image and previous.web == web_image:
        raise DrillError("candidate images are already deployed; rollback would prove nothing")

    _preflight(runner, namespace, api_image, web_image)
    mutated = False
    try:
        mutated = True
        _apply_candidate(runner, namespace, api_image, web_image)
        smoke()
        _rollback(runner, namespace)
        assert_restored(runner, namespace, previous)
        smoke()
    except Exception as exc:
        if mutated:
            try:
                _restore_exact(runner, namespace, previous)
            except Exception as restore_exc:
                raise DrillError(
                    f"drill failed ({exc}); emergency restore also failed ({restore_exc})"
                ) from exc
        if isinstance(exc, DrillError):
            raise
        raise DrillError(f"drill failed: {exc}") from exc
    return previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--namespace", default="nexora")
    parser.add_argument(
        "--smoke-script",
        type=Path,
        default=Path("scripts/staging_smoke.py"),
    )
    args = parser.parse_args()

    try:
        previous = run_drill(
            args.api_image,
            args.web_image,
            namespace=args.namespace,
            smoke=lambda: _smoke_subprocess(args.smoke_script),
        )
    except DrillError as exc:
        print(f"STAGING ROLLBACK DRILL FAILED: {exc}", file=sys.stderr)
        return 1

    print("STAGING ROLLBACK DRILL PASSED")
    print("Candidate rollout and exact-image rollback both passed the deployed smoke.")
    print(f"Restored api: {previous.api}")
    print(f"Restored worker: {previous.worker}")
    print(f"Restored web: {previous.web}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
