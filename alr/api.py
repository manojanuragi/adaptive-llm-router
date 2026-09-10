"""HTTP API layer — section 29 (Example MVP API), hardened for deployment.

This is the ONLY file in the project that needs FastAPI/pydantic. The
engine (analyzer/router/validator/context/tools/pipeline) has zero
dependency on this module and can be used as a library directly (see
demo.py / pipeline_demo.py).

Run:
    pip install -r requirements.txt
    export ALR_API_KEYS=your-key-here   # required in production, see below
    uvicorn alr.api:app --host 0.0.0.0 --port 8000

Endpoints:
    POST /route     — dry run: routing decision only, no execution
    POST /execute    — full pipeline: analyze -> route -> execute -> validate
                        -> escalate -> trace (policy-gated)
    GET  /metrics   — rollup metrics from the trace store (section 16)
    GET  /health     — liveness probe (process is up)
    GET  /ready      — readiness probe (dependencies reachable)

Deployment-readiness features in this file:
    - API key auth (alr/auth.py) — OFF if ALR_API_KEYS is unset, with a
      loud startup warning. Set it in any real deployment.
    - Per-key rate limiting (alr/rate_limit.py), in-memory — fine for one
      replica; swap for Redis-backed limiting once you run more than one.
    - Request body size cap, to stop someone sending a 500MB task string.
    - Policy engine (alr/policy.py) is enforced inside Pipeline itself, so
      it can't be bypassed by hitting /execute directly.
    - Structured JSON logging with trace_id on every request.
    - Startup/shutdown lifecycle logging (no persistent connections to
      close for SQLite, but this is where you'd close a Postgres pool).
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .analyzer import TaskAnalyzer
from .auth import build_fastapi_dependency, load_api_keys_from_env
from .evaluation import compute_evaluation_metrics
from .logging_config import configure_logging, get_logger
from .models import Privacy, Risk, TaskEnvelope
from .pipeline import Pipeline
from .policy import PolicyEngine
from .rate_limit import build_rate_limiter
from .registry import ModelRegistry
from .router import AdaptiveRouter

configure_logging()
log = get_logger("api")

MAX_BODY_BYTES = int(os.environ.get("ALR_MAX_BODY_BYTES", 200_000))  # ~200KB
RATE_LIMIT_CAPACITY = int(os.environ.get("ALR_RATE_LIMIT_CAPACITY", 60))
RATE_LIMIT_REFILL_PER_S = float(os.environ.get("ALR_RATE_LIMIT_REFILL_PER_S", 1.0))

_pipeline: Optional[Pipeline] = None
_registry: Optional[ModelRegistry] = None
_analyzer: Optional[TaskAnalyzer] = None
_router: Optional[AdaptiveRouter] = None
# Redis-backed if REDIS_URL is set and reachable (required once you run more
# than one replica), else falls back to per-process in-memory — see
# alr/rate_limit.py and the README's Autoscaling section.
_rate_limiter = build_rate_limiter(capacity=RATE_LIMIT_CAPACITY, refill_per_second=RATE_LIMIT_REFILL_PER_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pipeline, _registry, _analyzer, _router
    log.info("starting up")

    if not load_api_keys_from_env():
        log.warning(
            "ALR_API_KEYS is not set — auth is DISABLED. "
            "Set it before exposing this service beyond localhost."
        )

    if type(_rate_limiter).__name__ != "RedisRateLimiter":
        log.warning(
            "REDIS_URL not set/reachable — rate limiting is per-process in-memory. "
            "Fine for one replica; set REDIS_URL before running more than one "
            "(see README Autoscaling section) or per-key limits become "
            "capacity x replica_count."
        )
    if not os.environ.get("DATABASE_URL"):
        log.warning(
            "DATABASE_URL not set — using SQLite trace store (single-writer, "
            "single-file). Set DATABASE_URL to a Postgres DSN before running "
            "more than one replica."
        )

    _registry = ModelRegistry()
    _analyzer = TaskAnalyzer()
    _router = AdaptiveRouter(_registry)
    _pipeline = Pipeline(policy_engine=PolicyEngine())

    yield

    log.info("shutting down")
    # SQLite needs no explicit close; if you swap to a pooled Postgres
    # client, close the pool here.


app = FastAPI(title="Adaptive LLM Router", version="0.2.0", lifespan=lifespan)
require_api_key = build_fastapi_dependency()


@app.middleware("http")
async def request_guardrails(request: Request, call_next):
    """Body size cap + rate limit + request logging, applied once at the
    edge rather than duplicated in every route.
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})

    client_key = request.headers.get("x-api-key") or (request.client.host if request.client else "unknown")
    if request.url.path not in ("/health", "/ready") and not _rate_limiter.allow(client_key):
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    log.info(f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)")
    return response


class TaskRequest(BaseModel):
    task: str = Field(..., max_length=50_000)
    task_type: Optional[str] = None
    risk: Risk = Risk.LOW
    context_tokens: int = Field(default=0, ge=0, le=10_000_000)
    privacy: Privacy = Privacy.PUBLIC
    latency_budget_ms: int = Field(default=30_000, ge=100, le=600_000)
    quality_target: float = Field(default=0.90, ge=0.0, le=1.0)
    payload: Dict[str, Any] = Field(default_factory=dict)


def _to_envelope(req: TaskRequest) -> TaskEnvelope:
    return TaskEnvelope(
        task=req.task, task_type=req.task_type, risk=req.risk, context_tokens=req.context_tokens,
        privacy=req.privacy, latency_budget_ms=req.latency_budget_ms, quality_target=req.quality_target,
        payload=req.payload,
    )


@app.post("/route")
def route(req: TaskRequest, _key: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Dry run — section 29 example. Returns the decision without executing."""
    envelope = _to_envelope(req)
    features = _analyzer.analyze(envelope)
    decision = _router.route(envelope, features)
    return {
        "trace_id": decision.trace_id, "route": decision.route.value, "model": decision.model,
        "reason": decision.reason, "confidence": decision.confidence,
        "category": features.category, "complexity": features.complexity,
    }


@app.post("/execute")
async def execute(req: TaskRequest, _key: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Full pipeline: policy check + route + execute + validate + escalate + trace.
    Runs async so a slow local/frontier call doesn't block other requests.
    """
    envelope = _to_envelope(req)
    try:
        result = await _pipeline.run_async(envelope)
    except Exception as e:  # noqa: BLE001 — never leak internals to the client
        log.error(f"unhandled pipeline error: {e}", extra={"trace_id": envelope.trace_id})
        raise HTTPException(status_code=500, detail="Internal error processing task") from e

    return {
        "trace_id": result.trace_id, "route": result.route.value, "model": result.model,
        "output": result.output, "confidence": result.confidence, "success": result.success,
        "escalated": result.escalated, "escalation_reason": result.escalation_reason,
        "latency_ms": result.latency_ms, "cost": result.cost,
    }


@app.get("/metrics")
def metrics(_key: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Cheap rollup metrics — section 16 (enterprise dashboard numbers)."""
    return _pipeline.trace_store.summary_metrics()


@app.get("/metrics/full")
def metrics_full(_key: str = Depends(require_api_key)) -> Dict[str, Any]:
    """Full evaluation metric set — sections 16, 17, and 18: quality,
    economics, performance (P50/P95 latency), routing correctness
    (false-local / false-frontier rate proxies), context compression
    ratio, and quality-per-dollar/-second. See alr/evaluation.py for the
    definitions and caveats on each proxy metric.
    """
    rows = _pipeline.trace_store.all_rows()
    return compute_evaluation_metrics(rows, registry=_pipeline.registry).to_dict()


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness only — does not check dependencies. Unauthenticated on
    purpose: load balancers/orchestrators hit this without a key.
    Includes the container hostname so you can confirm a load balancer
    is actually distributing traffic across replicas rather than pinning
    to one (see README Autoscaling section)."""
    import socket

    return {"status": "ok", "instance": socket.gethostname()}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness — actually checks the trace store is reachable, so an
    orchestrator can stop routing traffic here if the DB is down."""
    try:
        _pipeline.trace_store.summary_metrics()
        return JSONResponse(status_code=200, content={"status": "ready"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not ready", "detail": str(e)})
