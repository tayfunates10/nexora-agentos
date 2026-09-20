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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="image name as the overlay declares it")
    parser.add_argument("--digest", required=True, help="sha256:... digest published by CI")
    parser.add_argument("--new-name", help="registry path to publish under, when it changes")
    parser.add_argument("--overlay", type=Path, default=OVERLAY)
    arguments = parser.parse_args()

    try:
        document = arguments.overlay.read_text()
        promoted = pin_digest(document, arguments.image, arguments.digest, arguments.new_name)
    except (OSError, PromotionError) as error:
        print(f"Promotion refused: {error}", file=sys.stderr)
        return 1

    arguments.overlay.write_text(promoted)
    print(f"Pinned {arguments.image} to {arguments.digest} in {arguments.overlay}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
