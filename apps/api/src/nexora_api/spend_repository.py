"""Durable spend ledger, period consumption and budget storage.

The ledger is append-only and keyed by a deterministic source key, so a retried or
crash-recovered worker attempt records a priced call exactly once. Budget checks read
the same ledger the worker writes, which keeps enforcement and reporting consistent.
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row, tuple_row

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.metrics import observe_spend, observe_spend_alert
from nexora_api.spend import (
    Budget,
    BudgetDecision,
    BudgetEnforcement,
    BudgetInput,
    SpendAlert,
    SpendCategory,
    SpendCategoryTotal,
    SpendRecord,
    SpendSummary,
    budget_decision,
)
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission

# The accounting period is the UTC calendar month. It is derived from the database
# clock so a worker with a skewed process clock cannot shift a tenant's budget window.
PERIOD_START = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
PERIOD = f"""occurred_at >= {PERIOD_START}
             AND occurred_at < {PERIOD_START} + interval '1 month'"""
PERIOD_DAY = "(date_trunc('month', now() AT TIME ZONE 'UTC'))::date"


@dataclass(frozen=True, slots=True)
class SpendWrite:
    """The result of appending one priced call: was it new, and what did it cross."""

    recorded: bool
    alerts: tuple[int, ...] = ()


def observe_write(provider: str, category: str, cost_micros: int, write: SpendWrite) -> None:
    """Post-commit telemetry for a ledger write. Never called for a replayed unit of work."""
    if not write.recorded:
        return
    observe_spend(provider, category, cost_micros)
    for threshold in write.alerts:
        observe_spend_alert(threshold)


async def period_consumption(connection, workspace_id: UUID) -> dict:
    result = await connection.execute(
        f"""SELECT {PERIOD_START} AS period_start,
                   {PERIOD_START} + interval '1 month' AS period_end,
                   coalesce(sum(cost_micros),0)::bigint AS consumed_micros
            FROM workspace_spend_records
            WHERE workspace_id=%s AND {PERIOD}""",
        (workspace_id,),
    )
    return await result.fetchone()


async def load_budget(connection, workspace_id: UUID) -> dict | None:
    result = await connection.execute(
        """SELECT workspace_id,monthly_limit_micros,enforcement,alert_thresholds,updated_at
           FROM workspace_spend_budgets WHERE workspace_id=%s""",
        (workspace_id,),
    )
    return await result.fetchone()


async def period_alerts(connection, workspace_id: UUID) -> list[SpendAlert]:
    result = await connection.execute(
        f"""SELECT threshold_percent,monthly_limit_micros,consumed_micros,
                   enforcement,created_at
            FROM workspace_spend_alerts
            WHERE workspace_id=%s AND period_start={PERIOD_DAY}
            ORDER BY threshold_percent""",
        (workspace_id,),
    )
    return [SpendAlert(**row) for row in await result.fetchall()]


async def raise_threshold_alerts(connection, workspace_id: UUID) -> tuple[int, ...]:
    """Record every budget threshold this period has now reached.

    The unique constraint on (workspace, period, threshold) is what makes an alert
    happen once: a busy month inserts the same row over and over and keeps one.
    Comparison is integer arithmetic, so a threshold cannot fire a micro early.
    """
    # This helper runs inside whatever transaction is paying for the work, so it
    # reads its own rows rather than trusting that caller's row factory.
    async with connection.cursor(row_factory=tuple_row) as cursor:
        await cursor.execute(
            f"""WITH budget AS (
                    SELECT monthly_limit_micros,enforcement,alert_thresholds
                    FROM workspace_spend_budgets WHERE workspace_id=%s
                ), consumed AS (
                    SELECT coalesce(sum(cost_micros),0)::bigint AS micros
                    FROM workspace_spend_records
                    WHERE workspace_id=%s AND {PERIOD}
                )
                INSERT INTO workspace_spend_alerts
                    (id,workspace_id,period_start,threshold_percent,
                     monthly_limit_micros,consumed_micros,enforcement)
                SELECT gen_random_uuid(),%s,{PERIOD_DAY},threshold,
                       budget.monthly_limit_micros,consumed.micros,budget.enforcement
                FROM budget,consumed,unnest(budget.alert_thresholds) AS threshold
                WHERE consumed.micros * 100 >= threshold::bigint * budget.monthly_limit_micros
                ON CONFLICT (workspace_id,period_start,threshold_percent) DO NOTHING
                RETURNING threshold_percent""",
            (workspace_id, workspace_id, workspace_id),
        )
        return tuple(sorted(row[0] for row in await cursor.fetchall()))


async def evaluate_budget(connection, workspace_id: UUID) -> BudgetDecision:
    """Pre-egress budget decision for one workspace in the current period.

    Concurrent calls in the same workspace each read committed spend, so parallel
    execution can exceed a limit by the cost of the calls already in flight. Profile
    token limits bound that overshoot; the ledger always records what was actually spent.
    """
    budget = await load_budget(connection, workspace_id)
    consumption = await period_consumption(connection, workspace_id)
    return budget_decision(
        budget["monthly_limit_micros"] if budget else None,
        BudgetEnforcement(budget["enforcement"]) if budget else None,
        consumption["consumed_micros"],
    )


async def record_spend(
    connection,
    *,
    workspace_id: UUID,
    source_key: str,
    category: SpendCategory,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
) -> SpendWrite:
    """Append one priced call and record any budget threshold it crosses.

    Both writes share the transaction of the work being paid for, so an alert can
    never exist for spend that was rolled back.
    """
    inserted = await connection.execute(
        """INSERT INTO workspace_spend_records
           (id,workspace_id,source_key,category,provider,model,
            input_tokens,output_tokens,cost_micros)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (workspace_id,source_key) DO NOTHING""",
        (
            uuid4(),
            workspace_id,
            source_key,
            str(category),
            provider,
            model,
            input_tokens,
            output_tokens,
            cost_micros,
        ),
    )
    if inserted.rowcount != 1:
        return SpendWrite(False)
    alerts = await raise_threshold_alerts(connection, workspace_id)
    return SpendWrite(True, alerts)


class SpendRepository:
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

    async def summary(self, principal: Principal, workspace_id: UUID) -> SpendSummary:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            budget = await load_budget(connection, workspace_id)
            consumption = await period_consumption(connection, workspace_id)
            result = await connection.execute(
                f"""SELECT category,
                           count(*)::integer AS call_count,
                           coalesce(sum(input_tokens),0)::bigint AS input_tokens,
                           coalesce(sum(output_tokens),0)::bigint AS output_tokens,
                           coalesce(sum(cost_micros),0)::bigint AS cost_micros
                    FROM workspace_spend_records
                    WHERE workspace_id=%s AND {PERIOD}
                    GROUP BY category
                    ORDER BY category""",
                (workspace_id,),
            )
            categories = [SpendCategoryTotal(**row) for row in await result.fetchall()]
            alerts = await period_alerts(connection, workspace_id)
        decision = budget_decision(
            budget["monthly_limit_micros"] if budget else None,
            BudgetEnforcement(budget["enforcement"]) if budget else None,
            consumption["consumed_micros"],
        )
        return SpendSummary(
            workspace_id=workspace_id,
            period_start=consumption["period_start"],
            period_end=consumption["period_end"],
            consumed_micros=consumption["consumed_micros"],
            monthly_limit_micros=decision.limit_micros,
            enforcement=BudgetEnforcement(budget["enforcement"]) if budget else None,
            remaining_micros=decision.remaining_micros,
            exhausted=not decision.allowed,
            alert_thresholds=list(budget["alert_thresholds"]) if budget else [],
            alerts=alerts,
            categories=categories,
        )

    async def set_budget(
        self,
        principal: Principal,
        workspace_id: UUID,
        body: BudgetInput,
        request_id: str,
    ) -> Budget:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_SPEND
            )
            result = await connection.execute(
                """INSERT INTO workspace_spend_budgets
                   (workspace_id,monthly_limit_micros,enforcement,alert_thresholds,
                    updated_by_issuer,updated_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id) DO UPDATE
                   SET monthly_limit_micros=EXCLUDED.monthly_limit_micros,
                       enforcement=EXCLUDED.enforcement,
                       alert_thresholds=EXCLUDED.alert_thresholds,
                       updated_by_issuer=EXCLUDED.updated_by_issuer,
                       updated_by_subject=EXCLUDED.updated_by_subject,
                       updated_at=now()
                   RETURNING workspace_id,monthly_limit_micros,enforcement,
                             alert_thresholds,updated_at""",
                (
                    workspace_id,
                    body.monthly_limit_micros,
                    str(body.enforcement),
                    body.alert_thresholds,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await result.fetchone()
            await connection.execute(
                """INSERT INTO workspace_spend_budget_events
                   (id,workspace_id,monthly_limit_micros,enforcement,alert_thresholds,
                    actor_issuer,actor_subject,request_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4(),
                    workspace_id,
                    body.monthly_limit_micros,
                    str(body.enforcement),
                    body.alert_thresholds,
                    principal.issuer,
                    principal.subject,
                    request_id,
                ),
            )
            # A tighter limit can put a period past a threshold it never crossed by
            # spending, so the decision is evaluated the moment it is recorded.
            fired = await raise_threshold_alerts(connection, workspace_id)
            await self.workspaces.audit(
                connection, principal, workspace_id, "spend.budget.set", request_id
            )
        for threshold in fired:
            observe_spend_alert(threshold)
        return Budget(**row)

    async def list_records(
        self,
        principal: Principal,
        workspace_id: UUID,
        limit: int,
        cursor: UUID | None,
        category: SpendCategory | None,
    ) -> list[SpendRecord]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            boundary = None
            if cursor is not None:
                anchor = await connection.execute(
                    """SELECT occurred_at,id FROM workspace_spend_records
                       WHERE workspace_id=%s AND id=%s""",
                    (workspace_id, cursor),
                )
                boundary = await anchor.fetchone()
                if not boundary:
                    raise HTTPException(404)
            # Append-only rows plus UUID give stable, newest-first keyset pagination.
            result = await connection.execute(
                """SELECT id,source_key,category,provider,model,
                          input_tokens,output_tokens,cost_micros,occurred_at
                   FROM workspace_spend_records
                   WHERE workspace_id=%s
                     AND (%s::text IS NULL OR category=%s::text)
                     AND (%s::timestamptz IS NULL OR (occurred_at,id) < (%s,%s::uuid))
                   ORDER BY occurred_at DESC,id DESC LIMIT %s""",
                (
                    workspace_id,
                    str(category) if category else None,
                    str(category) if category else None,
                    boundary["occurred_at"] if boundary else None,
                    boundary["occurred_at"] if boundary else None,
                    boundary["id"] if boundary else None,
                    limit,
                ),
            )
            return [SpendRecord(**row) for row in await result.fetchall()]
