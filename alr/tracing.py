"""Observability — section 16. SQLite for the MVP (swap for Postgres later,
same schema). Every routing decision + outcome is recorded so section 24
(router memory) and section 39-1 (routing dataset as the real moat) have
something to learn from.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterator, Optional

from .models import ExecutionResult, RouteDecision

DEFAULT_DB_PATH = "alr_traces.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    task_type TEXT,
    complexity REAL,
    risk TEXT,
    selected_route TEXT,
    selected_model TEXT,
    alternatives TEXT,
    estimated_cost REAL,
    actual_cost REAL,
    estimated_quality REAL,
    confidence REAL,
    success INTEGER,
    escalated INTEGER,
    escalation_reason TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    context_tokens_before INTEGER,
    context_tokens_after INTEGER,
    created_at REAL
);
"""

# Columns added after the original v1 schema. Kept as an explicit list
# (name, SQL type) so both backends can migrate an existing v1 database
# in place with ALTER TABLE ... ADD COLUMN instead of requiring a fresh
# file/database — evaluation.py (section 16/17/18 metrics) needs these.
_V2_COLUMNS = [
    ("estimated_quality", "REAL"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("context_tokens_before", "INTEGER"),
    ("context_tokens_after", "INTEGER"),
]


class TraceStore:
    """SQLite-backed trace store, WAL-mode for better read/write
    concurrency under a single-process API server. For multi-replica
    deployments, set DATABASE_URL to a Postgres DSN and use
    PostgresTraceStore instead (see bottom of this file) — same schema,
    swapped backend, verified against a live postgres:16-alpine instance
    including a concurrent-startup race (see PostgresTraceStore's
    docstring below and the README's Autoscaling section).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(SCHEMA)
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(traces)").fetchall()}
            for name, sql_type in _V2_COLUMNS:
                if name not in existing_cols:
                    conn.execute(f"ALTER TABLE traces ADD COLUMN {name} {sql_type}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        decision: RouteDecision,
        result: ExecutionResult,
        task_type: Optional[str] = None,
        risk: Optional[str] = None,
        complexity: Optional[float] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO traces
                (trace_id, task_type, complexity, risk, selected_route, selected_model,
                 alternatives, estimated_cost, actual_cost, estimated_quality, confidence,
                 success, escalated, escalation_reason, latency_ms, input_tokens,
                 output_tokens, context_tokens_before, context_tokens_after, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.trace_id,
                    task_type,
                    complexity,
                    risk,
                    decision.route.value,
                    decision.model,
                    json.dumps(decision.alternatives),
                    decision.estimated_cost,
                    result.cost,
                    decision.confidence,
                    result.confidence,
                    int(result.success),
                    int(result.escalated),
                    result.escalation_reason,
                    result.latency_ms,
                    result.input_tokens,
                    result.output_tokens,
                    result.context_tokens_before,
                    result.context_tokens_after,
                    time.time(),
                ),
            )

    def all_rows(self) -> list:
        """Raw trace rows as plain dicts — the input evaluation.py's metric
        functions consume (section 16/17/18). Kept separate from
        summary_metrics() so the lightweight rollup stays cheap and the
        full evaluation pass is opt-in.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM traces ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def summary_metrics(self) -> dict:
        """Cheap rollup — section 16's headline numbers only. For the full
        metric set (P50/P95 latency, false-local/false-frontier rate,
        quality-per-dollar, etc.) use alr.evaluation.compute_evaluation_metrics
        (exposed at GET /metrics/full).
        """
        rows = self.all_rows()

        if not rows:
            return {"total_requests": 0}

        total = len(rows)
        frontier = sum(1 for r in rows if r["selected_route"] == "frontier")
        local = sum(1 for r in rows if r["selected_route"] == "local")
        tool = sum(1 for r in rows if r["selected_route"] == "tool")
        escalated = sum(1 for r in rows if r["escalated"])
        successes = sum(1 for r in rows if r["success"])
        total_cost = sum(r["actual_cost"] or 0 for r in rows)

        return {
            "total_requests": total,
            "frontier_requests": frontier,
            "local_requests": local,
            "tool_requests": tool,
            "escalation_rate": round(escalated / total, 3),
            "task_success_rate": round(successes / total, 3),
            "total_cost": round(total_cost, 4),
            "cost_per_successful_task": round(total_cost / successes, 6) if successes else None,
        }


# --- Postgres backend (required for multi-replica/autoscaled deployments) --
# Verified against a live postgres:16-alpine container during development
# (redis:7-alpine likewise for the rate limiter — see rate_limit.py) —
# this is no longer a "written but untested" claim. Same public interface
# as TraceStore (record, all_rows, summary_metrics), a drop-in swap in
# pipeline.py's Pipeline.__init__. Only imported if you actually set
# DATABASE_URL; the import is deferred so `psycopg` isn't a hard
# dependency for single-replica SQLite users.
POSTGRES_SCHEMA = SCHEMA.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY").replace(
    "TEXT PRIMARY KEY", "TEXT PRIMARY KEY"
)

# Arbitrary fixed key for the schema-setup advisory lock (any bigint works —
# it just needs to be the same constant across every replica so they
# actually contend for the same lock).
_SCHEMA_LOCK_ID = 727271


class PostgresTraceStore:
    def __init__(self, dsn: str):
        try:
            import psycopg  # type: ignore
        except ImportError as e:
            raise ImportError(
                "PostgresTraceStore requires `pip install psycopg[binary]`. "
                "This is optional — SQLite's TraceStore has no extra dependency."
            ) from e
        self._psycopg = psycopg
        self.dsn = dsn
        with psycopg.connect(dsn) as conn:
            # Postgres's CREATE TABLE IF NOT EXISTS is NOT safe against
            # concurrent execution — the existence check and the create
            # aren't atomic across sessions, so N replicas booting at once
            # (exactly the autoscaling scenario this store exists for) can
            # all pass the check simultaneously and race on inserting into
            # pg_type, raising "duplicate key value violates unique
            # constraint pg_type_typname_nsp_index". An advisory lock
            # serializes schema setup across replicas: the first one to
            # grab it does the DDL, the rest block briefly then see the
            # table already exists and proceed. Verified against a real
            # 3-replica concurrent-startup race during development.
            conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_ID,))
            try:
                conn.execute(POSTGRES_SCHEMA)
                for name, sql_type in _V2_COLUMNS:
                    conn.execute(f"ALTER TABLE traces ADD COLUMN IF NOT EXISTS {name} {sql_type}")
                conn.commit()
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_ID,))
                conn.commit()

    def record(self, decision: RouteDecision, result: ExecutionResult, task_type=None, risk=None, complexity=None) -> None:
        with self._psycopg.connect(self.dsn) as conn:
            conn.execute(
                """INSERT INTO traces
                (trace_id, task_type, complexity, risk, selected_route, selected_model,
                 alternatives, estimated_cost, actual_cost, estimated_quality, confidence,
                 success, escalated, escalation_reason, latency_ms, input_tokens,
                 output_tokens, context_tokens_before, context_tokens_after, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (trace_id) DO UPDATE SET
                    success = EXCLUDED.success,
                    confidence = EXCLUDED.confidence,
                    escalated = EXCLUDED.escalated,
                    escalation_reason = EXCLUDED.escalation_reason,
                    actual_cost = EXCLUDED.actual_cost,
                    latency_ms = EXCLUDED.latency_ms,
                    input_tokens = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    context_tokens_after = EXCLUDED.context_tokens_after""",
                (
                    decision.trace_id, task_type, complexity, risk, decision.route.value, decision.model,
                    json.dumps(decision.alternatives), decision.estimated_cost, result.cost, decision.confidence,
                    result.confidence, int(result.success), int(result.escalated), result.escalation_reason,
                    result.latency_ms, result.input_tokens, result.output_tokens, result.context_tokens_before,
                    result.context_tokens_after, time.time(),
                ),
            )
            conn.commit()

    def all_rows(self) -> list:
        with self._psycopg.connect(self.dsn) as conn:
            conn.row_factory = self._psycopg.rows.dict_row  # type: ignore[attr-defined]
            rows = conn.execute("SELECT * FROM traces ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def summary_metrics(self) -> dict:
        rows = self.all_rows()

        if not rows:
            return {"total_requests": 0}
        total = len(rows)
        frontier = sum(1 for r in rows if r["selected_route"] == "frontier")
        local = sum(1 for r in rows if r["selected_route"] == "local")
        tool = sum(1 for r in rows if r["selected_route"] == "tool")
        escalated = sum(1 for r in rows if r["escalated"])
        successes = sum(1 for r in rows if r["success"])
        total_cost = sum(r["actual_cost"] or 0 for r in rows)
        return {
            "total_requests": total,
            "frontier_requests": frontier,
            "local_requests": local,
            "tool_requests": tool,
            "escalation_rate": round(escalated / total, 3),
            "task_success_rate": round(successes / total, 3),
            "total_cost": round(total_cost, 4),
            "cost_per_successful_task": round(total_cost / successes, 6) if successes else None,
        }


def build_trace_store(db_path: str = DEFAULT_DB_PATH):
    """Factory: returns PostgresTraceStore if DATABASE_URL is set, else
    the (fully tested) SQLite TraceStore. Used by api.py.
    """
    import os

    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return PostgresTraceStore(dsn)
    return TraceStore(db_path)
