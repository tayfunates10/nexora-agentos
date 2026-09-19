---
name: docker-kubernetes
description: Use when creating Dockerfiles, Compose environments, Kubernetes manifests, Helm charts, probes, resources, network policies, or production container configuration.
---

# Docker & Kubernetes

Containers must be reproducible, minimal and non-root.

## Docker
- Multi-stage builds.
- Pin important runtime versions.
- Do not bake secrets into images.
- Health checks where meaningful.
- Separate build and runtime dependencies.

## Kubernetes
- Requests/limits for every workload.
- Readiness and liveness/startup probes chosen deliberately.
- Pod security and least-privilege service accounts.
- Network policies for sensitive services.
- Secrets through secret management, not committed manifests.
- Graceful shutdown for API/workers.
