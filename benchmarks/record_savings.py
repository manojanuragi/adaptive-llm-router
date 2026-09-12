"""Scheduled savings benchmark — appends a per-response token/cost savings
snapshot to benchmarks/history/ on every run. Driven by
.github/workflows/benchmark.yml on a 6-hour cron, but safe to run by hand.

Runs headlessly (no interactive `claude login` session needed) via
whichever frontier auth the caller has configured:

  - Default: ALR_FRONTIER_TRANSPORT left unset ("cli") with
    CLAUDE_CODE_OAUTH_TOKEN set — a long-lived token from running
    `claude setup-token` once, billed against your existing Claude
    Pro/Max subscription. No ANTHROPIC_API_KEY needed.
  - Optional fallback: ALR_FRONTIER_TRANSPORT=api with ANTHROPIC_API_KEY,
    billed as raw Anthropic API usage instead.

See README "Scheduled savings benchmark" / "Two ways to run Tier 2".

The task set is intentionally small and cheap: one Tier-0 tool task
(free) and one task engineered to route straight to the frontier tier
(the "architecture" category scores complexity 0.90, above the 0.80
frontier threshold in models.yaml, so it reaches Claude without needing a
local model — see alr/analyzer.py's CATEGORY_KEYWORDS). No local-only
task is included because CI has no Ollama server to call; local-routed
calls would just fail closed. This runs 4x/day forever against real
Claude usage (subscription or API), so keep the task set small and cheap
unless you're deliberately expanding what this benchmarks.

Usage:
    CLAUDE_CODE_OAUTH_TOKEN=... python3 benchmarks/record_savings.py
    # or: ALR_FRONTIER_TRANSPORT=api ANTHROPIC_API_KEY=sk-... python3 benchmarks/record_savings.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alr.models import Privacy, Risk, TaskEnvelope
from alr.pipeline import Pipeline

HISTORY_DIR = Path(__file__).parent / "history"
HISTORY_JSONL = HISTORY_DIR / "savings_history.jsonl"
SUMMARY_MD = HISTORY_DIR / "SUMMARY.md"

TASKS = [
    {"name": "tool_git_status", "text": "git status"},
    {
        "name": "architecture_rate_limiter",
        "text": (
            "Design the architecture for a distributed rate limiter service "
            "that must survive a single region outage. Keep it to a short outline."
        ),
    },
]


def run_tasks() -> list:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp_db:
        pipeline = Pipeline(db_path=tmp_db.name)
        rows = []
        for task in TASKS:
            envelope = TaskEnvelope(task=task["text"], risk=Risk.LOW, privacy=Privacy.PUBLIC)
            result = pipeline.run(envelope)
            row = {
                "task": task["name"],
                "route": result.route.value,
                "escalated": result.escalated,
                "success": result.success,
                "cost": result.cost,
                "baseline_cost": result.baseline_cost,
                "cost_saved": result.cost_saved,
                "cost_saved_pct": result.cost_saved_pct,
                "tokens_saved": result.tokens_saved,
                "tokens_saved_pct": result.tokens_saved_pct,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
            }
            if not result.success:
                # Capture *why* — a bare "success: false" isn't enough to
                # diagnose a failed run after the fact (this is exactly
                # what made an earlier auth failure hard to debug before
                # alr/frontier_llm.py was fixed to surface the CLI's real
                # error instead of a blank "claude CLI exited 1:").
                row["error"] = str(result.output)[:300]
            rows.append(row)
        return rows


def main() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows = run_tasks()

    total_cost = sum(r["cost"] for r in rows)
    total_baseline = sum(r["baseline_cost"] for r in rows)
    total_saved = sum(r["cost_saved"] for r in rows)
    total_tokens_saved = sum(r["tokens_saved"] for r in rows)
    overall_pct = round(total_saved / total_baseline * 100, 2) if total_baseline else 0.0

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tasks": rows,
        "total_cost": round(total_cost, 6),
        "total_baseline_cost": round(total_baseline, 6),
        "total_cost_saved": round(total_saved, 6),
        "cost_saved_pct": overall_pct,
        "total_tokens_saved": total_tokens_saved,
    }

    with open(HISTORY_JSONL, "a") as f:
        f.write(json.dumps(record) + "\n")

    _append_summary_row(record)
    print(json.dumps(record, indent=2))


def _append_summary_row(record: dict) -> None:
    header = (
        "# Scheduled savings benchmark history\n\n"
        "Appended automatically by `.github/workflows/benchmark.yml` every "
        "6 hours. Raw per-response data lives in `savings_history.jsonl` "
        "next to this file. `cost_saved_pct` is the same proxy metric "
        "`alr/pipeline.py::Pipeline._compute_savings` computes for every "
        "live `/execute` response — see the README's Honest limitations.\n\n"
        "| Timestamp (UTC) | Total cost | Baseline cost | Saved | Saved % | Tokens saved |\n"
        "|---|---|---|---|---|---|\n"
    )
    row = (
        f"| {record['timestamp']} | ${record['total_cost']:.5f} | "
        f"${record['total_baseline_cost']:.5f} | ${record['total_cost_saved']:.5f} | "
        f"{record['cost_saved_pct']:.1f}% | {record['total_tokens_saved']} |\n"
    )
    if not SUMMARY_MD.exists():
        SUMMARY_MD.write_text(header + row)
    else:
        with open(SUMMARY_MD, "a") as f:
            f.write(row)


if __name__ == "__main__":
    main()
