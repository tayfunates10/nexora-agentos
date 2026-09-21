# ADR 0037: Staging rollout and rollback acceptance gate

Status: accepted for the production-readiness milestone.

ADR 0036 proves that an already deployed staging environment can execute a real Nexora run.
It deliberately does not mutate Kubernetes. The remaining release risk is operational: a
candidate can pass repository CI and even run correctly after deployment while the documented
rollback path is untested.

## Decision

Add a manually triggered `Staging Rollout Rollback` workflow backed by
`scripts/staging_rollout_rollback.py`.

The workflow accepts the API and web image digests produced by the release pipeline and combines
them with operator-owned staging registry names. Both candidate references must be immutable
`@sha256:` references. The API image is used for both `nexora-api` and `nexora-worker`; the web
image is used for `nexora-web`.

Before mutation the runner:

1. records the exact image reference currently deployed for API, worker and web;
2. refuses a staging baseline that is not itself digest pinned;
3. verifies the Kubernetes identity can only perform the deployment/read operations the drill
   needs;
4. performs a server-side dry run of each candidate image patch.

The acceptance sequence is then:

1. patch API, worker and web to the candidate digests;
2. wait for all three Kubernetes rollouts;
3. run the deployed staging E2E smoke from ADR 0036;
4. execute `kubectl rollout undo` for all three deployments;
5. wait for all rollback rollouts;
6. prove each workload returned to the exact image reference captured before the drill;
7. run the deployed staging E2E smoke again against the rolled-back release.

A successful drill deliberately leaves staging on the release that was present before the test.
Promotion remains a separate reviewed action.

## Failure containment

Once the first workload mutation is attempted, any failure triggers an emergency restore using the
three exact image references captured before the drill. This recovery does not trust rollout
history: it patches the known previous images directly and waits for every deployment.

If the ordinary rollback returns a different image than the captured baseline, the gate fails even
if the pods become ready. The exact-image check catches an unexpected revision-history state rather
than treating readiness as proof that the intended release was restored.

The workflow shares the `staging-acceptance` concurrency group with the normal staging smoke so a
read-only acceptance run cannot race the rollout mutation.

## Schema boundary

The drill changes workload images only. It does not execute database migrations.

An operator applies the candidate migration before this gate when the candidate contains schema
changes. The first smoke proves the candidate works against that schema; the rollback smoke then
proves the previous code still works against the current schema. That is the operational check for
the expand/contract requirement documented in ADR 0012.

A schema rollback is never attempted. If a migration is wrong, recovery is a new forward-fix
migration.

## Staging Kubernetes credential

The GitHub `staging` environment supplies `NEXORA_STAGING_KUBECONFIG` as an environment secret.
It must represent a short-lived, environment-scoped identity where the cluster supports one.
The identity needs only the namespace-scoped permissions required by the drill:

- get, watch and patch `deployments.apps`;
- get and list `replicasets.apps`.

It does not need Secret read access, pod exec, cluster-admin, registry credentials, database
credentials, provider keys or MCP credentials. Registry pull authentication remains attached to the
staging workloads by the environment.

The existing Nexora access token and metrics token are used only by the smoke checks. Provider and
MCP credentials remain inside the deployed worker.

## Boundaries

This is a staging acceptance gate, not an automatic production deployer. It does not edit the
production overlay, create a release tag, mutate GitHub releases, or promote a digest.

The workflow assumes the staging deployments are named `nexora-api`, `nexora-worker` and
`nexora-web` in namespace `nexora`, matching the repository manifests.

## Skills applied

cicd-release, docker-kubernetes, testing-quality, security-threat-modeling,
code-review-debugging.
