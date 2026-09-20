# ADR 0031: PostgreSQL migration and runtime role separation

Status: implemented for the production Kubernetes deployment.

## Problem

The API, worker and migration Job previously consumed the same database credential. That made every
runtime process effectively a schema owner: a compromised API or worker could perform DDL, alter
migration state, or mutate tables belonging only to the other process.

## Decision

Production uses three independent database login identities:

- the migration identity owns the Nexora database/schema/tables and performs forward-only DDL;
- the API login inherits the `nexora_api_runtime` capability role;
- the worker login inherits the `nexora_worker_runtime` capability role.

The capability roles themselves are NOLOGIN roles created once by a DBA. Passwords, IAM bindings and
actual login names stay outside the repository. Kubernetes stores three separate connection URLs:
`NEXORA_MIGRATION_DATABASE_URL`, `NEXORA_API_DATABASE_URL`, and
`NEXORA_WORKER_DATABASE_URL`, mapping the appropriate one to the process-local
`NEXORA_DATABASE_URL` setting.

After every successful migration, the migration process reapplies the runtime grant policy. PUBLIC
loses CREATE on the public schema and all table/sequence privileges. Both runtime roles receive schema
USAGE and SELECT on the explicitly classified application tables. DML grants are process-specific:
the API cannot write worker receipts, model journals, judge scores or spend-ledger rows; the worker
cannot change workspaces, memberships, agent definitions, evaluation suites/cases/runs or spend
budgets. Neither runtime role receives TRUNCATE, REFERENCES or TRIGGER.

A migration introducing a new table fails deployment until that table is explicitly classified in
the grant policy. Runtime sequences are currently forbidden for the same reason. This turns database
permission review into a release gate instead of allowing future schema objects to inherit accidental
access.

The migration identity must own every Nexora table before separation is enabled. Existing deployments
therefore need a one-time DBA ownership transfer. The grant step also rejects runtime roles with
SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS attributes, and verifies their effective
privileges after applying grants so broader inherited membership fails closed.

## Local development

Docker Compose keeps its single development database owner for low-friction local setup. Production
Kubernetes is the hardened path. CI creates temporary NOLOGIN runtime roles against the integration
database and verifies the complete effective privilege matrix.

## Operational boundary

Database role separation limits what a compromised process can do at PostgreSQL. It does not replace
tenant scoping, API authorization, append-only triggers, backups, managed-database network controls or
credential rotation. Each login credential should be independently rotatable and stored in the
deployment secret manager rather than committed manifests.

Skills: postgres-data-modeling, security-threat-modeling, docker-kubernetes, testing-quality,
cicd-release.
