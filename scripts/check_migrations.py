#!/usr/bin/env python3
"""Fail a change that rewrites or removes an already-released migration.

The runtime refuses to apply a migration whose checksum no longer matches the one
recorded in the database, so an edited migration is discovered at deploy time against a
live database. This check moves that discovery to the pull request, where fixing it is
still cheap. New migrations are always allowed; changing history never is.
"""

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS = Path("apps/api/src/nexora_api/migrations")
NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def released(ref: str, directory: Path = MIGRATIONS) -> dict[str, str]:
    """Migration checksums as of a git ref, keyed by file name."""
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{ref}:{directory.as_posix()}"],
        capture_output=True,
        text=True,
        check=True,
    )
    checksums = {}
    for name in listing.stdout.split():
        if not name.endswith(".sql"):
            continue
        content = subprocess.run(
            ["git", "show", f"{ref}:{(directory / name).as_posix()}"],
            capture_output=True,
            check=True,
        )
        checksums[name] = digest(content.stdout)
    return checksums


def current(directory: Path = MIGRATIONS) -> dict[str, str]:
    return {path.name: digest(path.read_bytes()) for path in sorted(directory.glob("*.sql"))}


def violations(base: dict[str, str], head: dict[str, str]) -> list[str]:
    """Every way a change can break a database that already ran these migrations."""
    problems = []
    for name, checksum in sorted(base.items()):
        if name not in head:
            problems.append(f"{name}: released migration was deleted")
        elif head[name] != checksum:
            problems.append(f"{name}: released migration was modified")

    for name in sorted(head):
        if not NAME.fullmatch(name):
            problems.append(f"{name}: expected NNN_lower_snake_case.sql")

    prefixes: dict[str, list[str]] = {}
    for name in sorted(head):
        match = NAME.fullmatch(name)
        if match:
            prefixes.setdefault(match.group(1), []).append(name)
    for prefix, names in sorted(prefixes.items()):
        if len(names) > 1:
            # Two branches claimed the same number; order becomes incidental.
            problems.append(f"{prefix}: duplicate migration number across {', '.join(names)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="git ref holding released state")
    arguments = parser.parse_args()

    try:
        base = released(arguments.base)
    except subprocess.CalledProcessError:
        print(f"Cannot read migrations at {arguments.base}", file=sys.stderr)
        return 2

    problems = violations(base, current())
    if problems:
        print("Migration history is not append-only:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"Migrations are append-only against {arguments.base}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
