import hashlib
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.config import Settings
from nexora_api.evaluation import (
    EvalCase,
    EvalObservation,
    EvalResult,
    compare_result,
    evaluate_case,
)
from nexora_api.evaluations import (
    AgentRunEvalInput,
    EvalCaseDefinition,
    EvalCaseResult,
    EvalObservationInput,
    EvalRun,
    EvalRunInput,
    EvalRunSummary,
    EvalSuite,
    EvalSuiteInput,
    EvalSuiteSummary,
)
from nexora_api.run_results import summarize_result
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class EvaluationRepository:
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

    @staticmethod
    def _suite_summary(row) -> EvalSuiteSummary:
        return EvalSuiteSummary(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            version=row["version"],
            description=row["description"],
            case_count=row["case_count"],
            created_at=row["created_at"],
        )

    async def _load_suite(self, connection, workspace_id: UUID, suite_id: UUID) -> EvalSuite:
        result = await connection.execute(
            """SELECT s.id,s.workspace_id,s.name,s.version,s.description,s.created_at,
                      count(c.id)::integer AS case_count
               FROM eval_suites s
               LEFT JOIN eval_cases c ON c.suite_id=s.id AND c.workspace_id=s.workspace_id
               WHERE s.workspace_id=%s AND s.id=%s
               GROUP BY s.id""",
            (workspace_id, suite_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(404)
        cases_result = await connection.execute(
            """SELECT id,case_no,case_key,input_text,expected_tools,forbidden_tools,
                      expected_citations
               FROM eval_cases
               WHERE workspace_id=%s AND suite_id=%s
               ORDER BY case_no""",
            (workspace_id, suite_id),
        )
        cases = [
            EvalCaseDefinition(
                id=case["id"],
                case_no=case["case_no"],
                case_key=case["case_key"],
                input=case["input_text"],
                expected_tools=list(case["expected_tools"]),
                forbidden_tools=list(case["forbidden_tools"]),
                expected_citations=list(case["expected_citations"]),
            )
            for case in await cases_result.fetchall()
        ]
        summary = self._suite_summary(row)
        return EvalSuite(**summary.model_dump(), cases=cases)

    async def create_suite(
        self,
        principal,
        workspace_id: UUID,
        body: EvalSuiteInput,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[EvalSuite, bool]:
        request_hash = _fingerprint(body.model_dump(mode="json"))
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            existing_result = await connection.execute(
                """SELECT id,request_hash FROM eval_suites
                   WHERE workspace_id=%s AND created_by_issuer=%s
                     AND created_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409)
                return await self._load_suite(connection, workspace_id, existing["id"]), False

            duplicate = await connection.execute(
                """SELECT id FROM eval_suites
                   WHERE workspace_id=%s AND name=%s AND version=%s""",
                (workspace_id, body.name, body.version),
            )
            if await duplicate.fetchone():
                raise HTTPException(409)

            suite_id = uuid4()
            try:
                await connection.execute(
                    """INSERT INTO eval_suites
                       (id,workspace_id,name,version,description,created_by_issuer,
                        created_by_subject,request_hash,idempotency_key)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        suite_id,
                        workspace_id,
                        body.name,
                        body.version,
                        body.description,
                        principal.issuer,
                        principal.subject,
                        request_hash,
                        idempotency_key,
                    ),
                )
                for case_no, case in enumerate(body.cases, start=1):
                    await connection.execute(
                        """INSERT INTO eval_cases
                           (id,workspace_id,suite_id,case_no,case_key,input_text,
                            expected_tools,forbidden_tools,expected_citations)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            uuid4(),
                            workspace_id,
                            suite_id,
                            case_no,
                            case.case_key,
                            case.input,
                            sorted(case.expected_tools),
                            sorted(case.forbidden_tools),
                            sorted(case.expected_citations),
                        ),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(409) from exc

            await self.workspaces.audit(
                connection, principal, workspace_id, "eval_suite.created", request_id
            )
            return await self._load_suite(connection, workspace_id, suite_id), True

    async def list_suites(
        self,
        principal,
        workspace_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> list[EvalSuiteSummary]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT s.id,s.workspace_id,s.name,s.version,s.description,s.created_at,
                          count(c.id)::integer AS case_count
                   FROM eval_suites s
                   LEFT JOIN eval_cases c ON c.suite_id=s.id AND c.workspace_id=s.workspace_id
                   WHERE s.workspace_id=%s AND (%s::uuid IS NULL OR s.id > %s::uuid)
                   GROUP BY s.id
                   ORDER BY s.id LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            return [self._suite_summary(row) for row in await result.fetchall()]

    async def get_suite(self, principal, workspace_id: UUID, suite_id: UUID) -> EvalSuite:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            return await self._load_suite(connection, workspace_id, suite_id)

    @staticmethod
    def _canonical_run_input(suite_id: UUID, body: EvalRunInput) -> dict[str, object]:
        observations = [
            {
                "case_key": observation.case_key,
                "selected_tools": sorted(observation.selected_tools),
                "citations": sorted(observation.citations),
                "raw_output": observation.raw_output,
            }
            for observation in sorted(body.observations, key=lambda item: item.case_key)
        ]
        return {
            "suite_id": str(suite_id),
            "candidate_label": body.candidate_label,
            "baseline_eval_run_id": (
                str(body.baseline_eval_run_id) if body.baseline_eval_run_id else None
            ),
            "observations": observations,
        }

    @staticmethod
    def _canonical_agent_run_import(suite_id: UUID, body: AgentRunEvalInput) -> dict[str, object]:
        cases = [
            {"case_key": item.case_key, "agent_run_id": str(item.agent_run_id)}
            for item in sorted(body.cases, key=lambda item: item.case_key)
        ]
        return {
            "kind": "agent_run_import",
            "suite_id": str(suite_id),
            "candidate_label": body.candidate_label,
            "baseline_eval_run_id": (
                str(body.baseline_eval_run_id) if body.baseline_eval_run_id else None
            ),
            "cases": cases,
        }

    async def _load_run(self, connection, workspace_id: UUID, eval_run_id: UUID) -> EvalRun:
        result = await connection.execute(
            """SELECT id,workspace_id,suite_id,candidate_label,baseline_eval_run_id,
                      case_count,passed_count,failed_count,regression_count,
                      improvement_count,created_at
               FROM eval_runs WHERE workspace_id=%s AND id=%s""",
            (workspace_id, eval_run_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(404)
        results_query = await connection.execute(
            """SELECT r.case_id,c.case_key,r.passed,r.failures,r.selected_tools,
                      r.citations,r.raw_output,s.agent_run_id AS source_agent_run_id,
                      r.baseline_passed,r.regression,r.improvement
               FROM eval_case_results r
               JOIN eval_cases c
                 ON c.id=r.case_id AND c.workspace_id=r.workspace_id
               LEFT JOIN eval_agent_run_sources s
                 ON s.eval_run_id=r.eval_run_id
                AND s.case_id=r.case_id
                AND s.workspace_id=r.workspace_id
               WHERE r.workspace_id=%s AND r.eval_run_id=%s
               ORDER BY c.case_no""",
            (workspace_id, eval_run_id),
        )
        results = [
            EvalCaseResult(
                case_id=item["case_id"],
                case_key=item["case_key"],
                passed=item["passed"],
                failures=list(item["failures"]),
                selected_tools=list(item["selected_tools"]),
                citations=list(item["citations"]),
                raw_output=item["raw_output"],
                source_agent_run_id=item["source_agent_run_id"],
                baseline_passed=item["baseline_passed"],
                regression=item["regression"],
                improvement=item["improvement"],
            )
            for item in await results_query.fetchall()
        ]
        return EvalRun(
            id=row["id"],
            workspace_id=row["workspace_id"],
            suite_id=row["suite_id"],
            candidate_label=row["candidate_label"],
            baseline_eval_run_id=row["baseline_eval_run_id"],
            case_count=row["case_count"],
            passed_count=row["passed_count"],
            failed_count=row["failed_count"],
            regression_count=row["regression_count"],
            improvement_count=row["improvement_count"],
            created_at=row["created_at"],
            results=results,
        )

    async def create_run(
        self,
        principal,
        workspace_id: UUID,
        suite_id: UUID,
        body: EvalRunInput,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[EvalRun, bool]:
        request_hash = _fingerprint(self._canonical_run_input(suite_id, body))
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            existing_result = await connection.execute(
                """SELECT id,request_hash FROM eval_runs
                   WHERE workspace_id=%s AND created_by_issuer=%s
                     AND created_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409)
                return await self._load_run(connection, workspace_id, existing["id"]), False

            cases_result = await connection.execute(
                """SELECT id,case_key,expected_tools,forbidden_tools,expected_citations
                   FROM eval_cases
                   WHERE workspace_id=%s AND suite_id=%s
                   ORDER BY case_no""",
                (workspace_id, suite_id),
            )
            cases = await cases_result.fetchall()
            if not cases:
                raise HTTPException(404)

            observations = {item.case_key: item for item in body.observations}
            expected_keys = {case["case_key"] for case in cases}
            if set(observations) != expected_keys:
                raise HTTPException(422)

            baseline: dict[UUID, EvalResult] = {}
            if body.baseline_eval_run_id:
                baseline_run = await connection.execute(
                    """SELECT id FROM eval_runs
                       WHERE workspace_id=%s AND id=%s AND suite_id=%s""",
                    (workspace_id, body.baseline_eval_run_id, suite_id),
                )
                if not await baseline_run.fetchone():
                    raise HTTPException(404)
                baseline_results = await connection.execute(
                    """SELECT r.case_id,c.case_key,r.passed,r.failures
                       FROM eval_case_results r
                       JOIN eval_cases c
                         ON c.id=r.case_id AND c.workspace_id=r.workspace_id
                       WHERE r.workspace_id=%s AND r.eval_run_id=%s""",
                    (workspace_id, body.baseline_eval_run_id),
                )
                for item in await baseline_results.fetchall():
                    baseline[item["case_id"]] = EvalResult(
                        case_id=item["case_key"],
                        passed=item["passed"],
                        failures=tuple(item["failures"]),
                    )
                if len(baseline) != len(cases):
                    raise HTTPException(409)

            evaluated = []
            for case in cases:
                observation_input = observations[case["case_key"]]
                candidate = evaluate_case(
                    EvalCase(
                        id=case["case_key"],
                        expected_tools=frozenset(case["expected_tools"]),
                        forbidden_tools=frozenset(case["forbidden_tools"]),
                        expected_citations=frozenset(case["expected_citations"]),
                    ),
                    EvalObservation(
                        selected_tools=frozenset(observation_input.selected_tools),
                        citations=frozenset(observation_input.citations),
                    ),
                )
                comparison = compare_result(candidate, baseline.get(case["id"]))
                evaluated.append((case, observation_input, candidate, comparison))

            passed_count = sum(candidate.passed for _, _, candidate, _ in evaluated)
            regression_count = sum(comparison.regression for _, _, _, comparison in evaluated)
            improvement_count = sum(comparison.improvement for _, _, _, comparison in evaluated)
            run_id = uuid4()
            await connection.execute(
                """INSERT INTO eval_runs
                   (id,workspace_id,suite_id,candidate_label,baseline_eval_run_id,
                    created_by_issuer,created_by_subject,request_hash,idempotency_key,
                    case_count,passed_count,failed_count,regression_count,improvement_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    workspace_id,
                    suite_id,
                    body.candidate_label,
                    body.baseline_eval_run_id,
                    principal.issuer,
                    principal.subject,
                    request_hash,
                    idempotency_key,
                    len(evaluated),
                    passed_count,
                    len(evaluated) - passed_count,
                    regression_count,
                    improvement_count,
                ),
            )
            for case, observation_input, candidate, comparison in evaluated:
                baseline_result = baseline.get(case["id"])
                await connection.execute(
                    """INSERT INTO eval_case_results
                       (eval_run_id,workspace_id,case_id,passed,failures,selected_tools,
                        citations,raw_output,baseline_passed,regression,improvement)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        run_id,
                        workspace_id,
                        case["id"],
                        candidate.passed,
                        Jsonb(list(candidate.failures)),
                        sorted(observation_input.selected_tools),
                        sorted(observation_input.citations),
                        observation_input.raw_output if not candidate.passed else None,
                        baseline_result.passed if baseline_result else None,
                        comparison.regression,
                        comparison.improvement,
                    ),
                )
            await self.workspaces.audit(
                connection, principal, workspace_id, "eval_run.completed", request_id
            )
            return await self._load_run(connection, workspace_id, run_id), True

    async def create_run_from_agent_runs(
        self,
        principal,
        workspace_id: UUID,
        suite_id: UUID,
        body: AgentRunEvalInput,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[EvalRun, bool]:
        request_hash = _fingerprint(self._canonical_agent_run_import(suite_id, body))
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            existing_result = await connection.execute(
                """SELECT id,request_hash FROM eval_runs
                   WHERE workspace_id=%s AND created_by_issuer=%s
                     AND created_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409)
                return await self._load_run(connection, workspace_id, existing["id"]), False

            cases_result = await connection.execute(
                """SELECT id,case_key,input_text,expected_tools,forbidden_tools,
                          expected_citations
                   FROM eval_cases
                   WHERE workspace_id=%s AND suite_id=%s
                   ORDER BY case_no""",
                (workspace_id, suite_id),
            )
            cases = await cases_result.fetchall()
            if not cases:
                raise HTTPException(404)

            mappings = {item.case_key: item for item in body.cases}
            expected_keys = {case["case_key"] for case in cases}
            if set(mappings) != expected_keys:
                raise HTTPException(422, "eval_cases_must_be_complete")
            if any(case["expected_citations"] for case in cases):
                raise HTTPException(422, "verified_retrieval_provenance_required")

            observations: dict[str, EvalObservationInput] = {}
            source_run_ids: dict[UUID, UUID] = {}
            for case in cases:
                mapping = mappings[case["case_key"]]
                run_result = await connection.execute(
                    """SELECT id,workspace_id,agent_id,trace_id,status,failure_code,input_text
                       FROM agent_runs
                       WHERE workspace_id=%s AND id=%s
                         AND requested_by_issuer=%s AND requested_by_subject=%s
                       FOR SHARE""",
                    (
                        workspace_id,
                        mapping.agent_run_id,
                        principal.issuer,
                        principal.subject,
                    ),
                )
                source_run = await run_result.fetchone()
                if not source_run:
                    raise HTTPException(404)
                if source_run["status"] != "succeeded":
                    raise HTTPException(409, "agent_run_not_evaluable")
                if source_run["input_text"] != case["input_text"]:
                    raise HTTPException(422, "agent_run_input_mismatch")

                steps_result = await connection.execute(
                    """SELECT step_no,provider,model,response
                       FROM agent_model_steps
                       WHERE workspace_id=%s AND run_id=%s
                       ORDER BY step_no LIMIT 33""",
                    (workspace_id, mapping.agent_run_id),
                )
                try:
                    terminal = summarize_result(source_run, await steps_result.fetchall())
                except (ValueError, KeyError, TypeError) as exc:
                    raise HTTPException(409, "agent_run_result_unavailable") from exc

                if len(terminal.selected_tools) > 32:
                    raise HTTPException(422, "agent_run_tool_set_exceeds_eval_limit")
                if terminal.output_text is None or len(terminal.output_text) > 50000:
                    raise HTTPException(422, "agent_run_output_exceeds_eval_limit")
                observations[case["case_key"]] = EvalObservationInput(
                    case_key=case["case_key"],
                    selected_tools=terminal.selected_tools,
                    citations=[],
                    raw_output=terminal.output_text,
                )
                source_run_ids[case["id"]] = mapping.agent_run_id

            baseline: dict[UUID, EvalResult] = {}
            if body.baseline_eval_run_id:
                baseline_run = await connection.execute(
                    """SELECT id FROM eval_runs
                       WHERE workspace_id=%s AND id=%s AND suite_id=%s""",
                    (workspace_id, body.baseline_eval_run_id, suite_id),
                )
                if not await baseline_run.fetchone():
                    raise HTTPException(404)
                baseline_results = await connection.execute(
                    """SELECT r.case_id,c.case_key,r.passed,r.failures
                       FROM eval_case_results r
                       JOIN eval_cases c
                         ON c.id=r.case_id AND c.workspace_id=r.workspace_id
                       WHERE r.workspace_id=%s AND r.eval_run_id=%s""",
                    (workspace_id, body.baseline_eval_run_id),
                )
                for item in await baseline_results.fetchall():
                    baseline[item["case_id"]] = EvalResult(
                        case_id=item["case_key"],
                        passed=item["passed"],
                        failures=tuple(item["failures"]),
                    )
                if len(baseline) != len(cases):
                    raise HTTPException(409)

            evaluated = []
            for case in cases:
                observation_input = observations[case["case_key"]]
                candidate = evaluate_case(
                    EvalCase(
                        id=case["case_key"],
                        expected_tools=frozenset(case["expected_tools"]),
                        forbidden_tools=frozenset(case["forbidden_tools"]),
                        expected_citations=frozenset(),
                    ),
                    EvalObservation(
                        selected_tools=frozenset(observation_input.selected_tools),
                        citations=frozenset(),
                    ),
                )
                comparison = compare_result(candidate, baseline.get(case["id"]))
                evaluated.append((case, observation_input, candidate, comparison))

            passed_count = sum(candidate.passed for _, _, candidate, _ in evaluated)
            regression_count = sum(comparison.regression for _, _, _, comparison in evaluated)
            improvement_count = sum(comparison.improvement for _, _, _, comparison in evaluated)
            run_id = uuid4()
            await connection.execute(
                """INSERT INTO eval_runs
                   (id,workspace_id,suite_id,candidate_label,baseline_eval_run_id,
                    created_by_issuer,created_by_subject,request_hash,idempotency_key,
                    case_count,passed_count,failed_count,regression_count,improvement_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    workspace_id,
                    suite_id,
                    body.candidate_label,
                    body.baseline_eval_run_id,
                    principal.issuer,
                    principal.subject,
                    request_hash,
                    idempotency_key,
                    len(evaluated),
                    passed_count,
                    len(evaluated) - passed_count,
                    regression_count,
                    improvement_count,
                ),
            )
            for case, observation_input, candidate, comparison in evaluated:
                baseline_result = baseline.get(case["id"])
                await connection.execute(
                    """INSERT INTO eval_case_results
                       (eval_run_id,workspace_id,case_id,passed,failures,selected_tools,
                        citations,raw_output,baseline_passed,regression,improvement)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        run_id,
                        workspace_id,
                        case["id"],
                        candidate.passed,
                        Jsonb(list(candidate.failures)),
                        sorted(observation_input.selected_tools),
                        [],
                        observation_input.raw_output if not candidate.passed else None,
                        baseline_result.passed if baseline_result else None,
                        comparison.regression,
                        comparison.improvement,
                    ),
                )
                await connection.execute(
                    """INSERT INTO eval_agent_run_sources
                       (eval_run_id,workspace_id,case_id,agent_run_id)
                       VALUES (%s,%s,%s,%s)""",
                    (run_id, workspace_id, case["id"], source_run_ids[case["id"]]),
                )
            await self.workspaces.audit(
                connection, principal, workspace_id, "eval_run.imported", request_id
            )
            return await self._load_run(connection, workspace_id, run_id), True

    async def list_runs(
        self,
        principal,
        workspace_id: UUID,
        suite_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> list[EvalRunSummary]:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            suite = await connection.execute(
                "SELECT id FROM eval_suites WHERE workspace_id=%s AND id=%s",
                (workspace_id, suite_id),
            )
            if not await suite.fetchone():
                raise HTTPException(404)

            boundary = None
            if cursor is not None:
                anchor = await connection.execute(
                    """SELECT created_at,id FROM eval_runs
                       WHERE workspace_id=%s AND suite_id=%s AND id=%s""",
                    (workspace_id, suite_id, cursor),
                )
                boundary = await anchor.fetchone()
                if not boundary:
                    raise HTTPException(404)

            # Immutable creation time plus UUID gives stable, newest-first keyset pagination.
            result = await connection.execute(
                """SELECT id,workspace_id,suite_id,candidate_label,baseline_eval_run_id,
                          case_count,passed_count,failed_count,regression_count,
                          improvement_count,created_at
                   FROM eval_runs
                   WHERE workspace_id=%s AND suite_id=%s
                     AND (%s::timestamptz IS NULL OR (created_at,id) < (%s,%s::uuid))
                   ORDER BY created_at DESC,id DESC LIMIT %s""",
                (
                    workspace_id,
                    suite_id,
                    boundary["created_at"] if boundary else None,
                    boundary["created_at"] if boundary else None,
                    boundary["id"] if boundary else None,
                    limit,
                ),
            )
            return [EvalRunSummary(**row) for row in await result.fetchall()]

    async def get_run(self, principal, workspace_id: UUID, eval_run_id: UUID) -> EvalRun:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_EVALS
            )
            return await self._load_run(connection, workspace_id, eval_run_id)
