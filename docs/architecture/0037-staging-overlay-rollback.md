# ADR 0037: Isolated staging overlay and rollback acceptance

Status: accepted for the production-readiness milestone.

A deployed smoke gate is useful only when there is a staging environment that can receive the exact
artifact intended for production without sharing production state. Nexora therefore treats staging
as a first-class promotion target rather than a mutable tag or an ad-hoc local deployment.

## Decision

`infra/k8s/overlays/staging` renders the normal hardened base into the
`nexora-staging` namespace. It keeps the base pod-security, non-root containers, default-deny
networking, probes, resource bounds and public-egress restrictions. It changes only
environment-specific concerns:

- API, web and worker run at one replica each;
- the API HPA is bounded to one through two replicas;
- the web talks to the API through the `nexora-staging` service DNS name;
- the worker runtime is explicitly staging-owned and enables hybrid retrieval;
- images remain pinned by immutable digest.

The staging worker admin service remains ClusterIP/headless and its NetworkPolicy still admits only
the monitoring boundary. The staging overlay does not add an Ingress, LoadBalancer or NodePort for
worker administration.

## Migration-first bootstrap

`infra/k8s/overlays/staging-migrate` contains only the namespace, service account, non-secret
configuration, NetworkPolicies and migration Job. It deliberately excludes API/web/worker
Deployments. This allows a first installation to create the minimum platform boundary, inject the
out-of-band staging Secret, run migrations, and only then start application workloads.

Staging must use a distinct PostgreSQL database/cluster and Redis/session namespace from production.
The migration credential for `nexora-staging` must never point at the production database. This is
an operator configuration requirement because credentials are intentionally not committed.

## Artifact promotion

The release artifact is built once. `scripts/promote_release.py --environment staging` writes the
same verified digest into the staging overlays. For `nexora/api`, it validates both the workload
and migration overlays before writing either so a migration cannot accidentally run a different API
artifact from the one staged.

After staging acceptance, `--environment production` pins that same digest into the production
overlay. There is no rebuild between environments.

## Rollout sequence

For a candidate whose staging overlay has already been updated:

1. verify GitHub/Sigstore provenance for both image digests;
2. create/update the `nexora-secrets` Secret in `nexora-staging` out of band;
3. delete the previous staging migration Job;
4. apply `overlays/staging-migrate` and wait for the Job to complete;
5. apply `overlays/staging`;
6. wait for API, web and worker rollouts;
7. run `Staging E2E Smoke` with retrieval enabled;
8. optionally run the safe approval fixture.

No repository workflow receives cluster credentials. The operator or deployment platform that owns
the cluster performs these steps.

## Rollback drill

Deployments keep three ReplicaSet revisions. Before production promotion, staging should prove the
previous application revision remains compatible with the migrated schema:

1. record the current candidate image IDs and rollout revisions;
2. run `kubectl rollout undo` for API, web and worker in `nexora-staging`;
3. wait for all three rollouts;
4. run the staging smoke gate against the previous application revision;
5. re-apply the candidate staging overlay and wait for all three rollouts;
6. run the staging smoke gate again.

Database migrations are never rolled back in place. A schema problem is repaired by a new
forward-compatible migration. Therefore a failed rollback drill blocks production promotion and
must be corrected before shipping.

Worker jobs are durable and fenced, so a worker rollout may recover an interrupted attempt without
duplicating a completed side effect; governed non-read tools still require their own idempotency
contract.

## CI verification

Platform CI renders base, migrate, staging-migrate, staging and production Kustomize targets and
validates each rendered document with kubeconform. Repository tests additionally assert staging
namespace isolation, bounded replicas/HPA, staging service DNS, hybrid retrieval and digest pinning.

## Boundaries

Ingress/Gateway configuration, TLS certificates, managed PostgreSQL/Redis provisioning and
cluster-authentication mechanisms belong to the deployment environment. Staging does not weaken
those boundaries to make testing easier.

## Skills applied

docker-kubernetes, cicd-release, security-threat-modeling, testing-quality,
auth-rbac-multitenancy, event-driven-workflows, rag-engineering.
