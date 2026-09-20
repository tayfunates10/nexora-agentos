# ADR 0011: Operator-configured worker service

Status: accepted for the deployment milestone.

ADR 0009 described the durable executor as an opt-in worker service but shipped it as a
library with no process. This increment delivers that process: an entrypoint, the
operator configuration it requires, its admin surface and its shutdown behavior.

## Operator configuration
Model candidates and execution profiles come from a JSON file the operator mounts at
`NEXORA_WORKER_RUNTIME_CONFIG`. A run selects a profile only by the `model_profile` its
agent definition already carries; it cannot name a model, a provider, an endpoint or a
tool that the profile does not list. Provider credentials are read from the process
environment and never appear in the configuration file, which is why an unknown key
there is rejected rather than ignored.

The loader fails closed. A missing path, an unreadable or oversized file, malformed
JSON, an unknown field, a profile with no workspaces, a profile whose providers have no
model candidate, or a provider with no available adapter all stop the worker from
starting. Silently starting a worker that can never execute — or one that would execute
more than an operator declared — is worse than not starting.

Adapter capabilities are derived from the same declared candidates, so routing and the
provider adapter cannot disagree about what a model supports.

## Process lifetime
The loop owns process concerns only; claiming, leasing, fencing, retries, approval
suspension and cancellation stay in the worker state machine, and model turns and tool
calls stay in the executor. SIGTERM stops the loop between jobs, so an in-flight attempt
keeps its lease and finishes instead of being abandoned for another worker to recover
later. An idle poll waits on the shutdown event rather than sleeping, so a stopping
worker exits promptly.

A dependency failure backs the loop off with bounded exponential delay instead of
exiting: a worker must outlive a database or queue blip, and the delay is interruptible
by shutdown. Retry, attempt limits and dead-lettering remain the state machine's
responsibility; the loop never decides a run's outcome.

## Admin surface
The worker serves liveness, readiness and guarded metric scraping on its own port. A
deployed worker needs probes and its own exposition, and the API process cannot report
on it because metrics are per-process. That surface carries no tenant API, no OpenAPI
document, and the scrape endpoint uses exactly the same token gate as the API.

## Deployment boundary
The worker is an opt-in Compose profile, so the default stack is unchanged. Retrieval is
not wired into the worker: the executor accepts a retriever, but no embedding adapter
exists yet, so a configured worker runs without retrieval rather than with a stub.
Kubernetes manifests, image promotion and rollback remain the next increment.

## Skills applied
agent-architecture, docker-kubernetes, security-threat-modeling, model-routing,
observability-otel, testing-quality.

## References
- https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/
- https://docs.docker.com/compose/how-tos/profiles/
