# ADR 0042: Staging deployment

Status: implemented.

## Context

The repository could prove a release two ways and deploy it none. The staging smoke (ADR 0036)
validates an environment that is already running, and the rollout/rollback drill (ADR 0037)
rolls a candidate out only to roll it back, leaving staging on the previous release by design.
Getting a release onto staging in the first place was an undocumented sequence of `kubectl`
commands against manifests that existed for production only: one overlay, one namespace, and a
migration Job pinned to `nexora/api:0.1.0` — a tag that no registry serves.

So the first environment a change should reach was the one with no supported way to reach it.

## Decision

A staging overlay, a deploy script and a manual workflow, each mirroring the production path
rather than inventing a second one.

`infra/k8s/overlays/staging` renders the same base into the `nexora-staging` namespace: the same
restricted pod security, default-deny network policy, probes and split database identities, so a
rollout here exercises what production will run. It differs only where the difference is the
point: one replica each, the API autoscaler removed so that count is what actually runs, the
API and web disruption budgets relaxed to `minAvailable: 0` because a single-replica workload
behind `minAvailable: 1` cannot be drained at all, and full trace sampling.

`infra/k8s/overlays/staging/migrate` carries the migration Job in the same namespace, pinned to
the same digest. A schema change applied by a different build than the one about to serve it is
not a release, and a committed test fails when the two overlays disagree.

`scripts/deploy_staging.py` is the deployment itself, standard library only like its siblings:

1. Both overlays must already pin exactly the digests the caller named. The reviewed file in git
   decides what runs; the workflow input is a confirmation, not a source. A mismatch stops the
   deploy and says to promote the overlay — with `scripts/promote_release.py --overlay`, the same
   reviewed edit production uses — rather than deploying something no one approved.
2. The staging identity's permissions are checked with `kubectl auth can-i`, then the whole
   overlay is server-side dry-run, so admission, schema and quota are validated before anything
   is mutated.
3. The migration Job is deleted, re-applied and awaited. A migration that does not complete stops
   the deploy before any workload is touched.
4. The overlay is applied, all three rollouts are awaited, and the images actually running are
   read back and compared with what was requested. A deploy that cannot prove what is running
   fails.
5. If the rollout fails and there was a previous release, its exact images are restored — the
   state the expand-contract migration rule exists for. On a first deployment there is nothing to
   restore, and the script says so instead of pretending it recovered.

`Deploy Staging` is `workflow_dispatch` only, bound to the `staging` GitHub environment so its
protection rules and secrets apply, and it shares the `staging-acceptance` concurrency group with
the smoke and the drill so a deploy can never race either. It optionally runs the deployed smoke
afterwards, which is what turns "the rollout finished" into "the environment works".

## State and trust boundaries

Nothing here holds a provider key, an MCP credential or a database password: the kubeconfig is
written with `umask 077`, exported for the job and removed in an `always()` step, and every other
credential stays in the cluster Secret the operator created out of band. The deploy mutates
workloads and the migration Job in one namespace and nothing else; it cannot promote production,
which remains a reviewed digest edit.

Staging is a real environment, not a test fixture: it runs real images against a real database
with real provider egress if the operator configured one. A run started there spends real money,
which is why the workspace budget applies to it exactly as it does anywhere else.

## Verification

Contract tests drive the deploy against a fake cluster: a successful deployment proves the
requested release is running, the migration is ordered before any workload apply, a failed
migration stops before the rollout, a failed rollout restores the previous images, a first
deployment reports that there is nothing to restore, a release the overlay does not pin is
refused, a migration Job pinned to another build is refused, an identity without the required
verbs is refused, skipping migrations never touches the Job, and redeploying the current release
is a valid no-op. Manifest tests assert that both staging overlays are digest-pinned, agree on
the API digest and use a namespace of their own; release-tooling tests assert both are promotable
as shipped. CI renders and validates both new overlays with kubeconform alongside the existing
three.

Skills: docker-kubernetes, cicd-release, testing-quality, security-threat-modeling,
postgres-data-modeling.
