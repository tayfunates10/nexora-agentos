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
  --from-literal=NEXORA_DATABASE_URL='postgresql://user:password@host:5432/nexora' \
  --from-literal=NEXORA_REDIS_URL='redis://host:6379/0' \
  --from-literal=NEXORA_SESSION_REDIS_URL='redis://host:6379/1' \
  --from-literal=NEXORA_METRICS_TOKEN='<at least 32 characters>' \
  --from-literal=NEXORA_WEB_ORIGIN='https://nexora.example' \
  --from-literal=NEXORA_OIDC_CLIENT_ID='<client id>' \
  --from-literal=NEXORA_OIDC_CLIENT_SECRET='<client secret>' \
  --from-literal=NEXORA_OPENAI_API_KEY='<provider key>' \
  --from-file=NEXORA_AUTH_PUBLIC_KEY=./auth-public-key.pem
```

Then edit, in `base/configmap.yaml`, the issuer and audience, and the worker's model
profiles — the workspace IDs, provider and model this deployment allows. A run can
never name anything the profiles do not list.

## Deploying a release

Migrations run first and are awaited, so no pod starts against a schema it has not
migrated. Migrations must remain backward compatible with the running version: the old
pods keep serving while the Job runs.

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

Sign-in performs OIDC discovery and the code exchange from the web pod, so it needs TLS
egress to the identity provider. The policy allows public address space only; an
identity provider on a private address needs a rule naming it.

## Validating changes

```bash
kubectl kustomize infra/k8s/overlays/production | kubeconform -strict -summary
pytest apps/api/tests/test_deployment_manifests.py
kubectl apply -k infra/k8s/overlays/production --dry-run=server
```

CI runs the first two. The server dry run needs a cluster and is the operator's gate.
