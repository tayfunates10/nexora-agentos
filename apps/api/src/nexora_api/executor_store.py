import hashlib
from dataclasses import asdict, dataclass

from psycopg.types.json import Jsonb

from nexora_api.execution_fence import executable_run
from nexora_api.model_costs import record_model_usage_cost
from nexora_api.model_routing import ProviderResponse, ProviderToolCall, ProviderUsage
from nexora_api.rag import build_untrusted_context, sha256_text
from nexora_api.run_state import RunStateStore
from nexora_api.runtime_events import append_run_event
from nexora_api.tool_contracts import ToolContractError


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
            run = await executable_run(connection, context)
            result = await connection.execute(
                """SELECT r.query_hash,r.context_hash,r.embedding_input_tokens,r.chunk_count,
                          c.context_text
                   FROM agent_run_retrievals r
                   LEFT JOIN agent_run_retrieval_context c
                     ON c.run_id=r.run_id AND c.workspace_id=r.workspace_id
                   WHERE r.workspace_id=%s AND r.run_id=%s""",
                (context.workspace_id, context.run_id),
            )
            row = await result.fetchone()
            if row is None:
                return None
            if row["context_text"] is None:
                raise ToolContractError("retrieval_snapshot_unavailable")
            expected_query_hash = hashlib.sha256(context.input_text.encode("utf-8")).hexdigest()
            if row["query_hash"] != expected_query_hash:
                raise ToolContractError("retrieval_snapshot_unavailable")
            if (
                hashlib.sha256(row["context_text"].encode("utf-8")).hexdigest()
                != row["context_hash"]
            ):
                raise ToolContractError("retrieval_snapshot_unavailable")
            if row["chunk_count"]:
                visible = await connection.execute(
                    """SELECT p.position
                       FROM agent_run_retrieval_chunks p
                       JOIN rag_chunks c
                         ON c.id=p.chunk_id AND c.workspace_id=p.workspace_id
                       JOIN rag_sources s
                         ON s.id=c.source_id AND s.workspace_id=c.workspace_id
                       WHERE p.workspace_id=%s AND p.run_id=%s
                         AND s.is_current
                         AND c.source_id=p.source_id
                         AND s.source_key=p.source_key
                         AND c.source_version=p.source_version
                         AND c.chunk_index=p.chunk_index
                         AND c.content_hash=p.content_hash
                         AND (
                             c.access_scope='workspace'
                             OR EXISTS (
                                 SELECT 1 FROM rag_source_acl a
                                 WHERE a.workspace_id=c.workspace_id
                                   AND a.source_id=c.source_id
                                   AND a.issuer=%s AND a.subject=%s
                             )
                         )
                       ORDER BY p.position
                       FOR SHARE OF c,s""",
                    (
                        context.workspace_id,
                        context.run_id,
                        run["requested_by_issuer"],
                        run["requested_by_subject"],
                    ),
                )
                visible_rows = await visible.fetchall()
                if len(visible_rows) != row["chunk_count"]:
                    raise ToolContractError("retrieval_snapshot_unavailable")
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
            run = await executable_run(connection, context)
            if result.chunks:
                chunk_ids = [chunk.id for chunk in result.chunks]
                visible = await connection.execute(
                    """SELECT c.id,c.source_id,s.source_key,c.source_version,
                              c.chunk_index,c.content_hash
                       FROM rag_chunks c
                       JOIN rag_sources s
                         ON s.id=c.source_id AND s.workspace_id=c.workspace_id
                       WHERE c.workspace_id=%s
                         AND c.id=ANY(%s::uuid[])
                         AND s.is_current
                         AND (
                             c.access_scope='workspace'
                             OR EXISTS (
                                 SELECT 1 FROM rag_source_acl a
                                 WHERE a.workspace_id=c.workspace_id
                                   AND a.source_id=c.source_id
                                   AND a.issuer=%s AND a.subject=%s
                             )
                         )
                       FOR SHARE OF c,s""",
                    (
                        context.workspace_id,
                        chunk_ids,
                        run["requested_by_issuer"],
                        run["requested_by_subject"],
                    ),
                )
                current = {item["id"]: item for item in await visible.fetchall()}
                for chunk in result.chunks:
                    row = current.get(chunk.id)
                    if (
                        row is None
                        or row["source_id"] != chunk.source_id
                        or row["source_key"] != chunk.source_key
                        or row["source_version"] != chunk.source_version
                        or row["chunk_index"] != chunk.chunk_index
                        or row["content_hash"] != sha256_text(chunk.content)
                    ):
                        raise ToolContractError("retrieval_snapshot_unavailable")
            inserted = await connection.execute(
                """INSERT INTO agent_run_retrievals
                   (run_id,workspace_id,query_hash,context_hash,
                    embedding_input_tokens,chunk_count)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO NOTHING""",
                (
                    context.run_id,
                    context.workspace_id,
                    query_hash,
                    context_hash,
                    result.embedding_input_tokens,
                    len(result.chunks),
                ),
            )
            if inserted.rowcount == 1:
                await connection.execute(
                    """INSERT INTO agent_run_retrieval_context
                       (run_id,workspace_id,context_text)
                       VALUES (%s,%s,%s)""",
                    (context.run_id, context.workspace_id, context_text),
                )
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
                """SELECT r.query_hash,r.context_hash,r.embedding_input_tokens,r.chunk_count,
                          c.context_text
                   FROM agent_run_retrievals r
                   LEFT JOIN agent_run_retrieval_context c
                     ON c.run_id=r.run_id AND c.workspace_id=r.workspace_id
                   WHERE r.workspace_id=%s AND r.run_id=%s""",
                (context.workspace_id, context.run_id),
            )
            row = await current.fetchone()
            if (
                row is None
                or row["context_text"] is None
                or row["query_hash"] != query_hash
                or row["context_hash"] != context_hash
                or row["context_text"] != context_text
            ):
                raise ToolContractError("retrieval_snapshot_unavailable")
            return RunRetrievalSnapshot(
                context_text=row["context_text"],
                embedding_input_tokens=row["embedding_input_tokens"],
                chunk_count=row["chunk_count"],
            )

    async def record_cost(
        self,
        context,
        step_no,
        decision,
        response,
        provider_request_id,
    ):
        # Cost accounting records a provider call that already happened. It deliberately
        # does not require the execution fence: a worker that loses its lease after the
        # provider responds still incurred a real charge that belongs to this run.
        async with self.connection() as connection:
            return await record_model_usage_cost(
                connection,
                workspace_id=context.workspace_id,
                source_kind="agent_model_step",
                provider_request_id=provider_request_id,
                attempt_count=context.attempt_count,
                run_id=context.run_id,
                step_no=step_no,
                provider=decision.candidate.provider,
                model=decision.candidate.model,
                usage=response.usage,
                pricing=decision.candidate.pricing,
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
