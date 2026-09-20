from dataclasses import dataclass

from nexora_api.model_routing import ModelPricing, ProviderUsage

SOURCE_KINDS = frozenset(
    {"agent_model_step", "eval_judge_candidate", "eval_judge_baseline"}
)


class ModelCostError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelCostSummary:
    call_count: int
    priced_call_count: int
    total_usd_picos: int | None
    pricing_complete: bool
    pricing_versions: tuple[str, ...]


async def record_model_usage_cost(
    connection,
    *,
    workspace_id,
    source_kind: str,
    provider_request_id: str,
    attempt_count: int,
    provider: str,
    model: str,
    usage: ProviderUsage,
    pricing: ModelPricing | None,
    run_id=None,
    step_no: int | None = None,
    judge_run_id=None,
    eval_run_id=None,
    case_id=None,
) -> int | None:
    if source_kind not in SOURCE_KINDS:
        raise ValueError("unsupported model cost source")
    if attempt_count < 0:
        raise ValueError("attempt_count cannot be negative")

    version = pricing.version if pricing else None
    input_rate = pricing.input_usd_micros_per_million_tokens if pricing else None
    output_rate = pricing.output_usd_micros_per_million_tokens if pricing else None
    expected = (
        str(workspace_id),
        source_kind,
        provider_request_id,
        attempt_count,
        str(run_id) if run_id else None,
        step_no,
        str(judge_run_id) if judge_run_id else None,
        str(eval_run_id) if eval_run_id else None,
        str(case_id) if case_id else None,
        provider,
        model,
        version,
        usage.input_tokens,
        usage.output_tokens,
        input_rate,
        output_rate,
    )

    inserted = await connection.execute(
        """INSERT INTO model_usage_costs
           (workspace_id,source_kind,provider_request_id,attempt_count,
            run_id,step_no,judge_run_id,eval_run_id,case_id,provider,model,
            pricing_version,input_tokens,output_tokens,
            input_usd_micros_per_million_tokens,
            output_usd_micros_per_million_tokens)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (workspace_id,provider,provider_request_id) DO NOTHING
           RETURNING total_usd_picos""",
        (
            workspace_id,
            source_kind,
            provider_request_id,
            attempt_count,
            run_id,
            step_no,
            judge_run_id,
            eval_run_id,
            case_id,
            provider,
            model,
            version,
            usage.input_tokens,
            usage.output_tokens,
            input_rate,
            output_rate,
        ),
    )
    row = await inserted.fetchone()
    if row is not None:
        value = row["total_usd_picos"]
        return int(value) if value is not None else None

    existing_result = await connection.execute(
        """SELECT workspace_id,source_kind,provider_request_id,attempt_count,
                  run_id,step_no,judge_run_id,eval_run_id,case_id,provider,model,
                  pricing_version,input_tokens,output_tokens,
                  input_usd_micros_per_million_tokens,
                  output_usd_micros_per_million_tokens,total_usd_picos
           FROM model_usage_costs
           WHERE workspace_id=%s AND provider=%s AND provider_request_id=%s""",
        (workspace_id, provider, provider_request_id),
    )
    existing = await existing_result.fetchone()
    if existing is None:
        raise ModelCostError("model_cost_snapshot_unavailable", retryable=True)

    actual = (
        str(existing["workspace_id"]),
        existing["source_kind"],
        existing["provider_request_id"],
        existing["attempt_count"],
        str(existing["run_id"]) if existing["run_id"] else None,
        existing["step_no"],
        str(existing["judge_run_id"]) if existing["judge_run_id"] else None,
        str(existing["eval_run_id"]) if existing["eval_run_id"] else None,
        str(existing["case_id"]) if existing["case_id"] else None,
        existing["provider"],
        existing["model"],
        existing["pricing_version"],
        existing["input_tokens"],
        existing["output_tokens"],
        existing["input_usd_micros_per_million_tokens"],
        existing["output_usd_micros_per_million_tokens"],
    )
    if actual != expected:
        raise ModelCostError("model_cost_snapshot_conflict", retryable=False)
    value = existing["total_usd_picos"]
    return int(value) if value is not None else None


async def load_model_cost_summary(
    connection,
    *,
    workspace_id,
    run_id=None,
    judge_run_id=None,
    minimum_expected_calls: int = 0,
) -> ModelCostSummary:
    if (run_id is None) == (judge_run_id is None):
        raise ValueError("exactly one cost owner must be supplied")
    if minimum_expected_calls < 0:
        raise ValueError("minimum_expected_calls cannot be negative")

    owner_column = "run_id" if run_id is not None else "judge_run_id"
    owner_id = run_id if run_id is not None else judge_run_id
    result = await connection.execute(
        f"""SELECT count(*)::integer AS call_count,
                   count(total_usd_picos)::integer AS priced_call_count,
                   sum(total_usd_picos) AS total_usd_picos,
                   COALESCE(
                       array_agg(DISTINCT pricing_version ORDER BY pricing_version)
                           FILTER (WHERE pricing_version IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS pricing_versions
            FROM model_usage_costs
            WHERE workspace_id=%s AND {owner_column}=%s""",
        (workspace_id, owner_id),
    )
    row = await result.fetchone()
    call_count = row["call_count"]
    priced_call_count = row["priced_call_count"]
    complete = call_count >= minimum_expected_calls and priced_call_count == call_count
    total = row["total_usd_picos"]
    if complete:
        total_usd_picos = int(total) if total is not None else 0
    else:
        total_usd_picos = None
    return ModelCostSummary(
        call_count=call_count,
        priced_call_count=priced_call_count,
        total_usd_picos=total_usd_picos,
        pricing_complete=complete,
        pricing_versions=tuple(row["pricing_versions"]),
    )
