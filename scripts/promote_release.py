#!/usr/bin/env python3
"""Pin a published image digest into a Kustomize overlay.

Promotion is an explicit, reviewable edit: the digest CI produced is written into the
overlay and merged like any other change. The file is edited in place rather than
re-serialized so its comments and ordering survive, and only a well-formed digest for an
image the overlay already declares is accepted.
"""

import argparse
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_ENTRY = re.compile(r"^(\s*)-\s+name:\s*(\S+)\s*$")
FIELD = re.compile(r"^(\s*)(newName|digest|newTag):\s*(\S+)\s*$")
OVERLAY = Path("infra/k8s/overlays/production/kustomization.yaml")
PRODUCTION_MIGRATE_OVERLAY = Path("infra/k8s/overlays/production-migrate/kustomization.yaml")
STAGING_OVERLAY = Path("infra/k8s/overlays/staging/kustomization.yaml")
STAGING_MIGRATE_OVERLAY = Path("infra/k8s/overlays/staging-migrate/kustomization.yaml")


class PromotionError(ValueError):
    """A promotion that would produce an unusable or unverifiable overlay."""


def pin_digest(document: str, image: str, digest: str, new_name: str | None = None) -> str:
    """Return the overlay with `image` pinned to `digest`, preserving everything else."""
    if not DIGEST.fullmatch(digest):
        raise PromotionError(f"not a sha256 digest: {digest}")

    lines = document.splitlines(keepends=True)
    updated = list(lines)
    inside = False
    changed = False
    for index, line in enumerate(lines):
        entry = IMAGE_ENTRY.match(line)
        if entry:
            inside = entry.group(2) == image
            continue
        if not inside:
            continue
        field = FIELD.match(line)
        if field is None:
            inside = False
            continue
        indent, key, _ = field.groups()
        if key == "digest":
            updated[index] = f"{indent}digest: {digest}\n"
            changed = True
        elif key == "newName" and new_name is not None:
            updated[index] = f"{indent}newName: {new_name}\n"
        elif key == "newTag":
            # A tag alongside a digest makes it ambiguous which artifact ships.
            raise PromotionError(f"{image}: remove newTag before promoting by digest")

    if not changed:
        raise PromotionError(f"{image}: no digest field to promote in this overlay")
    return "".join(updated)


def promotion_targets(
    image: str,
    *,
    environment: str | None,
    overlay: Path | None,
) -> list[Path]:
    if overlay is not None:
        return [overlay]
    if environment in (None, "production"):
        targets = [OVERLAY]
        if image == "nexora/api":
            targets.append(PRODUCTION_MIGRATE_OVERLAY)
        return targets
    if environment == "staging":
        targets = [STAGING_OVERLAY]
        if image == "nexora/api":
            targets.append(STAGING_MIGRATE_OVERLAY)
        return targets
    raise PromotionError(f"unknown environment: {environment}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="image name as the overlay declares it")
    parser.add_argument("--digest", required=True, help="sha256:... digest published by CI")
    parser.add_argument("--new-name", help="registry path to publish under, when it changes")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--overlay", type=Path)
    target.add_argument("--environment", choices=("staging", "production"))
    arguments = parser.parse_args()

    try:
        targets = promotion_targets(
            arguments.image,
            environment=arguments.environment,
            overlay=arguments.overlay,
        )
        # Validate every target before writing any of them. In staging this keeps the
        # API workload and migration Job on the same immutable artifact.
        promoted = []
        for path in targets:
            document = path.read_text()
            promoted.append(
                (
                    path,
                    pin_digest(
                        document,
                        arguments.image,
                        arguments.digest,
                        arguments.new_name,
                    ),
                )
            )
    except (OSError, PromotionError) as error:
        print(f"Promotion refused: {error}", file=sys.stderr)
        return 1

    for path, document in promoted:
        path.write_text(document)
        print(f"Pinned {arguments.image} to {arguments.digest} in {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
