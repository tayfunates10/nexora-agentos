import hashlib
from dataclasses import asdict, dataclass

from psycopg.types.json import Jsonb

from nexora_api.execution_fence import executable_run
from nexora_api.model_routing import ProviderResponse, ProviderToolCall, ProviderUsage
from nexora_api.rag import build_untrusted_context, sha256_text
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


@dataclass(frozen=True, slots=True)
class RunRetrievalSnapshot:
    context_text: str
    embedding_input_tokens: int
    chunk_count: int


class ExecutorStore(RunStateStore):
    async def check(self, context):
        async with self.connection() as connection:
            return await executable_run(connection, context)

    async def load_retrieval(self, context):
        async with self.connection() as connection:
            await executable_run(connection, context)
            result = await connection.execute(
                """SELECT query_hash,context_text,context_hash,
                          embedding_input_tokens,chunk_count
                   FROM agent_run_retrievals
                   WHERE workspace_id=%s AND run_id=%s""",
                (context.workspace_id, context.run_id),
            )
            row = await result.fetchone()
            if row is None:
                return None
            expected_query_hash = hashlib.sha256(context.input_text.encode("utf-8")).hexdigest()
            if row["query_hash"] != expected_query_hash:
                raise RuntimeError("retrieval snapshot query mismatch")
            if hashlib.sha256(row["context_text"].encode("utf-8")).hexdigest() != row["context_hash"]:
                raise RuntimeError("retrieval snapshot context hash mismatch")
            return RunRetrievalSnapshot(
                context_text=row["context_text"],
                embedding_input_tokens=row["embedding_input_tokens"],
                chunk_count=row["chunk_count"],
            )

    async def save_retrieval(self, context, result):
        context_text = build_untrusted_context(result.chunks) if result.chunks else ""
        query_hash = hashlib.sha256(context.input_text.encode("utf-8")).hexdigest()
        context_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        async with self.connection() as connection:
            await executable_run(connection, context)
            inserted = await connection.execute(
                """INSERT INTO agent_run_retrievals
                   (run_id,workspace_id,query_hash,context_text,context_hash,
                    embedding_input_tokens,chunk_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO NOTHING""",
                (
                    context.run_id,
                    context.workspace_id,
                    query_hash,
                    context_text,
                    context_hash,
                    result.embedding_input_tokens,
                    len(result.chunks),
                ),
            )
            if inserted.rowcount == 1:
                for position, chunk in enumerate(result.chunks):
                    await connection.execute(
                        """INSERT INTO agent_run_retrieval_chunks
                           (run_id,workspace_id,position,chunk_id,source_id,source_key,
                            source_version,chunk_index,content_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            context.run_id,
                            context.workspace_id,
                            position,
                            chunk.id,
                            chunk.source_id,
                            chunk.source_key,
                            chunk.source_version,
                            chunk.chunk_index,
                            sha256_text(chunk.content),
                        ),
                    )
                await append_run_event(
                    connection,
                    context.workspace_id,
                    context.run_id,
                    "retrieval.completed",
                    {
                        "chunk_count": len(result.chunks),
                        "embedding_input_tokens": result.embedding_input_tokens,
                    },
                )
            current = await connection.execute(
                """SELECT query_hash,context_text,context_hash,
                          embedding_input_tokens,chunk_count
                   FROM agent_run_retrievals
                   WHERE workspace_id=%s AND run_id=%s""",
                (context.workspace_id, context.run_id),
            )
            row = await current.fetchone()
            if row is None or row["query_hash"] != query_hash or row["context_hash"] != context_hash:
                raise RuntimeError("retrieval snapshot conflict")
            return RunRetrievalSnapshot(
                context_text=row["context_text"],
                embedding_input_tokens=row["embedding_input_tokens"],
                chunk_count=row["chunk_count"],
            )

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
