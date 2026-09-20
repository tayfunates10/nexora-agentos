# ADR 0013: Release pipeline

Status: accepted for the deployment milestone.

ADR 0012 expects a digest and says a moving tag cannot be the artifact CI verified.
Nothing produced that digest. This increment closes the loop: build the artifact once,
attach evidence to it, promote it by an explicit edit, and refuse a change that would
rewrite migration history a deployed database has already applied.

## Build once
Pushing to `main` builds each image a single time and publishes it to the registry
tagged by commit SHA. There is no `latest`: the only stable reference is the digest the
build returns, which is what the overlay pins. The build runs on the job's short-lived
token rather than a stored credential, and the job is attached to a deployment
environment so an organization can require approval before anything is published.

## Evidence attached to the artifact
BuildKit emits maximum-mode provenance and an SBOM as attestations on the published
image, so what a digest contains and how it was produced can be answered from the
registry rather than from a build log that expires. This uses the builder's own
attestation support rather than extra third-party actions, which keeps the workflow's
supply chain to the runner, Docker and one pinned first-party checkout action.

Sigstore-signed GitHub attestations verifiable with `gh attestation verify` would add a
second, independent chain of custody. That is a later addition, not a substitute for the
attestations already attached here.

## Promotion is an edit, not an event
The digest is written into the production overlay by a script and merged like any other
change, so promotion is reviewable, revertible and visible in history. The script edits
the file in place rather than re-serializing it, so comments and ordering survive, and it
refuses anything that would make the deployed artifact ambiguous: a malformed digest, an
image the overlay does not declare, or a tag left beside a digest. The release job prints
the exact command for the digest it just published.

Deployment itself stays outside this repository. Cluster credentials belong to the
environment that owns the cluster, and a deploy workflow here would either hold them or
pretend to. The apply and rollback sequence is documented instead.

## Migration history is append-only
The runtime already refuses to apply a migration whose checksum no longer matches the
one recorded in the database, so an edited migration fails at deploy time against a live
database. CI now checks the same rule against the base branch, where fixing it is still
cheap: a released migration may not be modified or deleted, names must sort
deterministically, and two branches may not claim the same number. Adding a migration is
always allowed.

This is the check that makes the rollback story in ADR 0012 true. `kubectl rollout undo`
moves code back but not schema, so the previous release must tolerate the current schema,
and that only holds while old migrations stay exactly as they were applied.

## Boundaries
Image signing and registry retention, cluster credentials and the deploy step, staging
promotion between environments, and vulnerability scanning of published images are not
here. The scripts are build-time tools kept out of the runtime package, so nothing in a
published image depends on them.

## Skills applied
cicd-release, docker-kubernetes, security-threat-modeling, postgres-data-modeling,
testing-quality.

## References
- https://docs.docker.com/build/metadata/attestations/
- https://docs.github.com/actions/deployment/targeting-different-environments/using-environments-for-deployment
- https://docs.github.com/packages/working-with-a-github-packages-registry/working-with-the-container-registry
