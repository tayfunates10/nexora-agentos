# Nexora AgentOS

Enterprise-grade platform for building, orchestrating, securing, evaluating, and observing autonomous AI agents.

## Multi-agent engineering skills

Nexora keeps one canonical skill source under `skills/`. Generated mirrors make the same engineering guidance available to Claude Code, Codex-compatible agents, Cursor, Gemini-compatible discovery, OpenCode, and Copilot-oriented workflows.

After changing a canonical skill, run:

```bash
python scripts/sync_skills.py
python scripts/sync_skills.py --check
```

CI rejects skill drift so every supported coding agent receives the same project rules.
