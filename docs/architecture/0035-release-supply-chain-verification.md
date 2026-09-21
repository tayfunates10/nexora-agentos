# ADR 0035: Release supply-chain verification

Status: accepted for the production hardening milestone.

ADR 0013 made releases immutable and reviewable: build once, publish by commit SHA,
attach BuildKit provenance/SBOM evidence, and promote only by digest. Two production
questions were still unanswered: whether the exact published digest has actionable severe
vulnerabilities, and whether an operator can cryptographically verify that the digest came
from Nexora's release workflow and source commit.

## Decision

The release workflow treats the published image digest as the security subject. It never
scans or signs a moving tag.

After BuildKit pushes the image, the workflow installs Trivy 0.74.0 from the project's
immutable GitHub release assets. The checksum manifest is itself pinned by SHA-256 in the
workflow; the downloaded Linux archive must then match the checksum recorded in that
verified manifest. The scanner is therefore treated as a supply-chain input rather than a
trusted ambient executable.

The workflow runs two vulnerability passes against the immutable digest:

1. report every HIGH and CRITICAL vulnerability so operators can see the complete severe
   set, including findings without a published fix;
2. fail the release when a HIGH or CRITICAL finding has a published fix.

An image that fails the second pass remains an unpromoted registry artifact. The workflow
does not print staging or production promotion commands for it.

After the vulnerability gate passes, `actions/attest` is pinned to the exact v4.2.1
commit and receives only short-lived GitHub Actions OIDC authority. It creates a signed
SLSA provenance attestation for the image name and digest and stores it with GitHub and the
OCI registry. No long-lived signing private key is introduced.

The next step immediately verifies the attestation with `gh attestation verify`. The
verification pins all three identities that matter:

- repository: the Nexora repository that owns the build;
- signer workflow: `.github/workflows/release.yml`;
- source digest: the exact Git commit being released.

Only after scan and provenance verification succeed does the job summary expose staging-first
and production promotion commands for the same digest.

## Threat model

The controls address four release-path threats:

- a mutable tag resolving to content other than the reviewed artifact;
- a compromised or substituted scanner binary;
- a severe known vulnerability entering the production promotion path unnoticed;
- an image digest being presented as a Nexora build without a verifiable workflow/source
  identity.

The workflow action itself is pinned by full commit SHA. The scanner release is versioned
and its checksum manifest is pinned by SHA-256. Signing uses workload identity and Sigstore
rather than a repository secret that could be copied and replayed elsewhere.

## Boundaries

Trivy's vulnerability database is live external security data and can change after a
release. The release records a point-in-time gate, not a guarantee that no vulnerability
will be disclosed later. Continuous registry rescanning remains an operator responsibility.

Findings without a published fix are reported but are not an automatic release blocker.
This avoids making production availability depend on an issue the project cannot yet
remediate while keeping the risk visible for an explicit operator decision.

The signed GitHub provenance complements BuildKit's attached provenance and SBOM. It does
not replace image digest pinning, environment protection, registry retention policy,
cluster admission policy, or runtime hardening.

## Rollback

Rollback remains digest/revision based. A failed security gate alters no environment overlay,
so there is nothing to roll back. Staging acceptance and the application rollback drill happen
before production promotion; if a promoted digest later becomes unacceptable, revert to a
previously verified digest and follow the forward-fix migration rules in ADR 0037.

## Skills applied

cicd-release, security-threat-modeling, docker-kubernetes, testing-quality.
