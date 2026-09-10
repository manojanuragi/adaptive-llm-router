"""The core loop from section 41:

  UNDERSTAND -> CLASSIFY -> ESTIMATE COMPLEXITY -> CHECK POLICY ->
  SELECT TOOL/LOCAL/FRONTIER -> EXECUTE -> VALIDATE -> ESCALATE IF NEEDED ->
  MEASURE -> LEARN

This module wires the pieces together. It's intentionally thin — each
step delegates to its own module so you can swap implementations
independently (e.g. router.py Strategy A -> Strategy B) without touching
this file.

Two execution paths are provided:
  - `run()`       — fully synchronous. Used by demo.py / pipeline_demo.py
                    and by the existing sync test suite.
  - `run_async()` — non-blocking; local/frontier calls run in a worker
                    thread via asyncio.to_thread so they don't block the
                    event loop under FastAPI/uvicorn. This is what
                    alr/api.py uses.

Both share the same CHECK POLICY step via alr/policy.py — no execution
path can bypass it, closing the "policy engine missing" gap.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .analyzer import TaskAnalyzer
from .context import ContextManager
from .frontier_llm import (
    FrontierLLMError,
    call_frontier_llm,
    call_frontier_llm_async,
    estimate_cost,
)
from .local_llm import LocalLLMUnavailable, call_local_llm, call_local_llm_async
from .logging_config import get_logger
from .models import ExecutionResult, RouteDecision, RouteTier, TaskEnvelope
from .policy import PolicyDecision, PolicyEngine
from .registry import ModelRegistry
from .router import AdaptiveRouter
from .tools import run_tool
from .tracing import build_trace_store
from .validator import Validator

log = get_logger("pipeline")


class PolicyDeniedError(Exception):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Pipeline:
    def __init__(
        self,
        registry_path: Optional[str] = None,
        db_path: str = "alr_traces.db",
        policy_engine: Optional[PolicyEngine] = None,
    ):
        self.registry = ModelRegistry(registry_path) if registry_path else ModelRegistry()
        self.analyzer = TaskAnalyzer()
        self.router = AdaptiveRouter(self.registry)
        self.validator = Validator(self.registry)
        self.context_manager = ContextManager()
        self.trace_store = build_trace_store(db_path)
        self.policy = policy_engine or PolicyEngine()

    # ---------------------------------------------------------------
    # Shared pre-execution steps (policy-checked, identical for both
    # sync and async paths)
    # ---------------------------------------------------------------
    def _plan(self, envelope: TaskEnvelope):
        features = self.analyzer.analyze(envelope)
        decision = self.router.route(envelope, features)

        policy_result = self.policy.evaluate(envelope, tool_or_model=decision.model)
        if policy_result.decision != PolicyDecision.ALLOW:
            log.warning(
                "policy blocked execution",
                extra={"trace_id": envelope.trace_id},
            )
            raise PolicyDeniedError(policy_result.reasons)

        return features, decision

    def _build_frontier_prompt(self, envelope: TaskEnvelope):
        """Context Firewall (sections 12, 23, 32-C): if the caller attached
        raw evidence under payload['context_text'] (logs, file contents,
        git output, ...), compress it through the Context Manager before it
        reaches the frontier model instead of forwarding it raw. Returns
        (prompt_text, context_tokens_before, context_tokens_after) — the
        token counts feed the "context compression ratio" metric in
        alr/evaluation.py. Word-count is used as the token proxy (no
        tokenizer dependency), consistent with the other heuristics in
        this MVP.
        """
        raw_context = envelope.payload.get("context_text")
        if not raw_context:
            return envelope.task, None, None

        evidence = self.context_manager.compress(raw_context, reference_id=envelope.trace_id)
        summary_json = json.dumps(evidence.summary)
        prompt = f"{envelope.task}\n\nCompressed evidence:\n{summary_json}"
        tokens_before = len(raw_context.split())
        tokens_after = len(summary_json.split())
        return prompt, tokens_before, tokens_after

    def _denied_result(self, envelope: TaskEnvelope, decision: RouteDecision, exc: PolicyDeniedError) -> ExecutionResult:
        return ExecutionResult(
            trace_id=envelope.trace_id, route=decision.route, model=decision.model,
            output=f"Blocked by policy engine: {exc}", confidence=0.0, success=False,
            escalation_reason="policy_denied",
        )

    # ---------------------------------------------------------------
    # Synchronous path
    # ---------------------------------------------------------------
    def run(self, envelope: TaskEnvelope) -> ExecutionResult:
        start = time.time()

        features, decision = None, None
        try:
            features, decision = self._plan(envelope)
        except PolicyDeniedError as e:
            # Need a decision object to record a trace even when denied
            # pre-execution; re-derive it without re-running policy.
            decision = self.router.route(envelope, self.analyzer.analyze(envelope))
            result = self._denied_result(envelope, decision, e)
            result.latency_ms = int((time.time() - start) * 1000)
            self.trace_store.record(decision=decision, result=result, task_type=None, risk=envelope.risk.value, complexity=None)
            return result

        result = self._execute(envelope, decision)

        if decision.route == RouteTier.LOCAL and result.success:
            validation = self.validator.validate_local_result(
                text=str(result.output),
                model_name=decision.model,
                category=features.category,
                self_reported_confidence=getattr(result, "_self_reported_confidence", None),
            )
            result.confidence = validation.confidence
            if validation.should_escalate:
                cloud_models = self.registry.cloud_capable_models()
                if envelope.privacy.value == "restricted" or not cloud_models:
                    result.escalation_reason = (
                        "would escalate but privacy=restricted or no frontier model registered"
                    )
                else:
                    frontier_model = cloud_models[0].name
                    escalated_decision = self.router.route(envelope, features)
                    escalated_decision.route = RouteTier.FRONTIER
                    escalated_decision.model = frontier_model
                    escalated_result = self._execute(envelope, escalated_decision)
                    escalated_result.escalated = True
                    escalated_result.escalation_reason = "; ".join(validation.reasons)
                    result = escalated_result

        result.latency_ms = int((time.time() - start) * 1000)
        self.trace_store.record(
            decision=decision, result=result, task_type=features.category,
            risk=envelope.risk.value, complexity=features.complexity,
        )
        return result

    def _execute(self, envelope: TaskEnvelope, decision: RouteDecision) -> ExecutionResult:
        if decision.route == RouteTier.TOOL:
            try:
                output = run_tool(decision.model, envelope.payload)
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=output, confidence=1.0, success=True,
                )
            except Exception as e:  # noqa: BLE001
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                )

        if decision.route == RouteTier.LOCAL:
            spec = self.registry.get(decision.model)
            try:
                resp = call_local_llm(spec, envelope.task)
                result = ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=resp.text, confidence=decision.confidence, success=True,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                )
                result._self_reported_confidence = resp.self_reported_confidence  # type: ignore[attr-defined]
                return result
            except LocalLLMUnavailable as e:
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                    escalation_reason="local model unavailable",
                )

        if decision.route == RouteTier.FRONTIER:
            spec = self.registry.get(decision.model)
            prompt, ctx_before, ctx_after = self._build_frontier_prompt(envelope)
            try:
                resp = call_frontier_llm(spec, prompt)
                cost = resp.cost_usd if resp.cost_usd is not None else estimate_cost(spec, resp.input_tokens, resp.output_tokens)
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=resp.text, confidence=decision.confidence, success=True, cost=cost,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                    context_tokens_before=ctx_before, context_tokens_after=ctx_after,
                )
            except FrontierLLMError as e:
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                    context_tokens_before=ctx_before, context_tokens_after=ctx_after,
                )

        raise ValueError(f"Unknown route: {decision.route}")

    # ---------------------------------------------------------------
    # Async path (used by the FastAPI layer)
    # ---------------------------------------------------------------
    async def run_async(self, envelope: TaskEnvelope) -> ExecutionResult:
        start = time.time()

        try:
            features, decision = self._plan(envelope)
        except PolicyDeniedError as e:
            decision = self.router.route(envelope, self.analyzer.analyze(envelope))
            result = self._denied_result(envelope, decision, e)
            result.latency_ms = int((time.time() - start) * 1000)
            self.trace_store.record(decision=decision, result=result, task_type=None, risk=envelope.risk.value, complexity=None)
            return result

        result = await self._execute_async(envelope, decision)

        if decision.route == RouteTier.LOCAL and result.success:
            validation = self.validator.validate_local_result(
                text=str(result.output),
                model_name=decision.model,
                category=features.category,
                self_reported_confidence=getattr(result, "_self_reported_confidence", None),
            )
            result.confidence = validation.confidence
            if validation.should_escalate:
                cloud_models = self.registry.cloud_capable_models()
                if envelope.privacy.value == "restricted" or not cloud_models:
                    result.escalation_reason = (
                        "would escalate but privacy=restricted or no frontier model registered"
                    )
                else:
                    frontier_model = cloud_models[0].name
                    escalated_decision = self.router.route(envelope, features)
                    escalated_decision.route = RouteTier.FRONTIER
                    escalated_decision.model = frontier_model
                    escalated_result = await self._execute_async(envelope, escalated_decision)
                    escalated_result.escalated = True
                    escalated_result.escalation_reason = "; ".join(validation.reasons)
                    result = escalated_result

        result.latency_ms = int((time.time() - start) * 1000)
        self.trace_store.record(
            decision=decision, result=result, task_type=features.category,
            risk=envelope.risk.value, complexity=features.complexity,
        )
        return result

    async def _execute_async(self, envelope: TaskEnvelope, decision: RouteDecision) -> ExecutionResult:
        if decision.route == RouteTier.TOOL:
            try:
                output = run_tool(decision.model, envelope.payload)
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=output, confidence=1.0, success=True,
                )
            except Exception as e:  # noqa: BLE001
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                )

        if decision.route == RouteTier.LOCAL:
            spec = self.registry.get(decision.model)
            try:
                resp = await call_local_llm_async(spec, envelope.task)
                result = ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=resp.text, confidence=decision.confidence, success=True,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                )
                result._self_reported_confidence = resp.self_reported_confidence  # type: ignore[attr-defined]
                return result
            except LocalLLMUnavailable as e:
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                    escalation_reason="local model unavailable",
                )

        if decision.route == RouteTier.FRONTIER:
            spec = self.registry.get(decision.model)
            prompt, ctx_before, ctx_after = self._build_frontier_prompt(envelope)
            try:
                resp = await call_frontier_llm_async(spec, prompt)
                cost = resp.cost_usd if resp.cost_usd is not None else estimate_cost(spec, resp.input_tokens, resp.output_tokens)
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=resp.text, confidence=decision.confidence, success=True, cost=cost,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                    context_tokens_before=ctx_before, context_tokens_after=ctx_after,
                )
            except FrontierLLMError as e:
                return ExecutionResult(
                    trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                    output=str(e), confidence=0.0, success=False,
                    context_tokens_before=ctx_before, context_tokens_after=ctx_after,
                )

        raise ValueError(f"Unknown route: {decision.route}")
