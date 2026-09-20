# ADR 0012: Kubernetes deployment manifests

Status: accepted for the deployment milestone.

Compose describes local development. This increment describes production: how the API,
worker and web run on Kubernetes, what they are allowed to reach, and how a release is
applied and reversed.

## Plain Kustomize
Kustomize ships inside `kubectl`, so a deployment needs no templating engine, no chart
repository and no cluster-side release state. Manifests stay readable as the objects
they are, and the overlay carries only what actually differs per environment: replica
counts, the image digests being promoted, and telemetry settings. A chart would buy
parameterization this platform does not yet need.

## Least privilege
The namespace enforces the restricted Pod Security Standard, so a workload that would
run privileged or as root is rejected at admission rather than relying on each pod
getting its own `securityContext` right. Every pod additionally runs as a non-root user
with a read-only root filesystem, all capabilities dropped, no privilege escalation and
the default seccomp profile. No workload talks to the Kubernetes API, so no pod receives
a service account token.

Nothing in the manifests holds a credential. Connection strings, the scrape token, the
provider key, the OIDC client secret and the token verification key come from a Secret
created out of band, and every credential-shaped environment variable is a
`secretKeyRef`. A test enforces both halves of that rule.

## Network boundaries
The namespace is default-deny for ingress and egress, and each allowance is written
separately. Two workloads leave the cluster, both over TLS and both only to public
address space, with private ranges, link-local and carrier-grade NAT excluded: the
worker, which calls model providers, and the web, which performs OIDC discovery and the
authorization-code exchange server side. A compromised tool, or a model-supplied
address, therefore cannot turn either into a proxy for cluster services or cloud
metadata. The API reaches no external network at all. Datastore egress is limited to the
datastore ports, and telemetry egress to the collector.

This is the same control the gateway already applies at the application layer, expressed
where the platform can enforce it without trusting the process.

## Probes and disruption
Readiness depends on PostgreSQL and Redis; liveness deliberately does not. A dependency
outage should withdraw traffic, not restart every pod and turn a blip into a restart
loop. The web shares one path for both probes because it renders an unavailable state
rather than failing when the API is down, so its liveness is already independent of the
datastores; a separate readiness endpoint would be decorative.

The API and web roll with `maxUnavailable: 0` behind a disruption budget and a preStop
pause that lets endpoint removal propagate. The worker gets a 60-second termination
grace period because SIGTERM stops its loop between jobs and the in-flight attempt
finishes under its own lease.

The API autoscales on CPU. The worker does not: queue depth is the signal that matters
for it, and consuming a custom metric needs an adapter this increment does not add, so
its replica count is set deliberately instead of scaled on a signal that does not
describe the work. CPU limits are set on every container per the platform rule; if the
latency SLO shows throttling, relaxing the API's limit is the first thing to try.

## Releases and rollback
The migration Job is applied and awaited before the workload rollout, so no pod starts
against a schema it has not migrated. It is kept out of `base` because a Job spec is
immutable and must be deleted and re-applied per release. Migrations must be backward
compatible with the running version, since old pods keep serving while the Job runs.

Images are pinned by digest in the overlay. A moving tag cannot be the artifact CI
verified. Rollback is `kubectl rollout undo`, which moves code back but not schema;
reverting a migration is a forward fix, which is the other reason expand/contract is a
requirement here rather than a preference.

## Verification without a cluster
CI renders every Kustomize target and validates the result against the Kubernetes
schemas with a checksum-pinned kubeconform, and the test suite asserts the rules above
directly: unprivileged workloads, bounded resources, probe semantics, no committed
credential, selector consistency, default-deny networking, the provider egress
exclusions and digest pinning. A server-side dry run remains the operator's gate,
because only a cluster can answer admission and quota.

## Boundaries
Ingress or Gateway objects, certificates, hostnames, the database and Redis themselves,
the OTLP collector and Prometheus are environment-owned and are not in this repository.
Image build and publication are not here either; the overlay expects a digest, and
producing it is the next increment.

## Skills applied
docker-kubernetes, security-threat-modeling, cicd-release, observability-otel,
testing-quality.

## References
- https://kubernetes.io/docs/concepts/security/pod-security-standards/
- https://kubernetes.io/docs/concepts/services-networking/network-policies/
- https://kubectl.docs.kubernetes.io/references/kustomize/
