---
name: security-threat-modeling
description: Use when reviewing security, designing trust boundaries, threat models, STRIDE-style analysis, secrets handling, sandboxing, SSRF, injection, or privileged agent actions.
---

# Security Threat Modeling

Threat-model features before shipping privileged capabilities.

## Identify
Assets, actors, entry points, trust boundaries, credentials, external dependencies and irreversible actions.

## Minimum threats
Prompt injection, confused deputy, cross-tenant data access, SSRF, insecure tool schemas, secret leakage, replay, privilege escalation, webhook forgery, unsafe file handling and denial of service.

## Controls
Least privilege, allowlists, schema validation, egress restrictions, short-lived credentials, rate limits, audit logs, approval gates and secure defaults.

## Requirement
Security controls must be enforced outside the LLM. A system prompt is not an authorization mechanism.
