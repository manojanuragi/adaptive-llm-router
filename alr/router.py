"""Adaptive Routing Engine — section 9, Strategy A (rule-based MVP).

"Start here... simple, explainable, easy to debug, easy to benchmark."
Swap this module for a learned classifier (Strategy B) later without
touching analyzer.py, tools.py, or the connectors — everything downstream
only depends on RouteDecision.
"""
from __future__ import annotations

from typing import List

from .models import Privacy, RouteDecision, RouteTier, TaskEnvelope, TaskFeatures
from .registry import ModelRegistry


class AdaptiveRouter:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def route(self, envelope: TaskEnvelope, features: TaskFeatures) -> RouteDecision:
        reasons: List[str] = []

        # Tier 0 — deterministic tool always wins if one matched. Section 5:
        # "This tier should be preferred whenever possible."
        if features.matched_tool:
            return RouteDecision(
                trace_id=envelope.trace_id,
                route=RouteTier.TOOL,
                model=features.matched_tool,
                reason=[f"deterministic tool match: {features.matched_tool}"],
                confidence=1.0,
                estimated_cost=0.0,
            )

        # Privacy is a hard constraint (section 20) — restricted data can
        # never be routed to a cloud model, regardless of complexity.
        local_models = self.registry.local_only_models()
        cloud_models = self.registry.cloud_capable_models()

        if envelope.privacy == Privacy.RESTRICTED:
            if not local_models:
                raise RuntimeError(
                    "Task requires privacy=restricted but no local-only model is registered."
                )
            reasons.append("privacy=restricted: cloud models excluded (hard constraint)")
            chosen = self._best_model(local_models, features.category)
            return RouteDecision(
                trace_id=envelope.trace_id,
                route=RouteTier.LOCAL,
                model=chosen.name,
                reason=reasons,
                confidence=self.registry.expected_quality(chosen.name, features.category),
                estimated_cost=0.0,
                alternatives=[],
            )

        # High complexity or declared high risk skips straight to frontier —
        # section 5, Tier 2 examples include "high-risk decisions".
        if (
            features.complexity >= self.registry.complexity_frontier_threshold
            or envelope.risk.value == "high"
        ):
            if not cloud_models:
                reasons.append("complexity/risk warrants frontier but none registered; falling back to local")
                chosen = self._best_model(local_models, features.category)
                route = RouteTier.LOCAL
            else:
                reasons.append(
                    f"complexity={features.complexity:.2f} >= threshold "
                    f"{self.registry.complexity_frontier_threshold} or risk=high"
                )
                chosen = self._best_model(cloud_models, features.category)
                route = RouteTier.FRONTIER
            return RouteDecision(
                trace_id=envelope.trace_id,
                route=route,
                model=chosen.name,
                reason=reasons,
                confidence=self.registry.expected_quality(chosen.name, features.category),
                estimated_cost=0.0,
                alternatives=[m.name for m in (cloud_models if route == RouteTier.FRONTIER else local_models) if m.name != chosen.name],
            )

        # Default: try local first, cheapest tier that's plausible.
        # (Escalation on low confidence happens later in the pipeline —
        # this is the *initial* route, not the final word. Section 10/11.)
        if local_models:
            chosen = self._best_model(local_models, features.category)
            reasons.append(f"default local-first routing for category='{features.category}'")
            return RouteDecision(
                trace_id=envelope.trace_id,
                route=RouteTier.LOCAL,
                model=chosen.name,
                reason=reasons,
                confidence=self.registry.expected_quality(chosen.name, features.category),
                estimated_cost=0.0,
                alternatives=[m.name for m in cloud_models],
            )

        # No local model registered at all — fall back to frontier.
        if not cloud_models:
            raise RuntimeError("No models registered in ModelRegistry.")
        chosen = self._best_model(cloud_models, features.category)
        reasons.append("no local model registered; routing to frontier")
        return RouteDecision(
            trace_id=envelope.trace_id,
            route=RouteTier.FRONTIER,
            model=chosen.name,
            reason=reasons,
            confidence=self.registry.expected_quality(chosen.name, features.category),
            estimated_cost=0.0,
        )

    def _best_model(self, candidates, category: str):
        return max(candidates, key=lambda m: self.registry.expected_quality(m.name, category))
