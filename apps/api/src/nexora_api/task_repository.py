import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.execution_fence import executable_run
from nexora_api.run_state import ExecutionContext
from nexora_api.runtime_events import append_run_event
from nexora_api.tasks import RunTask, RunTaskEvidence
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


@dataclass
class RunTaskError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


class RunTaskRepository:
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

    async def _view(self, connection, row) -> RunTask:
        dependencies = await connection.execute(
            "SELECT depends_on_task_id FROM run_task_dependencies WHERE task_id=%s ORDER BY depends_on_task_id",
            (row["id"],),
        )
        evidence = await connection.execute(
            """SELECT id,verification_tool_name,summary,satisfied,created_at
               FROM run_task_evidence WHERE task_id=%s ORDER BY created_at,id""",
            (row["id"],),
        )
        return RunTask(
            id=row["id"], workspace_id=row["workspace_id"], run_id=row["run_id"],
            parent_task_id=row["parent_task_id"], kind=row["kind"], title=row["title"],
            description=row["description"], status=row["status"],
            action_tool_name=row["action_tool_name"],
            verification_state=row["verification_state"],
            dependencies=[item["depends_on_task_id"] for item in await dependencies.fetchall()],
            evidence=[RunTaskEvidence(**item) for item in await evidence.fetchall()],
            created_at=row["created_at"], updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    async def list_tasks(self, principal: Principal, workspace_id: UUID, run_id: UUID,
                         limit: int, cursor: UUID | None) -> list[RunTask]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            run_result = await connection.execute(
                "SELECT id FROM agent_runs WHERE id=%s AND workspace_id=%s", (run_id, workspace_id)
            )
            if await run_result.fetchone() is None:
                raise HTTPException(404)
            result = await connection.execute(
                """SELECT * FROM run_tasks WHERE workspace_id=%s AND run_id=%s
                   AND (%s::uuid IS NULL OR id > %s::uuid) ORDER BY id LIMIT %s""",
                (workspace_id, run_id, cursor, cursor, limit),
            )
            return [await self._view(connection, row) for row in await result.fetchall()]

    @staticmethod
    def _request_hash(document: dict[str, Any]) -> str:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _uuid(value: object, code: str) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise RunTaskError(code) from None

    @staticmethod
    def _internal_tool(context: ExecutionContext, public_name: str) -> str:
        if context.allowed_tools is not None and public_name not in context.allowed_tools:
            raise RunTaskError("task_tool_not_allowed")
        return (context.tool_aliases or {}).get(public_name, public_name)

    async def create_follow_up(self, context: ExecutionContext,
                               arguments: dict[str, object]) -> dict[str, object]:
        title = str(arguments.get("title", "")).strip()
        description = str(arguments.get("description", "")).strip()
        action_tool = arguments.get("action_tool")
        action_tool = str(action_tool) if action_tool is not None else None
        idempotency_key = str(arguments.get("idempotency_key", ""))
        raw_dependencies = arguments.get("depends_on", [])
        if not isinstance(raw_dependencies, list):
            raise RunTaskError("task_dependencies_invalid")
        dependencies = [self._uuid(value, "task_dependency_invalid") for value in raw_dependencies]
        if len(dependencies) != len(set(dependencies)):
            raise RunTaskError("task_dependency_duplicate")
        request_hash = self._request_hash({
            "title": title, "description": description, "action_tool": action_tool,
            "depends_on": sorted(str(value) for value in dependencies),
        })

        async with self.connection() as connection:
            await executable_run(connection, context)
            existing_result = await connection.execute(
                "SELECT * FROM run_tasks WHERE run_id=%s AND idempotency_key=%s FOR UPDATE",
                (context.run_id, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise RunTaskError("task_idempotency_conflict")
                return {"task_id": str(existing["id"]), "status": existing["status"],
                        "verification_state": existing["verification_state"]}

            root_result = await connection.execute(
                "SELECT id FROM run_tasks WHERE workspace_id=%s AND run_id=%s AND kind='goal' FOR SHARE",
                (context.workspace_id, context.run_id),
            )
            root = await root_result.fetchone()
            if root is None:
                raise RunTaskError("task_goal_missing")

            if action_tool is not None:
                internal = self._internal_tool(context, action_tool)
                tool_result = await connection.execute(
                    """SELECT server_key,side_effect,enabled FROM tool_definitions
                       WHERE workspace_id=%s AND name=%s""",
                    (context.workspace_id, internal),
                )
                tool = await tool_result.fetchone()
                if (tool is None or not tool["enabled"] or tool["server_key"] == "tasks"
                        or tool["side_effect"] == "read"):
                    raise RunTaskError("task_action_tool_invalid")

            if dependencies:
                rows = await connection.execute(
                    """SELECT id FROM run_tasks WHERE workspace_id=%s AND run_id=%s
                       AND id=ANY(%s::uuid[])""",
                    (context.workspace_id, context.run_id, dependencies),
                )
                if {row["id"] for row in await rows.fetchall()} != set(dependencies):
                    raise RunTaskError("task_dependency_cross_run")

            task_id = uuid4()
            await connection.execute(
                """INSERT INTO run_tasks
                   (id,workspace_id,run_id,parent_task_id,kind,title,description,status,
                    action_tool_name,verification_state,idempotency_key,request_hash)
                   VALUES (%s,%s,%s,%s,'follow_up',%s,%s,'planned',%s,%s,%s,%s)""",
                (task_id, context.workspace_id, context.run_id, root["id"], title, description,
                 action_tool, "pending" if action_tool else "not_required",
                 idempotency_key, request_hash),
            )
            for dependency in dependencies:
                await connection.execute(
                    """INSERT INTO run_task_dependencies
                       (workspace_id,run_id,task_id,depends_on_task_id) VALUES (%s,%s,%s,%s)""",
                    (context.workspace_id, context.run_id, task_id, dependency),
                )
            await append_run_event(connection, context.workspace_id, context.run_id, "task.created",
                {"task_id": str(task_id), "parent_task_id": str(root["id"]),
                 "action_tool": action_tool})
            return {"task_id": str(task_id), "status": "planned",
                    "verification_state": "pending" if action_tool else "not_required"}

    async def verify(self, context: ExecutionContext,
                     arguments: dict[str, object]) -> dict[str, object]:
        task_id = self._uuid(arguments.get("task_id"), "task_id_invalid")
        verification_tool = str(arguments.get("verification_tool", ""))
        summary = str(arguments.get("summary", "")).strip()
        satisfied = arguments.get("satisfied")
        if not isinstance(satisfied, bool):
            raise RunTaskError("task_verification_invalid")
        idempotency_key = str(arguments.get("idempotency_key", ""))

        async with self.connection() as connection:
            await executable_run(connection, context)
            duplicate = await connection.execute(
                "SELECT task_id,satisfied FROM run_task_evidence WHERE run_id=%s AND idempotency_key=%s",
                (context.run_id, idempotency_key),
            )
            previous = await duplicate.fetchone()
            if previous is not None:
                if previous["task_id"] != task_id or previous["satisfied"] != satisfied:
                    raise RunTaskError("task_verification_idempotency_conflict")
                row_result = await connection.execute(
                    "SELECT status,verification_state FROM run_tasks WHERE id=%s", (task_id,)
                )
                row = await row_result.fetchone()
                return {"task_id": str(task_id), "status": row["status"],
                        "verification_state": row["verification_state"]}

            task_result = await connection.execute(
                """SELECT * FROM run_tasks WHERE id=%s AND workspace_id=%s AND run_id=%s
                   AND kind='follow_up' FOR UPDATE""",
                (task_id, context.workspace_id, context.run_id),
            )
            task = await task_result.fetchone()
            if task is None:
                raise RunTaskError("task_not_found")
            if task["status"] in ("succeeded", "failed", "cancelled"):
                raise RunTaskError("task_already_terminal")

            incomplete = await connection.execute(
                """SELECT 1 FROM run_task_dependencies d JOIN run_tasks t ON t.id=d.depends_on_task_id
                   WHERE d.task_id=%s AND t.status <> 'succeeded' LIMIT 1""", (task_id,)
            )
            if await incomplete.fetchone() is not None:
                raise RunTaskError("task_dependency_incomplete")

            action_call_id = None
            lower_bound = task["created_at"]
            if task["action_tool_name"] is not None:
                internal_action = self._internal_tool(context, task["action_tool_name"])
                action_result = await connection.execute(
                    """SELECT c.id,c.finished_at FROM tool_calls c JOIN tool_definitions t
                       ON t.id=c.tool_id AND t.workspace_id=c.workspace_id
                       WHERE c.workspace_id=%s AND c.run_id=%s AND t.name=%s
                       AND t.side_effect <> 'read' AND c.status='succeeded'
                       AND c.finished_at >= %s ORDER BY c.finished_at DESC,c.id DESC LIMIT 1""",
                    (context.workspace_id, context.run_id, internal_action, task["created_at"]),
                )
                action_call = await action_result.fetchone()
                if action_call is None:
                    raise RunTaskError("task_action_not_observed")
                action_call_id = action_call["id"]
                lower_bound = action_call["finished_at"]

            internal_verification = self._internal_tool(context, verification_tool)
            verification_result = await connection.execute(
                """SELECT c.id FROM tool_calls c JOIN tool_definitions t
                   ON t.id=c.tool_id AND t.workspace_id=c.workspace_id
                   WHERE c.workspace_id=%s AND c.run_id=%s AND t.name=%s
                   AND t.side_effect='read' AND c.status='succeeded' AND c.finished_at >= %s
                   ORDER BY c.finished_at DESC,c.id DESC LIMIT 1""",
                (context.workspace_id, context.run_id, internal_verification, lower_bound),
            )
            verification_call = await verification_result.fetchone()
            if verification_call is None:
                raise RunTaskError("task_verification_evidence_missing")

            evidence_id = uuid4()
            await connection.execute(
                """INSERT INTO run_task_evidence
                   (id,workspace_id,run_id,task_id,action_tool_call_id,verification_tool_call_id,
                    verification_tool_name,summary,satisfied,idempotency_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (evidence_id, context.workspace_id, context.run_id, task_id, action_call_id,
                 verification_call["id"], verification_tool, summary, satisfied, idempotency_key),
            )
            status = "succeeded" if satisfied else "failed"
            verification_state = "verified" if satisfied else "failed"
            await connection.execute(
                """UPDATE run_tasks SET status=%s,verification_state=%s,completed_at=now(),
                   updated_at=now() WHERE id=%s""", (status, verification_state, task_id)
            )
            await append_run_event(connection, context.workspace_id, context.run_id,
                "task.verified" if satisfied else "task.verification_failed",
                {"task_id": str(task_id), "verification_tool": verification_tool,
                 "verification_tool_call_id": str(verification_call["id"])})
            return {"task_id": str(task_id), "status": status,
                    "verification_state": verification_state, "evidence_id": str(evidence_id)}
