#!/usr/bin/env python3
"""Synchronize canonical Nexora AgentOS skills to agent-specific discovery paths."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills"
TARGETS = (
    ROOT / ".claude" / "skills",
    ROOT / ".agents" / "skills",
    ROOT / ".opencode" / "skills",
    ROOT / ".copilot" / "skills",
)


def canonical_skills() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for skill_md in sorted(SOURCE.glob("*/SKILL.md")):
        result[skill_md.parent.name] = skill_md
    return result


def target_skills(target: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not target.exists():
        return result
    for skill_md in sorted(target.glob("*/SKILL.md")):
        result[skill_md.parent.name] = skill_md
    return result


def check() -> int:
    source = canonical_skills()
    errors: list[str] = []

    if not source:
        errors.append("No canonical skills found under skills/*/SKILL.md")

    for target in TARGETS:
        current = target_skills(target)
        missing = sorted(set(source) - set(current))
        extra = sorted(set(current) - set(source))

        for name in missing:
            errors.append(f"{target.relative_to(ROOT)}: missing {name}")
        for name in extra:
            errors.append(f"{target.relative_to(ROOT)}: stale extra skill {name}")

        for name in sorted(set(source) & set(current)):
            if source[name].read_bytes() != current[name].read_bytes():
                errors.append(f"{target.relative_to(ROOT)}: drift in {name}/SKILL.md")

    if errors:
        print("Skill synchronization check failed:")
        for error in errors:
            print(f"  - {error}")
        print("\nRun: python scripts/sync_skills.py")
        return 1

    print(f"Skill synchronization OK: {len(source)} canonical skills across {len(TARGETS)} targets.")
    return 0


def sync() -> int:
    source = canonical_skills()
    if not source:
        print("No canonical skills found under skills/*/SKILL.md", file=sys.stderr)
        return 1

    for target in TARGETS:
        target.mkdir(parents=True, exist_ok=True)
        current = target_skills(target)

        for name in sorted(set(current) - set(source)):
            shutil.rmtree(current[name].parent)

        for name, src in source.items():
            dst_dir = target / name
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / "SKILL.md")

    print(f"Synchronized {len(source)} skills across {len(TARGETS)} targets.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Check mirrors without modifying files.")
    args = parser.parse_args()
    return check() if args.check else sync()


if __name__ == "__main__":
    raise SystemExit(main())
