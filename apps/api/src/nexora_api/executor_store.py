from dataclasses import asdict

from psycopg.types.json import Jsonb

from nexora_api.execution_fence import executable_run
from nexora_api.model_routing import ProviderResponse, ProviderToolCall, ProviderUsage
from nexora_api.run_state import RunStateStore
from nexora_api.runtime_events import append_run_event


def decode_response(value):
    return ProviderResponse(
        text=value["text"],
        tool_calls=tuple(ProviderToolCall(**call) for call in value["tool_calls"]),
        structured_output=value["structured_output"],
        usage=ProviderUsage(**value["usage"]),
        finish_reason=value["finish_reason"],
    )


class ExecutorStore(RunStateStore):
    async def check(self, context):
        async with self.connection() as connection:
            return await executable_run(connection, context)

    async def load(self, context, step_no):
        async with self.connection() as connection:
            await executable_run(connection, context)
            result = await connection.execute(
                """SELECT response FROM agent_model_steps
                   WHERE workspace_id=%s AND run_id=%s AND step_no=%s""",
                (context.workspace_id, context.run_id, step_no),
            )
            row = await result.fetchone()
            return decode_response(row["response"]) if row else None

    async def save(self, context, step_no, decision, response):
        async with self.connection() as connection:
            await executable_run(connection, context)
            await connection.execute(
                """INSERT INTO agent_model_steps
                   (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    context.workspace_id,
                    context.run_id,
                    step_no,
                    decision.candidate.provider,
                    decision.candidate.model,
                    decision.reason,
                    Jsonb(asdict(response)),
                ),
            )
            await append_run_event(
                connection,
                context.workspace_id,
                context.run_id,
                "model.completed",
                {
                    "step": step_no,
                    "provider": decision.candidate.provider,
                    "model": decision.candidate.model,
                    "routing_reason": decision.reason,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "finish_reason": response.finish_reason,
                },
            )
