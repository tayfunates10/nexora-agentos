---
name: cicd-release
description: Use when designing GitHub Actions, CI checks, release workflows, deployment gates, artifact provenance, migrations, rollback, or environment promotion.
---

# CI/CD & Release

Every merge should produce verifiable evidence.

## Pull request checks
Format/lint -> type check -> unit tests -> security/static checks -> build -> selected integration tests.

## Release
Build immutable artifacts once and promote them.
Run database migration safety checks before deployment.
Use environment protection for production.
Support rollback or forward-fix strategy explicitly.

## Security
Use short-lived/OIDC cloud credentials where possible.
Pin third-party workflow actions to trusted versions/SHAs.
Never print secrets to logs.
