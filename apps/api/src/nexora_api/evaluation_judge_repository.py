import hashlib
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from nexora_api.config import Settings
from nexora_api.evaluation_judges import EvalJudgeCaseScore, EvalJudgeRun
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


def _request_hash(eval_run_id: UUID) -> str:
    encoded = json.dumps(
        {"eval_run_id": str(eval_run_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class EvaluationJudgeRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def _load(self, connection, workspace_id: UUID, judge_run_id: UUID) -> EvalJudgeRun:
        result = await connection.execute(
            """SELECT id,workspace_id,eval_run_id,status,case_count,
                      judge_provider,judge_model,prompt_version,error_code,
                      created_at,finished_at
               FROM eval_judge_runs
               WHERE workspace_id=%s AND id=%s""",
            (workspace_id, judge_run_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(404)

        scores_result = await connection.execute(
            """SELECT s.case_id,c.case_key,s.task_completion,s.answer_relevance,
                      s.clarity,s.quality_milli,s.rationale,
                      s.baseline_task_completion,s.baseline_answer_relevance,
                      s.baseline_clarity,s.baseline_quality_milli,
                      s.quality_delta_milli,s.input_tokens,s.output_tokens,s.latency_ms
               FROM eval_judge_case_scores s
               JOIN eval_cases c
                 ON c.id=s.case_id AND c.workspace_id=s.workspace_id
               WHERE s.workspace_id=%s AND s.judge_run_id=%s
               ORDER BY c.case_no""",
            (workspace_id, judge_run_id),
        )
        score_rows = await scores_result.fetchall()
        scores = [
            EvalJudgeCaseScore(
                case_id=item["case_id"],
                case_key=item["case_key"],
                task_completion=item["task_completion"],
                answer_relevance=item["answer_relevance"],
                clarity=item["clarity"],
                quality_milli=item["quality_milli"],
                rationale=item["rationale"],
                baseline_task_completion=item["baseline_task_completion"],
                baseline_answer_relevance=item["baseline_answer_relevance"],
                baseline_clarity=item["baseline_clarity"],
                baseline_quality_milli=item["baseline_quality_milli"],
                quality_delta_milli=item["quality_delta_milli"],
                regression=(
                    item["baseline_quality_milli"] is not None
                    and item["quality_milli"] < item["baseline_quality_milli"]
                ),
                improvement=(
                    item["baseline_quality_milli"] is not None
                    and item["quality_milli"] > item["baseline_quality_milli"]
                ),
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
                latency_ms=item["latency_ms"],
            )
            for item in score_rows
        ]
        scored_count = len(scores)
        quality_milli = None
        baseline_quality_milli = None
        quality_delta_milli = None
        if scores:
            quality_milli = (
                sum(item.quality_milli for item in scores) + scored_count // 2
            ) // scored_count
            baseline_values = [
                item.baseline_quality_milli
                for item in scores
                if item.baseline_quality_milli is not None
            ]
            if len(baseline_values) == scored_count:
                baseline_quality_milli = (
                    sum(baseline_values) + scored_count // 2
                ) // scored_count
                quality_delta_milli = quality_milli - baseline_quality_milli

        return EvalJudgeRun(
            id=row["id"],
            workspace_id=row["workspace_id"],
            eval_run_id=row["eval_run_id"],
            status=row["status"],
            case_count=row["case_count"],
            scored_count=scored_count,
            judge_provider=row["judge_provider"],
            judge_model=row["judge_model"],
            prompt_version=row["prompt_version"],
            error_code=row["error_code"],
            quality_milli=quality_milli,
            baseline_quality_milli=baseline_quality_milli,
            quality_delta_milli=quality_delta_milli,
            regression_count=sum(item.regression for item in scores),
            improvement_count=sum(item.improvement for item in scores),
            input_tokens=sum(item.input_tokens for item in scores),
            output_tokens=sum(item.output_tokens for item in scores),
            latency_ms=sum(item.latency_ms for item in scores),
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            results=scores,
        )

    async def create(
        self,
        principal,
        workspace_id: UUID,
        eval_run_id: UUID,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[EvalJudgeRun, bool]:
        fingerprint = _request_hash(eval_run_id)
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            eval_result = await connection.execute(
                """SELECT id,case_count,baseline_eval_run_id,
                          created_by_issuer,created_by_subject
                   FROM eval_runs
                   WHERE workspace_id=%s AND id=%s
                   FOR SHARE""",
                (workspace_id, eval_run_id),
            )
            eval_run = await eval_result.fetchone()
            if not eval_run:
                raise HTTPException(404)
            if (
                eval_run["created_by_issuer"] != principal.issuer
                or eval_run["created_by_subject"] != principal.subject
            ):
                raise HTTPException(404)

            source_count_result = await connection.execute(
                """SELECT count(*)::integer AS count
                   FROM eval_agent_run_sources
                   WHERE workspace_id=%s AND eval_run_id=%s""",
                (workspace_id, eval_run_id),
            )
            source_count = (await source_count_result.fetchone())["count"]
            if source_count != eval_run["case_count"]:
                raise HTTPException(422, "judge_requires_agent_run_sources")

            baseline_id = eval_run["baseline_eval_run_id"]
            if baseline_id is not None:
                baseline_result = await connection.execute(
                    """SELECT id,case_count,created_by_issuer,created_by_subject
                       FROM eval_runs
                       WHERE workspace_id=%s AND id=%s
                       FOR SHARE""",
                    (workspace_id, baseline_id),
                )
                baseline = await baseline_result.fetchone()
                if (
                    not baseline
                    or baseline["created_by_issuer"] != principal.issuer
                    or baseline["created_by_subject"] != principal.subject
                ):
                    raise HTTPException(404)
                baseline_sources_result = await connection.execute(
                    """SELECT count(*)::integer AS count
                       FROM eval_agent_run_sources
                       WHERE workspace_id=%s AND eval_run_id=%s""",
                    (workspace_id, baseline_id),
                )
                baseline_sources = (await baseline_sources_result.fetchone())["count"]
                if baseline_sources != baseline["case_count"]:
                    raise HTTPException(422, "judge_baseline_requires_agent_run_sources")

            existing_result = await connection.execute(
                """SELECT id,request_hash FROM eval_judge_runs
                   WHERE workspace_id=%s
                     AND requested_by_issuer=%s
                     AND requested_by_subject=%s
                     AND idempotency_key=%s
                   FOR UPDATE""",
                (
                    workspace_id,
                    principal.issuer,
                    principal.subject,
                    idempotency_key,
                ),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise HTTPException(409)
                return await self._load(connection, workspace_id, existing["id"]), False

            judge_run_id = uuid4()
            try:
                await connection.execute(
                    """INSERT INTO eval_judge_runs
                       (id,workspace_id,eval_run_id,requested_by_issuer,
                        requested_by_subject,request_hash,idempotency_key,case_count)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        judge_run_id,
                        workspace_id,
                        eval_run_id,
                        principal.issuer,
                        principal.subject,
                        fingerprint,
                        idempotency_key,
                        eval_run["case_count"],
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(409) from exc

            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "eval_judge.queued",
                request_id,
            )
            return await self._load(connection, workspace_id, judge_run_id), True

    async def get(self, principal, workspace_id: UUID, judge_run_id: UUID) -> EvalJudgeRun:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            owner_result = await connection.execute(
                """SELECT requested_by_issuer,requested_by_subject
                   FROM eval_judge_runs
                   WHERE workspace_id=%s AND id=%s""",
                (workspace_id, judge_run_id),
            )
            owner = await owner_result.fetchone()
            if not owner:
                raise HTTPException(404)
            if (
                owner["requested_by_issuer"] != principal.issuer
                or owner["requested_by_subject"] != principal.subject
            ):
                raise HTTPException(404)
            return await self._load(connection, workspace_id, judge_run_id)
