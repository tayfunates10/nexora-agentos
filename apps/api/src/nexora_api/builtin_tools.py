from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from nexora_api.config import Settings
from nexora_api.tool_registry import SideEffect, ToolExecutionContext, ToolRegistry, ToolSpec


class RunStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID


class RunStatusOutput(BaseModel):
    run_id: UUID
    status: str
    attempt_count: int
    failure_code: str | None = None


def build_tool_registry(settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()

    @asynccontextmanager
    async def connection():
        async with await psycopg.AsyncConnection.connect(
            settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as value:
            yield value

    async def run_status(context: ToolExecutionContext, arguments: BaseModel):
        body = RunStatusInput.model_validate(arguments)
        async with connection() as database:
            result = await database.execute(
                """SELECT id,status,attempt_count,failure_code
                   FROM agent_runs WHERE workspace_id=%s AND id=%s""",
                (context.workspace_id, body.run_id),
            )
            row = await result.fetchone()
            if not row:
                from nexora_api.tool_registry import ToolExecutionError

                raise ToolExecutionError("run_not_found")
            return RunStatusOutput(
                run_id=row["id"],
                status=row["status"],
                attempt_count=row["attempt_count"],
                failure_code=row["failure_code"],
            )

    registry.register(
        ToolSpec(
            name="run.status.read",
            description=(
                "Read the current status of a run in the same workspace. "
                "This tool is read-only and cannot change run state."
            ),
            input_model=RunStatusInput,
            output_model=RunStatusOutput,
            side_effect=SideEffect.READ,
            handler=run_status,
        )
    )
    return registry
