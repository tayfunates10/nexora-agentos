# Kubernetes deployment

Plain Kustomize, applied with `kubectl`. No templating engine and no cluster add-on is
required beyond a CNI that enforces NetworkPolicy.

```
base/                  namespace, workloads, services, availability and network policy
migrate/               the schema migration Job, applied and awaited before a rollout
overlays/production/   replicas and the digests being promoted
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

The Release workflow publishes each image on every push to `main` and prints the exact
promotion command in its job summary:

```bash
python scripts/promote_release.py   --image nexora/api   --new-name ghcr.io/<owner>/nexora-api   --digest sha256:<digest from the release summary>
```

Commit that edit and merge it: promotion is reviewable history, not a side effect of a
build. The script refuses a malformed digest, an image the overlay does not declare, or
a tag left beside a digest.

## Rollback

Deployments keep three revisions, so a bad rollout is reversed without rebuilding:

```bash
kubectl rollout undo deployment/nexora-api -n nexora
```

A rollback moves the code back, not the schema. Reverting a migration is a forward-fix:
write and apply a new migration. This is why an expand/contract migration that the
previous release tolerates is a requirement rather than a preference.

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
kubectl kustomize infra/k8s/overlays/production | kubeconform -strict -summary
pytest apps/api/tests/test_deployment_manifests.py
kubectl apply -k infra/k8s/overlays/production --dry-run=server
```

CI runs the first two. The server dry run needs a cluster and is the operator's gate.
