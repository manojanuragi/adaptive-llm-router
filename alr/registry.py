"""Model registry — section 14/15 of the design doc.

Loads models.yaml so the router never hard-codes model names. Swap in a
database-backed registry later without touching router.py, since callers
only use ModelRegistry's public methods.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "models.yaml")


@dataclass
class ModelSpec:
    name: str
    provider: str
    model_name: str
    capabilities: List[str]
    cost_per_1k_tokens: float
    latency_ms_p50: int
    privacy: str  # "high" (local-only) | "cloud"
    endpoint: Optional[str] = None  # required for local providers (e.g. Ollama)


class ModelRegistry:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f)

        self._models: Dict[str, ModelSpec] = {}
        for name, cfg in raw.get("models", {}).items():
            self._models[name] = ModelSpec(
                name=name,
                provider=cfg["provider"],
                model_name=cfg["model_name"],
                capabilities=cfg.get("capabilities", []),
                cost_per_1k_tokens=float(cfg.get("cost_per_1k_tokens", 0.0)),
                latency_ms_p50=int(cfg.get("latency_ms_p50", 1000)),
                privacy=cfg.get("privacy", "cloud"),
                endpoint=cfg.get("endpoint"),
            )

        self._capability_matrix: Dict[str, Dict[str, float]] = raw.get("capability_matrix", {})
        self._escalation_cfg: Dict[str, float] = raw.get("escalation", {})

    def get(self, name: str) -> Optional[ModelSpec]:
        return self._models.get(name)

    def all(self) -> List[ModelSpec]:
        return list(self._models.values())

    def local_only_models(self) -> List[ModelSpec]:
        return [m for m in self._models.values() if m.privacy == "high"]

    def cloud_capable_models(self) -> List[ModelSpec]:
        return [m for m in self._models.values() if m.privacy == "cloud"]

    def expected_quality(self, model_name: str, category: str) -> float:
        """Prior quality estimate for a model on a task category.

        Falls back to 0.5 (coin flip) for unknown combinations rather than
        raising, so routing degrades gracefully as new task categories show
        up before the capability matrix has been updated for them.
        """
        return self._capability_matrix.get(model_name, {}).get(category, 0.5)

    @property
    def confidence_threshold(self) -> float:
        return float(self._escalation_cfg.get("confidence_threshold", 0.70))

    @property
    def complexity_frontier_threshold(self) -> float:
        return float(self._escalation_cfg.get("complexity_frontier_threshold", 0.80))
