import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import ValidationError

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.evaluation_judge import (
    JUDGE_RESPONSE_SCHEMA,
    JUDGE_SYSTEM_PROMPT,
    JudgeScores,
)
from nexora_api.model_routing import (
    ProviderAdapter,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
)
from nexora_api.run_results import summarize_result
from nexora_api.runtime_config import EvaluationJudgeConfig
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission

MAX_JUDGE_ATTEMPTS = 3


class JudgeExecutionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def judge_retry_delay(job_id: UUID, attempt_count: int) -> float:
    base = min(60.0, float(2 ** min(attempt_count, 6)))
    digest = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") / 65535
    return round(base * (1 + 0.25 * jitter), 3)


class EvaluationJudgeWorker:
    def __init__(
        self,
        settings: Settings,
        adapter: ProviderAdapter,
        config: EvaluationJudgeConfig,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ):
        if not 3 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 3 and 300")
        if adapter.name != config.provider:
            raise ValueError("judge adapter provider does not match configuration")
        self.settings = settings
        self.adapter = adapter
        self.config = config
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
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

    async def process_once(self) -> bool:
        job = await self._claim()
        if job is None:
            return False

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job["id"], stop))
        try:
            case = await self._next_case(job)
            if case is None:
                await self._finish_if_complete(job)
                return True

            await self._assert_authorized(job)
            candidate, candidate_usage, candidate_latency = await self._score(
                job,
                case["id"],
                case["input_text"],
                case["candidate_output"],
                "candidate",
            )

            baseline = None
            baseline_usage = (0, 0)
            baseline_latency = 0
            if case["baseline_output"] is not None:
                await self._assert_authorized(job)
                baseline, baseline_usage, baseline_latency = await self._score(
                    job,
                    case["id"],
                    case["input_text"],
                    case["baseline_output"],
                    "baseline",
                )

            await self._save_case(
                job,
                case["id"],
                candidate,
                baseline,
                input_tokens=candidate_usage[0] + baseline_usage[0],
                output_tokens=candidate_usage[1] + baseline_usage[1],
                latency_ms=candidate_latency + baseline_latency,
            )
        except HTTPException:
            await self._fail(job, "judge_permission_revoked", retryable=False)
        except JudgeExecutionError as exc:
            await self._fail(job, exc.code, retryable=exc.retryable)
        except ProviderError as exc:
            await self._fail(job, exc.code, retryable=exc.retryable)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(job, "judge_worker_error", retryable=True)
        finally:
            stop.set()
            await heartbeat
        return True

    async def _claim(self):
        allowed = list(self.config.allowed_workspaces)
        async with self.connection() as connection:
            await connection.execute(
                """UPDATE eval_judge_runs
                   SET status=CASE
                           WHEN attempt_count + 1 >= %s THEN 'failed'
                           ELSE 'queued'
                       END,
                       attempt_count=attempt_count+1,
                       available_at=now(),
                       lease_owner=NULL,
                       lease_expires_at=NULL,
                       error_code='lease_expired',
                       finished_at=CASE
                           WHEN attempt_count + 1 >= %s THEN now()
                           ELSE NULL
                       END,
                       updated_at=now()
                   WHERE status='running'
                     AND lease_expires_at < now()
                     AND workspace_id=ANY(%s::uuid[])
                     AND judge_provider=%s
                     AND judge_model=%s
                     AND prompt_version=%s""",
                (
                    MAX_JUDGE_ATTEMPTS,
                    MAX_JUDGE_ATTEMPTS,
                    allowed,
                    self.config.provider,
                    self.config.model,
                    self.config.prompt_version,
                ),
            )
            result = await connection.execute(
                """SELECT * FROM eval_judge_runs
                   WHERE status='queued'
                     AND available_at <= now()
                     AND workspace_id=ANY(%s::uuid[])
                     AND (
                         judge_provider IS NULL
                         OR (
                             judge_provider=%s
                             AND judge_model=%s
                             AND prompt_version=%s
                         )
                     )
                   ORDER BY available_at,created_at,id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1""",
                (
                    allowed,
                    self.config.provider,
                    self.config.model,
                    self.config.prompt_version,
                ),
            )
            job = await result.fetchone()
            if not job:
                return None
            updated = await connection.execute(
                """UPDATE eval_judge_runs
                   SET status='running',
                       lease_owner=%s,
                       lease_expires_at=now()+(%s * interval '1 second'),
                       judge_provider=COALESCE(judge_provider,%s),
                       judge_model=COALESCE(judge_model,%s),
                       prompt_version=COALESCE(prompt_version,%s),
                       error_code=NULL,
                       updated_at=now()
                   WHERE id=%s
                   RETURNING *""",
                (
                    self.worker_id,
                    self.lease_seconds,
                    self.config.provider,
                    self.config.model,
                    self.config.prompt_version,
                    job["id"],
                ),
            )
            return await updated.fetchone()

    async def _assert_authorized(self, job):
        principal = Principal(job["requested_by_issuer"], job["requested_by_subject"])
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                job["workspace_id"],
                Permission.MANAGE_EVALS,
            )

    async def _next_case(self, job):
        principal = Principal(job["requested_by_issuer"], job["requested_by_subject"])
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                job["workspace_id"],
                Permission.MANAGE_EVALS,
            )
            result = await connection.execute(
                """SELECT c.id,c.case_key,c.input_text,
                          s.agent_run_id AS candidate_agent_run_id,
                          r.baseline_eval_run_id,
                          bs.agent_run_id AS baseline_agent_run_id
                   FROM eval_judge_runs j
                   JOIN eval_runs r
                     ON r.id=j.eval_run_id AND r.workspace_id=j.workspace_id
                   JOIN eval_cases c
                     ON c.suite_id=r.suite_id AND c.workspace_id=r.workspace_id
                   JOIN eval_agent_run_sources s
                     ON s.eval_run_id=r.id
                    AND s.case_id=c.id
                    AND s.workspace_id=r.workspace_id
                   LEFT JOIN eval_agent_run_sources bs
                     ON bs.eval_run_id=r.baseline_eval_run_id
                    AND bs.case_id=c.id
                    AND bs.workspace_id=r.workspace_id
                   LEFT JOIN eval_judge_case_scores score
                     ON score.judge_run_id=j.id AND score.case_id=c.id
                   WHERE j.id=%s AND j.workspace_id=%s
                     AND score.case_id IS NULL
                   ORDER BY c.case_no
                   LIMIT 1""",
                (job["id"], job["workspace_id"]),
            )
            case = await result.fetchone()
            if case is None:
                return None
            if case["baseline_eval_run_id"] is not None and case["baseline_agent_run_id"] is None:
                raise JudgeExecutionError("judge_baseline_source_unavailable")

            candidate_output = await self._source_output(
                connection,
                job,
                case["candidate_agent_run_id"],
                case["input_text"],
            )
            baseline_output = None
            if case["baseline_agent_run_id"] is not None:
                baseline_output = await self._source_output(
                    connection,
                    job,
                    case["baseline_agent_run_id"],
                    case["input_text"],
                )
            return {
                "id": case["id"],
                "input_text": case["input_text"],
                "candidate_output": candidate_output,
                "baseline_output": baseline_output,
            }

    async def _source_output(self, connection, job, agent_run_id, expected_input):
        run_result = await connection.execute(
            """SELECT id,workspace_id,agent_id,trace_id,status,failure_code,input_text
               FROM agent_runs
               WHERE workspace_id=%s AND id=%s
                 AND requested_by_issuer=%s AND requested_by_subject=%s
               FOR SHARE""",
            (
                job["workspace_id"],
                agent_run_id,
                job["requested_by_issuer"],
                job["requested_by_subject"],
            ),
        )
        run = await run_result.fetchone()
        if not run or run["status"] != "succeeded" or run["input_text"] != expected_input:
            raise JudgeExecutionError("judge_source_unavailable")
        steps_result = await connection.execute(
            """SELECT step_no,provider,model,response
               FROM agent_model_steps
               WHERE workspace_id=%s AND run_id=%s
               ORDER BY step_no LIMIT 33""",
            (job["workspace_id"], agent_run_id),
        )
        try:
            terminal = summarize_result(run, await steps_result.fetchall())
        except (ValueError, KeyError, TypeError) as exc:
            raise JudgeExecutionError("judge_source_unavailable") from exc
        if terminal.output_text is None or len(terminal.output_text) > 50000:
            raise JudgeExecutionError("judge_source_unavailable")
        return terminal.output_text

    async def _score(self, job, case_id, task, answer, target):
        if len(task) + len(answer) > self.config.max_input_chars:
            raise JudgeExecutionError("judge_input_too_large")
        user_payload = json.dumps(
            {
                "task": task,
                "candidate_answer": answer,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request = ProviderRequest(
            request_id=(f"eval-judge:{job['id']}:{case_id}:{target}:{job['attempt_count']}"),
            messages=(
                ProviderMessage("system", JUDGE_SYSTEM_PROMPT),
                ProviderMessage("user", user_payload),
            ),
            response_schema=JUDGE_RESPONSE_SCHEMA,
            max_output_tokens=self.config.max_output_tokens,
        )
        started = time.perf_counter()
        response = await self.adapter.generate(
            model=self.config.model,
            request=request,
            timeout_seconds=self.config.timeout_seconds,
        )
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        if (
            response.finish_reason != "stop"
            or response.tool_calls
            or response.structured_output is None
        ):
            raise JudgeExecutionError("judge_invalid_response")
        try:
            scores = JudgeScores.model_validate(response.structured_output)
        except ValidationError as exc:
            raise JudgeExecutionError("judge_invalid_response") from exc
        return (
            scores,
            (response.usage.input_tokens, response.usage.output_tokens),
            latency_ms,
        )

    async def _heartbeat(self, job_id: UUID, stop: asyncio.Event):
        interval = max(1.0, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                async with self.connection() as connection:
                    renewed = await connection.execute(
                        """UPDATE eval_judge_runs
                           SET lease_expires_at=now()+(%s * interval '1 second'),
                               updated_at=now()
                           WHERE id=%s AND status='running' AND lease_owner=%s
                             AND lease_expires_at > now()""",
                        (self.lease_seconds, job_id, self.worker_id),
                    )
                    if renewed.rowcount != 1:
                        return

    async def _owned(self, connection, job_id: UUID):
        result = await connection.execute(
            "SELECT * FROM eval_judge_runs WHERE id=%s FOR UPDATE",
            (job_id,),
        )
        row = await result.fetchone()
        if not row:
            return None
        now_result = await connection.execute("SELECT now() AS now")
        now: datetime = (await now_result.fetchone())["now"]
        if (
            row["status"] != "running"
            or row["lease_owner"] != self.worker_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            return None
        return row

    async def _save_case(
        self,
        job,
        case_id: UUID,
        candidate: JudgeScores,
        baseline: JudgeScores | None,
        *,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ):
        async with self.connection() as connection:
            owned = await self._owned(connection, job["id"])
            if not owned:
                return False
            await connection.execute(
                """INSERT INTO eval_judge_case_scores
                   (judge_run_id,workspace_id,eval_run_id,case_id,
                    task_completion,answer_relevance,clarity,rationale,
                    baseline_task_completion,baseline_answer_relevance,
                    baseline_clarity,input_tokens,output_tokens,latency_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (judge_run_id,case_id) DO NOTHING""",
                (
                    job["id"],
                    job["workspace_id"],
                    job["eval_run_id"],
                    case_id,
                    candidate.task_completion,
                    candidate.answer_relevance,
                    candidate.clarity,
                    candidate.rationale,
                    baseline.task_completion if baseline else None,
                    baseline.answer_relevance if baseline else None,
                    baseline.clarity if baseline else None,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                ),
            )
            count_result = await connection.execute(
                """SELECT count(*)::integer AS count
                   FROM eval_judge_case_scores
                   WHERE judge_run_id=%s""",
                (job["id"],),
            )
            count = (await count_result.fetchone())["count"]
            if count == owned["case_count"]:
                await connection.execute(
                    """UPDATE eval_judge_runs
                       SET status='succeeded',lease_owner=NULL,lease_expires_at=NULL,
                           finished_at=now(),updated_at=now(),error_code=NULL
                       WHERE id=%s""",
                    (job["id"],),
                )
            else:
                await connection.execute(
                    """UPDATE eval_judge_runs
                       SET status='queued',available_at=now(),
                           lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                       WHERE id=%s""",
                    (job["id"],),
                )
            return True

    async def _finish_if_complete(self, job):
        async with self.connection() as connection:
            owned = await self._owned(connection, job["id"])
            if not owned:
                return False
            count_result = await connection.execute(
                """SELECT count(*)::integer AS count
                   FROM eval_judge_case_scores
                   WHERE judge_run_id=%s""",
                (job["id"],),
            )
            count = (await count_result.fetchone())["count"]
            if count != owned["case_count"]:
                raise JudgeExecutionError("judge_case_set_incomplete")
            await connection.execute(
                """UPDATE eval_judge_runs
                   SET status='succeeded',lease_owner=NULL,lease_expires_at=NULL,
                       finished_at=now(),updated_at=now(),error_code=NULL
                   WHERE id=%s""",
                (job["id"],),
            )
            return True

    async def _fail(self, job, error_code: str, *, retryable: bool):
        async with self.connection() as connection:
            owned = await self._owned(connection, job["id"])
            if not owned:
                return False
            next_attempt = owned["attempt_count"] + 1
            if retryable and next_attempt < MAX_JUDGE_ATTEMPTS:
                delay = judge_retry_delay(job["id"], next_attempt)
                await connection.execute(
                    """UPDATE eval_judge_runs
                       SET status='queued',attempt_count=%s,
                           available_at=now()+(%s * interval '1 second'),
                           lease_owner=NULL,lease_expires_at=NULL,
                           error_code=%s,updated_at=now()
                       WHERE id=%s""",
                    (next_attempt, delay, error_code[:100], job["id"]),
                )
            else:
                await connection.execute(
                    """UPDATE eval_judge_runs
                       SET status='failed',attempt_count=%s,
                           lease_owner=NULL,lease_expires_at=NULL,
                           error_code=%s,finished_at=now(),updated_at=now()
                       WHERE id=%s""",
                    (next_attempt, error_code[:100], job["id"]),
                )
            return True
