# ADR 0019: Requester-scoped terminal run results

Status: implemented.

## Context

The executor persists immutable model steps, but the public run resource contains lifecycle
metadata only. Consumers need a reliable final result before automated evaluation can collect
observations from real runs. A workspace membership alone is insufficient for raw output:
retrieval may have used sources whose ACL grants access only to the original requester.

## Decision

GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/result returns a typed terminal result.
Current workspace membership and the exact requester issuer/subject are required. Owners and
admins receive no override for another requester's output. Missing, foreign-workspace and
other-requester run IDs all return 404. Membership is rechecked on every request.

The repository locks the run for reading within the authorized transaction and reads at most
33 journal rows to enforce the executor's 32-step bound. The journal must be contiguous from
step zero. Failed or cancelled runs may legitimately have no recorded model steps.

For a successful run, the final recorded response must contain non-empty text, finish with
stop or refusal, and contain no tool calls. Otherwise the endpoint returns 409. Queued, running
and approval-waiting runs also return 409; their intermediate answers are never published as
final output. Failed and cancelled results always have null output_text and finish_reason.
Refusals remain explicit even though the runtime marks the execution as succeeded.

The response includes step/provider/model metadata and totals named recorded_input_tokens and
recorded_output_tokens. These count committed journal responses only, not provider attempts
that failed before persistence, and are not a billing estimate.

selected_tools is the sorted distinct set of model-requested tool names across recorded steps.
It does not assert execution or approval: a denied selection may appear here. Tool arguments,
tool outputs, intermediate model text and structured_output are excluded.

## State and trust boundaries

This read-only endpoint neither changes run state nor invokes providers, retrieval or tools.
Existing worker fences and immutable journal writes remain unchanged. The existing run/status
endpoint continues to expose only metadata to workspace members. This result is a historical
requester-owned snapshot; it is not re-retrieved or re-scored on read.

No evaluation is created automatically. A future importer must preserve suite/input matching,
terminal status, provenance, requester authorization, baseline compatibility and complete-case
semantics. Citation text alone must not be treated as verified retrieval provenance.

## Verification

Unit tests cover contiguous journal and step limits, success/refusal, terminal failure/cancel,
unfinished states, exact whitespace preservation, invalid token usage and exclusion of arguments
and intermediate text. Real worker integration verifies the stored response, usage and model
metadata, pending-result refusal, requester-only access including owner/admin denial, and loss
of access after workspace membership removal.

Skills: agent-architecture, llm-evaluation, api-openapi, fastapi-backend,
auth-rbac-multitenancy, testing-quality, code-review-debugging, cicd-release.
