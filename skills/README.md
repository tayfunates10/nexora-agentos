# Canonical Agent Skills

This directory is the single source of truth for Nexora AgentOS engineering skills.

Edit skill files only here:

```
skills/<skill-name>/SKILL.md
```

Then run:

```bash
python scripts/sync_skills.py
```

The sync script copies the canonical skills to supported agent discovery locations:

- `.claude/skills` — Claude Code
- `.agents/skills` — interoperable agent skills / Codex / Cursor / Gemini-compatible discovery
- `.opencode/skills` — OpenCode
- `.copilot/skills` — GitHub Copilot-compatible skill mirror

CI runs `python scripts/sync_skills.py --check` and fails if any generated copy drifts from the canonical source.
