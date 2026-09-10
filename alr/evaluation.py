"""Evaluation framework — sections 16 (Observability), 17 (Evaluation
Framework) and 18 (Critical Metric: False-Local Rate) of the design doc.

`alr/tracing.py`'s `summary_metrics()` gives you the cheap headline
numbers for a live dashboard. This module computes the *full* metric set
the doc asks for, and additionally provides:

  - `pass_at_k` — the unbiased pass@k estimator (section 17, Quality group)
  - `run_baseline` — forces the router to a fixed tier so you can compare
    Frontier-only / Local-only / Adaptive routing on the same task set
    (section 35's benchmark design)

Nothing here requires a live TraceStore: `compute_evaluation_metrics`
takes plain row dicts, so both `TraceStore.all_rows()` (production trace
history) and `run_baseline`'s in-memory rows (a one-off benchmark run)
feed the same computation.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import ExecutionResult, RouteDecision, RouteTier, TaskEnvelope
from .registry import ModelRegistry


# ---------------------------------------------------------------------
# Section 16/17/18 — the full evaluation metric set
# ---------------------------------------------------------------------
@dataclass
class EvaluationMetrics:
    total_requests: int

    # Quality (section 17)
    task_success_rate: Optional[float]
    local_success_rate: Optional[float]
    frontier_success_rate: Optional[float]
    model_failure_rate: Optional[float]

    # Economics (section 16/17)
    total_cost: float
    cost_per_task: Optional[float]
    cost_per_successful_task: Optional[float]
    frontier_tokens_total: int
    frontier_tokens_per_task: Optional[float]
    local_tokens_total: int
    total_tokens_per_task: Optional[float]

    # Performance (section 17)
    latency_p50_ms: Optional[float]
    latency_p95_ms: Optional[float]
    latency_p50_ms_by_route: Dict[str, float]
    latency_p95_ms_by_route: Dict[str, float]
    time_to_successful_completion_p50_ms: Optional[float]

    # Routing (section 17/18) — see `_routing_diagnostics` for the
    # proxy definitions; these are heuristics, not ground truth, exactly
    # as section 15 warns ("replace with measured numbers").
    escalation_rate: Optional[float]
    false_local_rate: Optional[float]
    false_frontier_rate: Optional[float]
    correct_routing_rate: Optional[float]

    # Context Firewall (sections 12, 23, 32-C)
    context_compression_ratio: Optional[float]
    context_samples: int

    # Economic-quality composites (section 16)
    quality_per_dollar: Optional[float]
    quality_per_second: Optional[float]

    # Breakdown
    requests_by_route: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. No numpy dependency, consistent with the
    rest of this codebase's stdlib-only engine.
    """
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return float(ordered[k])


def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return numerator / denominator


def _routing_diagnostics(rows: List[dict], registry: Optional[ModelRegistry]):
    """False-local / false-frontier / correct-routing proxies — section 18.

    A false-local decision means the router sent a task to the local
    model but it really needed the frontier model. Proxy: routed to
    local AND (it failed OR it was escalated afterwards) — both are
    direct evidence the local tier wasn't sufficient.

    A false-frontier decision means the router sent an easy task to the
    frontier model even though local would plausibly have solved it.
    This can only be estimated with the model registry's capability
    matrix (what would local's expected quality have been for this
    category?), and only for rows that reached frontier *by the default
    routing path* — not via a hard constraint (privacy=restricted,
    risk=high, or complexity over threshold), since those routes are
    correct by policy regardless of what local might have scored.
    Without a registry, false_frontier_rate is reported as None rather
    than guessed.
    """
    local_rows = [r for r in rows if r["selected_route"] == "local"]
    frontier_rows = [r for r in rows if r["selected_route"] == "frontier"]

    false_local = sum(1 for r in local_rows if not r["success"] or r["escalated"])
    false_local_rate = _safe_div(false_local, len(local_rows))

    false_frontier = 0
    frontier_denominator = len(frontier_rows)
    if registry is not None and frontier_rows:
        local_models = registry.local_only_models()
        threshold = registry.confidence_threshold
        complexity_threshold = registry.complexity_frontier_threshold
        for r in frontier_rows:
            if r.get("risk") == "high":
                continue  # hard constraint — correct by policy
            complexity = r.get("complexity")
            if complexity is not None and complexity >= complexity_threshold:
                continue  # hard constraint — correct by policy
            if not local_models or not r.get("task_type"):
                continue
            best_local_prior = max(
                registry.expected_quality(m.name, r["task_type"]) for m in local_models
            )
            if best_local_prior >= threshold:
                false_frontier += 1
    else:
        frontier_denominator = 0  # can't estimate without a registry — report None, not a guess

    false_frontier_rate = _safe_div(false_frontier, frontier_denominator) if frontier_denominator else None

    total = len(rows)
    if total:
        wrong = false_local + false_frontier
        correct_routing_rate = 1.0 - wrong / total
    else:
        correct_routing_rate = None

    return (
        round(false_local_rate, 4) if false_local_rate is not None else None,
        round(false_frontier_rate, 4) if false_frontier_rate is not None else None,
        round(correct_routing_rate, 4) if correct_routing_rate is not None else None,
    )


def compute_evaluation_metrics(
    rows: Iterable[dict], registry: Optional[ModelRegistry] = None
) -> EvaluationMetrics:
    """Compute the full section 16/17/18 metric set from trace rows.

    `rows` — an iterable of dicts shaped like `TraceStore.all_rows()`
    output (works for both the SQLite and Postgres backends, and for
    `run_baseline`'s in-memory rows below).

    `registry` — optional; without it, false_frontier_rate and
    correct_routing_rate degrade to None instead of a wrong guess (see
    `_routing_diagnostics`).
    """
    rows = list(rows)
    total = len(rows)

    if total == 0:
        return EvaluationMetrics(
            total_requests=0, task_success_rate=None, local_success_rate=None,
            frontier_success_rate=None, model_failure_rate=None, total_cost=0.0,
            cost_per_task=None, cost_per_successful_task=None, frontier_tokens_total=0,
            frontier_tokens_per_task=None, local_tokens_total=0, total_tokens_per_task=None,
            latency_p50_ms=None, latency_p95_ms=None, latency_p50_ms_by_route={},
            latency_p95_ms_by_route={}, time_to_successful_completion_p50_ms=None,
            escalation_rate=None, false_local_rate=None, false_frontier_rate=None,
            correct_routing_rate=None, context_compression_ratio=None, context_samples=0,
            quality_per_dollar=None, quality_per_second=None, requests_by_route={},
        )

    successes = [r for r in rows if r["success"]]
    escalated = [r for r in rows if r["escalated"]]
    total_cost = sum(r["actual_cost"] or 0.0 for r in rows)

    by_route: Dict[str, List[dict]] = {}
    for r in rows:
        by_route.setdefault(r["selected_route"], []).append(r)
    requests_by_route = {route: len(rs) for route, rs in by_route.items()}

    local_rows = by_route.get("local", [])
    frontier_rows = by_route.get("frontier", [])
    llm_rows = local_rows + frontier_rows  # model-executing tiers only, excludes tool tier

    local_success_rate = _safe_div(sum(1 for r in local_rows if r["success"]), len(local_rows))
    frontier_success_rate = _safe_div(sum(1 for r in frontier_rows if r["success"]), len(frontier_rows))
    model_failure_rate = _safe_div(sum(1 for r in llm_rows if not r["success"]), len(llm_rows))

    frontier_tokens_total = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in frontier_rows)
    local_tokens_total = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in local_rows)

    all_latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    latency_p50_ms_by_route = {}
    latency_p95_ms_by_route = {}
    for route, rs in by_route.items():
        lat = [r["latency_ms"] for r in rs if r.get("latency_ms") is not None]
        if lat:
            latency_p50_ms_by_route[route] = _percentile(lat, 50)
            latency_p95_ms_by_route[route] = _percentile(lat, 95)

    success_latencies = [r["latency_ms"] for r in successes if r.get("latency_ms") is not None]

    context_ratios = [
        r["context_tokens_after"] / r["context_tokens_before"]
        for r in rows
        if r.get("context_tokens_before") and r.get("context_tokens_after") is not None
        and r["context_tokens_before"] > 0
    ]

    false_local_rate, false_frontier_rate, correct_routing_rate = _routing_diagnostics(rows, registry)

    avg_cost_per_task = _safe_div(total_cost, total)
    avg_latency_s = _safe_div(sum(all_latencies) / 1000.0, len(all_latencies)) if all_latencies else None
    task_success_rate = _safe_div(len(successes), total)

    quality_per_dollar = (
        round(task_success_rate / avg_cost_per_task, 4)
        if task_success_rate is not None and avg_cost_per_task
        else None
    )
    quality_per_second = (
        round(task_success_rate / avg_latency_s, 4)
        if task_success_rate is not None and avg_latency_s
        else None
    )

    return EvaluationMetrics(
        total_requests=total,
        task_success_rate=round(task_success_rate, 4) if task_success_rate is not None else None,
        local_success_rate=round(local_success_rate, 4) if local_success_rate is not None else None,
        frontier_success_rate=round(frontier_success_rate, 4) if frontier_success_rate is not None else None,
        model_failure_rate=round(model_failure_rate, 4) if model_failure_rate is not None else None,
        total_cost=round(total_cost, 6),
        cost_per_task=round(avg_cost_per_task, 6) if avg_cost_per_task is not None else None,
        cost_per_successful_task=round(_safe_div(total_cost, len(successes)), 6) if successes else None,
        frontier_tokens_total=frontier_tokens_total,
        frontier_tokens_per_task=round(_safe_div(frontier_tokens_total, len(frontier_rows)), 2) if frontier_rows else None,
        local_tokens_total=local_tokens_total,
        total_tokens_per_task=round(_safe_div(frontier_tokens_total + local_tokens_total, total), 2),
        latency_p50_ms=_percentile(all_latencies, 50),
        latency_p95_ms=_percentile(all_latencies, 95),
        latency_p50_ms_by_route=latency_p50_ms_by_route,
        latency_p95_ms_by_route=latency_p95_ms_by_route,
        time_to_successful_completion_p50_ms=_percentile(success_latencies, 50),
        escalation_rate=round(_safe_div(len(escalated), total), 4),
        false_local_rate=false_local_rate,
        false_frontier_rate=false_frontier_rate,
        correct_routing_rate=correct_routing_rate,
        context_compression_ratio=(
            round(sum(context_ratios) / len(context_ratios), 4) if context_ratios else None
        ),
        context_samples=len(context_ratios),
        quality_per_dollar=quality_per_dollar,
        quality_per_second=quality_per_second,
        requests_by_route=requests_by_route,
    )


# ---------------------------------------------------------------------
# Section 17, Quality group — Pass@k
# ---------------------------------------------------------------------
def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator (Chen et al., the standard formula also
    used by HumanEval/Codex-style benchmarks): given `n` sampled attempts
    at a task of which `c` succeeded, the probability at least one of a
    random k-subset succeeds.
    """
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_at_k_from_attempts(attempts: Dict[str, List[bool]], k: int) -> Optional[float]:
    """Average pass@k across tasks (section 35's benchmark design: run
    each task N times, e.g. with local-model sampling temperature > 0,
    and check whether at least one of a k-subset of attempts passed).
    `attempts` maps task_id -> list of per-attempt success booleans.
    Requires every task to have at least `k` attempts recorded.
    """
    scores = []
    for task_id, results in attempts.items():
        n = len(results)
        if n < k:
            continue
        c = sum(1 for r in results if r)
        scores.append(pass_at_k(n, c, k))
    return round(sum(scores) / len(scores), 4) if scores else None


# ---------------------------------------------------------------------
# Section 35 — baseline comparison harness (Frontier-only / Local-only /
# Adaptive routing on the same task set)
# ---------------------------------------------------------------------
def _row_from_result(
    decision: RouteDecision, result: ExecutionResult, task_type: Optional[str],
    risk: Optional[str], complexity: Optional[float],
) -> dict:
    """Same shape as TraceStore.all_rows() output, built in-memory —
    lets compute_evaluation_metrics() work identically for a live trace
    store and a one-off benchmark run.
    """
    return {
        "trace_id": decision.trace_id, "task_type": task_type, "complexity": complexity,
        "risk": risk, "selected_route": decision.route.value, "selected_model": decision.model,
        "estimated_quality": decision.confidence, "confidence": result.confidence,
        "success": bool(result.success), "escalated": bool(result.escalated),
        "escalation_reason": result.escalation_reason, "actual_cost": result.cost,
        "latency_ms": result.latency_ms, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "context_tokens_before": result.context_tokens_before,
        "context_tokens_after": result.context_tokens_after,
    }


@contextmanager
def _forced_route(pipeline, tier: RouteTier):
    """Temporarily force every non-tool routing decision to `tier` —
    section 35's "Frontier-only" / "Local-only" baselines. Tool-tier
    matches are left alone (a deterministic `git status` doesn't become
    an LLM call just because we're benchmarking the frontier baseline —
    section 5's "prefer deterministic tools whenever possible" is a
    property of the task, not the model being benchmarked).
    """
    original_route = pipeline.router.route
    registry = pipeline.registry

    def forced(envelope: TaskEnvelope, features):
        decision = original_route(envelope, features)
        if decision.route == RouteTier.TOOL:
            return decision
        candidates = (
            registry.local_only_models() if tier == RouteTier.LOCAL else registry.cloud_capable_models()
        )
        if not candidates:
            return decision  # nothing to force onto, keep the original decision
        chosen = max(candidates, key=lambda m: registry.expected_quality(m.name, features.category))
        decision.route = tier
        decision.model = chosen.name
        decision.confidence = registry.expected_quality(chosen.name, features.category)
        return decision

    pipeline.router.route = forced
    try:
        yield
    finally:
        pipeline.router.route = original_route


def run_baseline(
    pipeline, envelopes: List[TaskEnvelope], mode: str = "adaptive", registry: Optional[ModelRegistry] = None
) -> EvaluationMetrics:
    """Run one baseline strategy (section 35) over a benchmark task set
    and return its full evaluation metrics. `mode` is one of:
    "adaptive" (the pipeline's normal rule-based routing, unmodified),
    "local_only", or "frontier_only". Does not touch the pipeline's
    trace store — rows are aggregated purely in memory, so you can call
    this repeatedly with different modes against the same task set
    without polluting production trace history.
    """
    rows: List[dict] = []

    def _run_one(envelope: TaskEnvelope):
        features = pipeline.analyzer.analyze(envelope)
        decision = pipeline.router.route(envelope, features)
        policy_result = pipeline.policy.evaluate(envelope, tool_or_model=decision.model)
        from .policy import PolicyDecision

        if policy_result.decision != PolicyDecision.ALLOW:
            result = ExecutionResult(
                trace_id=envelope.trace_id, route=decision.route, model=decision.model,
                output="Blocked by policy engine", confidence=0.0, success=False,
                escalation_reason="policy_denied",
            )
        else:
            result = pipeline._execute(envelope, decision)
        rows.append(_row_from_result(decision, result, features.category, envelope.risk.value, features.complexity))

    if mode == "adaptive":
        for env in envelopes:
            _run_one(env)
    elif mode in ("local_only", "frontier_only"):
        tier = RouteTier.LOCAL if mode == "local_only" else RouteTier.FRONTIER
        with _forced_route(pipeline, tier):
            for env in envelopes:
                _run_one(env)
    else:
        raise ValueError(f"Unknown baseline mode: {mode!r} (expected adaptive/local_only/frontier_only)")

    return compute_evaluation_metrics(rows, registry=registry or pipeline.registry)


def compare_baselines(
    pipeline, envelopes: List[TaskEnvelope], modes: Optional[List[str]] = None
) -> Dict[str, EvaluationMetrics]:
    """Section 35's comparison table in one call: run the same task set
    under each strategy and return {mode: EvaluationMetrics}.
    """
    modes = modes or ["frontier_only", "local_only", "adaptive"]
    return {mode: run_baseline(pipeline, envelopes, mode=mode) for mode in modes}
