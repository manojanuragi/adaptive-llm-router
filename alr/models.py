"""Core data structures shared across the router pipeline.

Deliberately built on stdlib `dataclasses` rather than pydantic so the
engine has zero third-party dependencies. The FastAPI layer (alr/api.py)
wraps these in pydantic models at the HTTP boundary only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, Dict, List, Optional
import uuid


class Privacy(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    RESTRICTED = "restricted"  # hard constraint: never leaves local infra


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteTier(str, Enum):
    TOOL = "tool"
    LOCAL = "local"
    FRONTIER = "frontier"


@dataclass
class TaskEnvelope:
    """Input to the router. Mirrors section 6 of the design doc."""

    task: str
    task_type: Optional[str] = None
    risk: Risk = Risk.LOW
    context_tokens: int = 0
    privacy: Privacy = Privacy.PUBLIC
    latency_budget_ms: int = 30_000
    quality_target: float = 0.90
    payload: Dict[str, Any] = field(default_factory=dict)
    available_models: Optional[List[str]] = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Per-caller identity for usage tracking (alr/auth.py resolves this
    # from ALR_API_KEYS' optional `key:identity` labels) — never the raw
    # API key itself. "anonymous" when auth is off or unset.
    caller_id: str = "anonymous"


@dataclass
class TaskFeatures:
    """Output of the Task Analyzer. Section 8 of the design doc."""

    category: str
    complexity: float          # 0..1
    reasoning_depth: float     # 0..1
    context_size: int
    is_code: bool
    matched_tool: Optional[str] = None


@dataclass
class RouteDecision:
    trace_id: str
    route: RouteTier
    model: str
    reason: List[str]
    confidence: float
    estimated_cost: float = 0.0
    alternatives: List[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    trace_id: str
    route: RouteTier
    model: str
    output: Any
    confidence: float
    success: bool
    escalated: bool = False
    escalation_reason: Optional[str] = None
    latency_ms: int = 0
    cost: float = 0.0
    started_at: float = field(default_factory=time)
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens_before: Optional[int] = None
    context_tokens_after: Optional[int] = None
    # Savings tracking (see Pipeline._compute_savings): what the registry's
    # frontier model would have cost for this same response, and how much
    # of that was actually avoided. A proxy, not measured ground truth —
    # see the README's Honest limitations.
    baseline_cost: float = 0.0
    cost_saved: float = 0.0
    cost_saved_pct: float = 0.0
    tokens_saved: int = 0
    tokens_saved_pct: float = 0.0
