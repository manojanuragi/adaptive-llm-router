#!/usr/bin/env python3
"""Command-line entry point for routing your own ad-hoc tasks through ALR.

This is the thing to actually use day-to-day if you want real tasks to
get the tool/local/frontier routing + cost savings tracking this project
builds — pipeline_demo.py and demo.py are fixed illustration scripts,
this one takes whatever you give it.

Usage:
    python3 alr_cli.py "git status"
    python3 alr_cli.py "Summarize this log excerpt: ..."
    echo "some task text" | python3 alr_cli.py
    python3 alr_cli.py "Summarize this" --context-file mylog.txt
    python3 alr_cli.py "Investigate this incident" --risk high --privacy restricted

Prints the routing decision, the output, and the per-response token/cost
savings figures — the same numbers POST /execute returns and the trace
store persists, just for one task run directly from your terminal
instead of through the HTTP API. Every call is recorded to the trace
store (SQLite by default, or Postgres if DATABASE_URL is set), so
`GET /metrics` (or `pipeline.trace_store.summary_metrics()`) accumulates
real usage over repeated runs of this script.
"""
from __future__ import annotations

import argparse
import sys

from alr.models import Privacy, Risk, TaskEnvelope
from alr.pipeline import Pipeline


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Route a single ad-hoc task through the Adaptive LLM Router."
    )
    parser.add_argument("task", nargs="?", help="Task text. Omit to read from stdin.")
    parser.add_argument("--risk", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--privacy", choices=["public", "private", "restricted"], default="public")
    parser.add_argument(
        "--context-file",
        help="Attach a file's contents as raw evidence (compressed via the "
        "Context Firewall before it ever reaches a frontier call).",
    )
    parser.add_argument(
        "--db", default="alr_traces.db",
        help="Trace store path (ignored if DATABASE_URL is set — see README 'Cloud trace store').",
    )
    args = parser.parse_args()

    task_text = args.task or sys.stdin.read().strip()
    if not task_text:
        parser.error("No task provided — pass it as an argument or pipe it via stdin.")

    payload = {}
    if args.context_file:
        with open(args.context_file, "r") as f:
            payload["context_text"] = f.read()

    envelope = TaskEnvelope(
        task=task_text,
        risk=Risk(args.risk),
        privacy=Privacy(args.privacy),
        payload=payload,
    )

    pipeline = Pipeline(db_path=args.db)
    result = pipeline.run(envelope)

    print(f"route: {result.route.value}  model: {result.model}  success: {result.success}")
    if result.escalated:
        print(f"escalated: {result.escalation_reason}")
    print(
        f"cost: ${result.cost:.6f}   saved: ${result.cost_saved:.6f} "
        f"({result.cost_saved_pct:.1f}%)   tokens_saved: {result.tokens_saved}"
    )
    print()
    print(result.output)

    if not result.success:
        sys.exit(1)


if __name__ == "__main__":
    main()
