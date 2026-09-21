# Kubernetes deployment

Plain Kustomize, applied with `kubectl`. No templating engine and no cluster add-on is
required beyond a CNI that enforces NetworkPolicy.

```
base/                       hardened namespace, workloads, services and network policy
migrate/                    production schema migration Job
overlays/staging-bootstrap/ staging namespace/config/policy only; no Job or Deployments
overlays/staging-migrate/   staging bootstrap plus the digest-pinned migration Job
overlays/staging/           isolated staging workloads and digest pins
overlays/production/        production replicas and digest pins
```

## Before the first apply

Create the Secret out of band. It is never committed, and nothing in this directory
contains a credential:

```bash
kubectl create namespace nexora
kubectl create secret generic nexora-secrets -n nexora \
  --from-literal=NEXORA_MIGRATION_DATABASE_URL='postgresql://nexora_migrate:password@host:5432/nexora' \
  --from-literal=NEXORA_API_DATABASE_URL='postgresql://nexora_api_login:password@host:5432/nexora' \
  --from-literal=NEXORA_WORKER_DATABASE_URL='postgresql://nexora_worker_login:password@host:5432/nexora' \
  --from-literal=NEXORA_REDIS_URL='redis://host:6379/0' \
  --from-literal=NEXORA_SESSION_REDIS_URL='redis://host:6379/1' \
  --from-literal=NEXORA_METRICS_TOKEN='<at least 32 characters>' \
  --from-literal=NEXORA_WEB_ORIGIN='https://nexora.example' \
  --from-literal=NEXORA_OIDC_CLIENT_ID='<client id>' \
  --from-literal=NEXORA_OIDC_CLIENT_SECRET='<client secret>' \
  --from-literal=NEXORA_OPENAI_API_KEY='<provider key>'
```

Before applying the migration Job, provision database roles once. Run
`infra/postgres/production_roles.sql` as a database administrator to create the NOLOGIN
capability roles. Create three independent login identities in your database/identity
system: one migration owner, one API login inheriting `nexora_api_runtime`, and one worker
login inheriting `nexora_worker_runtime`. The migration identity must own the Nexora
database, `public` schema and existing Nexora tables. For an upgraded installation, perform
the ownership transfer as a DBA before enabling these three URLs; the migration step fails
closed when it finds tables owned by another role.

Then edit, in `base/configmap.yaml`, the issuer and audience, and the worker's model
profiles — the workspace IDs, provider and model this deployment allows. The API discovers
the issuer's OIDC metadata and rotates RS256 signing keys automatically. If that provider
serves JWKS from another origin, set `NEXORA_AUTH_JWKS_URL` explicitly to that HTTPS URL.
A run can never name anything the profiles do not list.

If the worker runtime config enables retrieval ANN, provision the reviewed HNSW index before
rolling workers. Run this with the migration-owner database credential; API/worker runtime roles
cannot create indexes:

```bash
export NEXORA_DATABASE_URL='postgresql://nexora_migrate:password@host:5432/nexora'
python -m nexora_api.rag_ann ensure \
  --model text-embedding-3-small \
  --dimensions 1536
```

The model and dimensions must exactly match the worker retrieval configuration. HNSW is optional;
omit the runtime `ann` block for exact vector ranking. To roll back ANN, remove that block from the
worker runtime first, roll workers, then run the same command with `drop` instead of `ensure`.

If the worker runtime config declares remote MCP servers, put their bearer tokens in the
optional `nexora-mcp-secrets` Secret under the exact environment-variable names referenced
by `bearer_token_env`. The base deployment never contains those values and starts normally
when no MCP server is configured:

```bash
kubectl create secret generic nexora-mcp-secrets -n nexora \
  --from-literal=NEXORA_MCP_OPS_TOKEN='<short-lived server token>'
```

Remote MCP endpoints are operator configuration, not workspace data. The built-in transport
accepts HTTPS on port 443 only; the worker NetworkPolicy additionally excludes private,
loopback, link-local and carrier-grade NAT ranges.

## Staging first apply

Staging uses the separate `nexora-staging` namespace. It must also use a separate PostgreSQL
database/cluster and Redis/session namespace from production. Never point
`NEXORA_MIGRATION_DATABASE_URL` in the staging Secret at the production database.

Edit the staging overlay before first use: replace the `.invalid` OIDC issuer, the staging model
name and the allowed staging workspace UUID. The default staging runtime enables hybrid retrieval
without ANN so the E2E retrieval fixture can run without an operator-created HNSW index.

Apply only the bootstrap boundary first:

```bash
kubectl apply -k infra/k8s/overlays/staging-bootstrap
```

Then create `nexora-secrets` in `nexora-staging` with staging-only database, Redis, OIDC,
metrics and provider credentials. The key names are the same as production, but the values must not
reuse production state or credentials unless an external provider credential is intentionally shared.

Promote the verified release digests into staging. The API command updates both the staging
migration and workload overlays only after both files validate:

```bash
python scripts/promote_release.py \
  --environment staging \
  --image nexora/api \
  --new-name ghcr.io/<owner>/nexora-api \
  --digest sha256:<verified-api-digest>

python scripts/promote_release.py \
  --environment staging \
  --image nexora/web \
  --new-name ghcr.io/<owner>/nexora-web \
  --digest sha256:<verified-web-digest>
```

Run migration before workloads:

```bash
kubectl delete job nexora-migrate -n nexora-staging --ignore-not-found
kubectl apply -k infra/k8s/overlays/staging-migrate
kubectl wait --for=condition=complete job/nexora-migrate -n nexora-staging --timeout=300s

kubectl apply -k infra/k8s/overlays/staging
for deployment in nexora-api nexora-web nexora-worker; do
  kubectl rollout status "deployment/$deployment" -n nexora-staging --timeout=300s
done
```

After the public API/Gateway route points at staging, run **Staging E2E Smoke**. Hosted runners
verify retrieval through the configured source key and unique output sentinel; they do not need
access to worker administration. Enable `verify_worker_metrics` only for a private/self-hosted
runner that already has authorized cluster-network access.

## Deploying a release

Migrations run first and are awaited, so no pod starts against a schema it has not
migrated. The Job uses only the migration-owner credential. After DDL it revokes PUBLIC
application-object access and reapplies the reviewed API/worker privilege matrix. A new
table that has not been classified in that matrix fails the migration rather than receiving
implicit runtime access. Migrations must remain backward compatible with the running version:
the old pods keep serving while the Job runs.

```bash
kubectl delete job nexora-migrate -n nexora --ignore-not-found
kubectl apply -k infra/k8s/migrate
kubectl wait --for=condition=complete job/nexora-migrate -n nexora --timeout=300s
kubectl apply -k infra/k8s/overlays/production
kubectl rollout status deployment/nexora-api -n nexora --timeout=300s
```

Set the digests in `overlays/production/kustomization.yaml` to the images CI built and
verified. A moving tag cannot be the artifact that passed CI, so images are pinned by
digest and never by tag.

The Release workflow publishes each image on every push to `main`, scans the immutable
digest, creates a short-lived OIDC/Sigstore signed GitHub provenance attestation, verifies
that attestation against the release workflow and source commit, and then prints staging-first
and production promotion commands.

After staging smoke and rollback acceptance, pin the exact same digest into production:

```bash
python scripts/promote_release.py \
  --environment production \
  --image nexora/api \
  --new-name ghcr.io/<owner>/nexora-api \
  --digest sha256:<accepted-staging-api-digest>
```

Commit promotion edits and merge them: promotion is reviewable history, not a side effect of a
build. The script refuses malformed digests, undeclared images and any moving tag alongside a
digest. There is no rebuild between staging and production.

Operators can independently repeat the provenance check before applying the overlay:

```bash
docker login ghcr.io
gh attestation verify oci://ghcr.io/<owner>/<image>@sha256:<digest> \
  --repo <owner>/<repo> \
  --signer-workflow <owner>/<repo>/.github/workflows/release.yml \
  --source-digest <release-commit-sha>
```

The release gate reports every HIGH/CRITICAL image vulnerability and fails when one has a
published fix. Findings without a published fix stay visible for operator risk review but
do not automatically block promotion. See ADR 0035.

## Staging acceptance gate

After applying the candidate, run the manual `Staging E2E Smoke` workflow. The GitHub
`staging` environment supplies the public staging API origin, fixture identifiers and a
short-lived Nexora access token. For retrieval mode it also supplies a known source key and unique
non-secret sentinel that the prompt must recover.

The worker admin/metrics service stays cluster-internal. Do not expose it to make hosted CI pass.
Private runners with legitimate cluster access may opt into worker metric-delta verification.

Enable approval mode only after configuring a safe read-only or independently idempotent
`require_approval` MCP fixture. See ADR 0036 for exact variables and trust boundaries.

## Rollback and staging drill

Deployments keep three revisions. Before production promotion, prove the previous staging
application revision still works against the migrated schema:

```bash
for deployment in nexora-api nexora-web nexora-worker; do
  kubectl rollout undo "deployment/$deployment" -n nexora-staging
done
for deployment in nexora-api nexora-web nexora-worker; do
  kubectl rollout status "deployment/$deployment" -n nexora-staging --timeout=300s
done
```

Run **Staging E2E Smoke** against the rolled-back application revision. Then re-apply
`infra/k8s/overlays/staging`, wait for all three rollouts and run the smoke again. A failed
rollback drill blocks production promotion.

Production rollback uses the same application command in namespace `nexora`, but database
migrations are never rolled back in place. A schema correction is a new forward-compatible
migration. This expand/contract rule is what makes application rollback safe. See ADR 0037.

## Exposure

Services are `ClusterIP`. The Ingress or Gateway is operator-provided, because the
controller, certificates and hostnames belong to the environment rather than to this
repository. Label that namespace `nexora.dev/ingress: "true"`, and a Prometheus
namespace `nexora.dev/monitoring: "true"`, or the network policies will keep both out.

Scraping still needs `NEXORA_METRICS_TOKEN`: `/metrics` is gated by token as well as by
network policy.

The API base config rate-limits each verified issuer/subject through shared Redis (120 requests per
60 seconds by default). A 429 includes `Retry-After`; a Redis failure fails authenticated traffic
closed instead of bypassing the limit. This does not replace edge controls: configure the
Ingress/Gateway for unauthenticated request, connection and body-size limits without trusting
client-supplied forwarding headers inside the application.

Sign-in performs OIDC discovery and the code exchange from the web pod, while the API
performs OIDC discovery and JWKS refresh for bearer verification. The shared identity-provider
egress policy allows public TLS only; an identity provider on a private address needs an
environment-specific rule naming it. Token-provided `jku`/`x5u` URLs are never followed.

## Validating changes

```bash
for target in \
  infra/k8s/overlays/staging-bootstrap \
  infra/k8s/overlays/staging-migrate \
  infra/k8s/overlays/staging \
  infra/k8s/overlays/production
do
  kubectl kustomize "$target" | kubeconform -strict -summary
done
pytest apps/api/tests/test_deployment_manifests.py
kubectl apply -k infra/k8s/overlays/staging --dry-run=server
```

CI renders and validates every repository-owned Kustomize target. Server dry-run needs a real
cluster and remains an operator/deployment-platform gate.
