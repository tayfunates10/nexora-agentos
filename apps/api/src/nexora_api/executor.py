import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from nexora_api import metrics
from nexora_api.auth import Principal
from nexora_api.model_routing import (
    ModelCapability,
    ModelRouter,
    ProviderAdapter,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTool,
    RoutingRequest,
)
from nexora_api.rag import build_untrusted_context
from nexora_api.spend import SpendLimitExceeded, SpendPricingError, run_retrieval_source_key
from nexora_api.telemetry import record, record_error, span
from nexora_api.tool_contracts import ToolContractError
from nexora_api.worker import RetryableExecutionError, TerminalExecutionError


@dataclass(frozen=True)
class ExecutionProfile:
    allowed_workspaces: frozenset[UUID]
    allowed_providers: frozenset[str]
    allowed_tools: frozenset[str] = frozenset()
    max_steps: int = 8
    max_output_tokens: int = 2048
    max_total_tokens: int = 32000
    timeout_seconds: float = 60
    max_context_chars: int = 100000

    def __post_init__(self):
        if not 1 <= self.max_steps <= 32:
            raise ValueError("max_steps must be between 1 and 32")
        if not 1 <= self.max_output_tokens <= 16384:
            raise ValueError("invalid max_output_tokens")
        if not 1 <= self.max_total_tokens <= 1000000:
            raise ValueError("invalid max_total_tokens")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("invalid timeout_seconds")
        if not 1000 <= self.max_context_chars <= 200000:
            raise ValueError("invalid max_context_chars")


class DurableAgentExecutor:
    def __init__(
        self,
        *,
        store,
        router: ModelRouter,
        adapters: Mapping[str, ProviderAdapter],
        gateway,
        profiles: Mapping[str, ExecutionProfile],
        retriever=None,
    ):
        self.store = store
        self.router = router
        self.adapters = dict(adapters)
        self.gateway = gateway
        self.profiles = dict(profiles)
        self.retriever = retriever

    async def execute(self, context, is_cancelled):
        try:
            await self._execute(context, is_cancelled)
        except ProviderError as exc:
            error = RetryableExecutionError if exc.retryable else TerminalExecutionError
            raise error(exc.code) from exc
        except ToolContractError as exc:
            raise TerminalExecutionError(exc.code) from exc
        except (SpendLimitExceeded, SpendPricingError) as exc:
            raise TerminalExecutionError(exc.code) from exc

    async def _execute(self, context, is_cancelled):
        profile = self.profiles.get(context.model_profile)
        if profile is None or context.workspace_id not in profile.allowed_workspaces:
            raise TerminalExecutionError("model_profile_not_authorized")
        run = await self.store.check(context)
        principal = Principal(
            issuer=run["requested_by_issuer"], subject=run["requested_by_subject"]
        )
        tools = await self.gateway.repository.list_tools(principal, context.workspace_id, 101, None)
        advertised = tuple(
            ProviderTool(t.name, t.description, t.input_schema)
            for t in tools
            if t.enabled
            and (
                t.name in profile.allowed_tools
                or t.name in context.declared_tools
            )
        )
        if len(tools) > 100 or len(advertised) > 32:
            raise TerminalExecutionError("tool_limit_exceeded")
        allowed_names = {tool.name for tool in advertised}
        messages = [
            ProviderMessage("system", context.instructions),
            ProviderMessage("user", context.input_text),
        ]
        if self.retriever:
            initial_chars = sum(len(message.content) for message in messages)
            evidence = await self._retrieve(
                principal,
                context,
                max_evidence_chars=profile.max_context_chars - initial_chars,
            )
            if evidence:
                messages.append(ProviderMessage("user", evidence))
        total_tokens = 0
        for step in range(profile.max_steps):
            if await is_cancelled():
                raise TerminalExecutionError("cancel_requested")
            await self.store.check(context)
            if sum(len(m.content) for m in messages) > profile.max_context_chars:
                raise TerminalExecutionError("context_limit_exceeded")
            response = await self.store.load(context, step)
            if response is None:
                required = {ModelCapability.TEXT}
                if advertised:
                    required.add(ModelCapability.TOOLS)
                decision = self.router.route(
                    RoutingRequest(
                        required_capabilities=frozenset(required),
                        allowed_providers=profile.allowed_providers,
                    )
                )
                adapter = self.adapters.get(decision.candidate.provider)
                if adapter is None:
                    raise TerminalExecutionError("provider_not_configured")
                request = ProviderRequest(
                    request_id=f"{context.run_id}:{step}:{context.attempt_count}",
                    messages=tuple(messages),
                    tools=advertised,
                    max_output_tokens=min(
                        profile.max_output_tokens, profile.max_total_tokens - total_tokens
                    ),
                )
                # Budget is checked before egress, not after billing arrives.
                await self.store.authorize_spend(context)
                response = await self._observed_generate(
                    adapter, decision, request, profile.timeout_seconds, context, step, is_cancelled
                )
                self._validate(response)
                await self.store.save(context, step, decision, response)
            self._validate(response)
            total_tokens += response.usage.input_tokens + response.usage.output_tokens
            if total_tokens > profile.max_total_tokens:
                raise TerminalExecutionError("token_budget_exceeded")
            if response.finish_reason in ("stop", "refusal") and not response.tool_calls:
                if not response.text:
                    raise TerminalExecutionError("empty_model_response")
                return
            if response.finish_reason != "tool_call" or not response.tool_calls:
                raise TerminalExecutionError("model_response_incomplete")
            if total_tokens == profile.max_total_tokens:
                raise TerminalExecutionError("token_budget_exceeded")
            for index, call in enumerate(response.tool_calls):
                if call.name not in allowed_names:
                    raise TerminalExecutionError("model_tool_not_allowed")
                await self.store.check(context)
                key = f"step:{step}:tool:{index}"
                result = await self.gateway.invoke(
                    context, key, call.name, call.arguments, is_cancelled
                )
                # Explicit tool records preserve call identity across adapters. Results are
                # untrusted model input; only the gateway may authorize external actions.
                messages.append(ProviderMessage("assistant", "", tool_call=call))
                messages.append(
                    ProviderMessage(
                        "tool", json.dumps(result, ensure_ascii=False), tool_call_id=call.id
                    )
                )
        raise TerminalExecutionError("step_limit_exceeded")

    async def _retrieve(self, principal, context, *, max_evidence_chars):
        """Persist the exact evidence context once so retries cannot silently change it."""
        started = time.perf_counter()
        with span(
            "agent.retrieval",
            **{
                "nexora.workspace_id": context.workspace_id,
                "nexora.run_id": context.run_id,
            },
        ) as active:
            try:
                snapshot = await self.store.load_retrieval(context)
                if snapshot is None:
                    result = await self.retriever.retrieve(
                        principal,
                        context.workspace_id,
                        query=context.input_text,
                        request_id=f"worker-retrieval:{context.run_id}",
                        spend_key=run_retrieval_source_key(context.run_id),
                    )
                    evidence = build_untrusted_context(result.chunks) if result.chunks else ""
                    if len(evidence) > max_evidence_chars:
                        raise TerminalExecutionError("context_limit_exceeded")
                    snapshot = await self.store.save_retrieval(context, result)
                evidence = snapshot.context_text
                if len(evidence) > max_evidence_chars:
                    raise TerminalExecutionError("context_limit_exceeded")
            except Exception:
                metrics.observe_retrieval("error", time.perf_counter() - started)
                raise
            record(active, **{"nexora.outcome": "hit" if snapshot.chunk_count else "miss"})
            metrics.observe_retrieval("success", time.perf_counter() - started)
            return evidence

    async def _observed_generate(
        self, adapter, decision, request, timeout, context, step, is_cancelled
    ):
        provider = decision.candidate.provider
        started = time.perf_counter()
        with span(
            "model.generate",
            **{
                "nexora.workspace_id": context.workspace_id,
                "nexora.run_id": context.run_id,
                "nexora.attempt": context.attempt_count,
                "nexora.step": step,
                "nexora.provider": provider,
                "nexora.model": decision.candidate.model,
                "nexora.routing_reason": decision.reason,
            },
        ) as active:
            try:
                response = await self._generate(
                    adapter, decision.candidate.model, request, timeout, context, is_cancelled
                )
            except ProviderError as exc:
                record_error(active, exc.code)
                metrics.observe_model_call(provider, "error", time.perf_counter() - started)
                raise
            except RetryableExecutionError as exc:
                record_error(active, exc.code)
                metrics.observe_model_call(provider, "timeout", time.perf_counter() - started)
                raise
            except TerminalExecutionError as exc:
                outcome = "cancelled" if exc.code == "cancel_requested" else "error"
                record_error(active, exc.code)
                metrics.observe_model_call(provider, outcome, time.perf_counter() - started)
                raise
            except Exception:
                metrics.observe_model_call(provider, "error", time.perf_counter() - started)
                raise
            record(
                active,
                **{
                    "nexora.finish_reason": response.finish_reason,
                    "nexora.input_tokens": response.usage.input_tokens,
                    "nexora.output_tokens": response.usage.output_tokens,
                },
            )
            metrics.observe_model_call(
                provider,
                "success",
                time.perf_counter() - started,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            return response

    @staticmethod
    def _validate(response):
        if (
            len(response.tool_calls) > 16
            or len({c.id for c in response.tool_calls}) != len(response.tool_calls)
            or any(not c.id or len(c.id) > 200 or not c.name for c in response.tool_calls)
            or len(response.text or "") > 100000
            or response.usage.input_tokens < 0
            or response.usage.output_tokens < 0
        ):
            raise TerminalExecutionError("invalid_model_response")
        if len(json.dumps([c.arguments for c in response.tool_calls])) > 100000:
            raise TerminalExecutionError("model_arguments_too_large")

    async def _generate(self, adapter, model, request, timeout, context, is_cancelled):
        task = asyncio.create_task(
            adapter.generate(model=model, request=request, timeout_seconds=timeout)
        )
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if await is_cancelled():
                    raise TerminalExecutionError("cancel_requested")
                await self.store.check(context)
                if asyncio.get_running_loop().time() >= deadline and not task.done():
                    raise RetryableExecutionError("provider_timeout")
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
