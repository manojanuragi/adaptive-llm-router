"""Confidence & Escalation — section 10 of the design doc.

"Never blindly trust the local model." This module decides, after a
Tier-1 (local) execution, whether the result is HIGH / MEDIUM / LOW
confidence and whether to escalate to Tier 2 (frontier).

Phase-1 MVP uses simple heuristic signals (self-reported confidence +
refusal/hedge detection + registry prior). Swap in real calibration or
self-consistency voting later (see askalf/hybrid-style approaches) without
touching router.py or pipeline.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .registry import ModelRegistry

HEDGE_PATTERNS = [
    r"\bi'?m not sure\b",
    r"\bi don'?t know\b",
    r"\bcannot determine\b",
    r"\bunclear\b",
    r"\bmight be\b",
    r"\bit'?s hard to say\b",
]


@dataclass
class ValidationResult:
    confidence: float
    should_escalate: bool
    reasons: List[str]


class Validator:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def validate_local_result(
        self,
        text: str,
        model_name: str,
        category: str,
        self_reported_confidence: Optional[float],
    ) -> ValidationResult:
        reasons: List[str] = []
        prior = self.registry.expected_quality(model_name, category)

        signals = [prior]
        reasons.append(f"registry prior for {model_name}/{category}: {prior:.2f}")

        if self_reported_confidence is not None:
            signals.append(self_reported_confidence)
            reasons.append(f"self-reported confidence: {self_reported_confidence:.2f}")

        hedge_hits = sum(1 for p in HEDGE_PATTERNS if re.search(p, text.lower()))
        if hedge_hits:
            penalty = min(0.4, 0.15 * hedge_hits)
            signals.append(max(0.0, prior - penalty))
            reasons.append(f"hedging language detected ({hedge_hits}x) → confidence penalty {penalty:.2f}")

        if len(text.strip()) < 10:
            signals.append(0.1)
            reasons.append("output implausibly short")

        confidence = sum(signals) / len(signals)
        threshold = self.registry.confidence_threshold
        should_escalate = confidence < threshold
        if should_escalate:
            reasons.append(f"confidence {confidence:.2f} < threshold {threshold:.2f} → escalate")

        return ValidationResult(confidence=round(confidence, 3), should_escalate=should_escalate, reasons=reasons)
