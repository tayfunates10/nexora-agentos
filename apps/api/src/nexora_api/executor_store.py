import hashlib
from dataclasses import asdict, dataclass

from psycopg.types.json import Jsonb

from nexora_api.config import Settings
from nexora_api.execution_fence import executable_run
from nexora_api.metrics import observe_spend_denied
from nexora_api.model_routing import ProviderResponse, ProviderToolCall, ProviderUsage
from nexora_api.rag import build_untrusted_context, sha256_text
from nexora_api.run_state import RunStateStore
from nexora_api.runtime_events import append_run_event
from nexora_api.spend import (
    SpendCategory,
    SpendPolicy,
    SpendPricingError,
    agent_step_source_key,
)
from nexora_api.spend_repository import evaluate_budget, observe_write, record_spend
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
    def __init__(self, settings: Settings, spend: SpendPolicy | None = None):
        super().__init__(settings)
        # Without operator pricing nothing can be costed, so accounting stays off
        # rather than recording a fabricated zero cost.
        self.spend = spend

    async def authorize_spend(self, context):
        """Refuse provider egress once a workspace has spent its budget for the period."""
        if self.spend is None:
            return
        async with self.connection() as connection:
            await executable_run(connection, context)
            decision = await evaluate_budget(connection, context.workspace_id)
            if decision.allowed:
                return
            await append_run_event(
                connection,
                context.workspace_id,
                context.run_id,
                "spend.denied",
                {
                    "limit_micros": decision.limit_micros,
                    "consumed_micros": decision.consumed_micros,
                },
            )
        observe_spend_denied(SpendCategory.AGENT_RUN)
        raise ToolContractError("workspace_budget_exhausted")

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

    async def restore_answer_cache(self, context, step_no, decision, cache_key):
        """Restore a tenant-scoped cached terminal answer into this run's immutable journal."""
        async with self.connection() as connection:
            await executable_run(connection, context)
            result = await connection.execute(
                """SELECT response
                   FROM workspace_answer_cache
                   WHERE workspace_id=%s AND agent_id=%s AND cache_key=%s
                     AND provider=%s AND model=%s AND expires_at > now()
                   FOR UPDATE""",
                (
                    context.workspace_id,
                    context.agent_id,
                    cache_key,
                    decision.candidate.provider,
                    decision.candidate.model,
                ),
            )
            row = await result.fetchone()
            if row is None:
                return None
            response = decode_response(row["response"])
            await connection.execute(
                """INSERT INTO agent_model_steps
                   (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                   VALUES (%s,%s,%s,%s,%s,'workspace_answer_cache',%s)""",
                (
                    context.workspace_id,
                    context.run_id,
                    step_no,
                    decision.candidate.provider,
                    decision.candidate.model,
                    Jsonb(asdict(response)),
                ),
            )
            await connection.execute(
                """UPDATE workspace_answer_cache
                   SET hit_count=hit_count+1,last_used_at=now()
                   WHERE workspace_id=%s AND agent_id=%s AND cache_key=%s""",
                (context.workspace_id, context.agent_id, cache_key),
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
                    "routing_reason": "workspace_answer_cache",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "finish_reason": response.finish_reason,
                    "cost_micros": 0,
                    "cache_hit": True,
                },
            )
            return response

    async def save(
        self,
        context,
        step_no,
        decision,
        response,
        *,
        cache_key=None,
        cache_ttl_seconds=0,
    ):
        cost_micros = None
        if self.spend is not None:
            try:
                cost_micros = self.spend.cost_micros(
                    decision.candidate.provider,
                    decision.candidate.model,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
            except SpendPricingError as exc:
                raise ToolContractError(exc.code) from exc
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
            # The ledger row commits with the step it prices, so a replayed step is
            # never charged twice and a charged call always has its stored response.
            write = None
            if cost_micros is not None:
                write = await record_spend(
                    connection,
                    workspace_id=context.workspace_id,
                    source_key=agent_step_source_key(context.run_id, step_no),
                    category=SpendCategory.AGENT_RUN,
                    provider=decision.candidate.provider,
                    model=decision.candidate.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_micros=cost_micros,
                )
            if cache_key is not None and cache_ttl_seconds > 0:
                cached_response = {
                    **asdict(response),
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
                await connection.execute(
                    """INSERT INTO workspace_answer_cache
                       (workspace_id,agent_id,cache_key,provider,model,response,expires_at)
                       VALUES (%s,%s,%s,%s,%s,%s,now()+(%s * interval '1 second'))
                       ON CONFLICT (workspace_id,agent_id,cache_key) DO UPDATE SET
                         provider=EXCLUDED.provider,
                         model=EXCLUDED.model,
                         response=EXCLUDED.response,
                         last_used_at=now(),
                         expires_at=EXCLUDED.expires_at""",
                    (
                        context.workspace_id,
                        context.agent_id,
                        cache_key,
                        decision.candidate.provider,
                        decision.candidate.model,
                        Jsonb(cached_response),
                        cache_ttl_seconds,
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
                    "cost_micros": cost_micros,
                },
            )
        # Only a ledger row that this attempt actually wrote is counted.
        if write is not None:
            observe_write(decision.candidate.provider, SpendCategory.AGENT_RUN, cost_micros, write)
