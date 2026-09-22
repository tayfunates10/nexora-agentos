#!/usr/bin/env python3
"""Decide which release units a change affects.

Nexora releases three things on separate clocks: the console, the platform services and
the standard agent packages. A change to one must not republish the others — a Social
Media Agent fix should not redeploy the web application, and a console change should not
reissue agent versions.

This module is the single place that decision is made, so the release workflows stay
declarative and the rule itself is testable without a repository or a network.
"""

import argparse
import json
import subprocess
import sys

# Ordered by specificity: the first pattern that matches a path claims it.
IMAGE_RULES: list[tuple[str, str]] = [
    ("apps/api/", "nexora-api"),
    ("apps/web/", "nexora-web"),
]
# A change to the shared manifest contracts can alter how every package validates, so it
# republishes the catalog as well as the API image.
CATALOG_CONTRACTS = (
    "apps/api/src/nexora_api/agent_manifest.py",
    "apps/api/src/nexora_api/integration_manifest.py",
    "scripts/publish_catalog.py",
)
# Everything the running platform is built from. A change here rebuilds both images
# because either could be affected.
SHARED_PREFIXES = ("infra/", "compose.yaml", "package.json", "package-lock.json")
IMAGES = ("nexora-api", "nexora-web")


def units(paths: list[str]) -> dict[str, list[str]]:
    """Group changed paths into the release units that must run.

    Returns the container images to publish, the standard agent slugs whose packages
    changed, and the connector identifiers whose definitions changed.
    """
    images: set[str] = set()
    agents: set[str] = set()
    connectors: set[str] = set()

    for path in paths:
        if not path:
            continue
        normalized = path.replace("\\", "/").lstrip("./")
        if normalized.startswith("agents/"):
            parts = normalized.split("/")
            if len(parts) > 2:
                agents.add(parts[1])
            continue
        if normalized.startswith("connectors/"):
            parts = normalized.split("/")
            if len(parts) > 2:
                connectors.add(parts[1])
            continue
        if normalized in CATALOG_CONTRACTS:
            # The contract every package is validated against moved: revalidate and
            # republish all of them alongside the image that carries the contract.
            agents.add("*")
            connectors.add("*")
        if normalized.startswith(SHARED_PREFIXES):
            images.update(IMAGES)
            continue
        for prefix, image in IMAGE_RULES:
            if normalized.startswith(prefix):
                images.add(image)
                break

    return {
        "images": sorted(images),
        "agents": sorted(agents),
        "connectors": sorted(connectors),
    }


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="git ref the change is measured from")
    parser.add_argument("--head", default="HEAD", help="git ref holding the change")
    parser.add_argument(
        "--all",
        action="store_true",
        help="ignore the diff and release everything, for a manual full release",
    )
    arguments = parser.parse_args()

    if arguments.all or not arguments.base:
        scope = {"images": list(IMAGES), "agents": ["*"], "connectors": ["*"]}
    else:
        try:
            scope = units(changed_paths(arguments.base, arguments.head))
        except subprocess.CalledProcessError:
            # A shallow clone that cannot reach the base is not a reason to skip a
            # release; it is a reason to publish everything.
            print(f"Cannot diff {arguments.base}..{arguments.head}", file=sys.stderr)
            scope = {"images": list(IMAGES), "agents": ["*"], "connectors": ["*"]}

    print(json.dumps(scope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
