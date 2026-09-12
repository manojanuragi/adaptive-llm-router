#!/usr/bin/env python3
"""MCP server exposing ALR's routing pipeline as a tool an MCP client
(Claude Code, Claude Desktop, or anything else that speaks MCP) can call
mid-conversation.

This is NOT a way to route an entire interactive session through ALR —
that's architecturally impossible (see README/SETUP.md: ALR's frontier
tier is a single-turn, tool-free completion; an interactive session is
the opposite of that). What this *does* let you do: during a normal
conversation, delegate a specific, well-defined, self-contained sub-task
(summarize this log, extract this data, answer this one factual
question) to ALR's tool/local/frontier tiering instead of always
spending a frontier turn on it — with real cost/savings numbers reported
back for that one call.

Setup (see SETUP.md "Use ALR as a tool inside Claude" for the full
walkthrough):
    pip install "mcp<2"
    # register it with Claude Code — either run:
    claude mcp add alr -- python3 /path/to/alr_mcp_server.py
    # or check the .mcp.json already committed in this repo's root and
    # approve it the next time you open this project in Claude Code.

Run directly (for manual testing, not how a real client launches it):
    python3 alr_mcp_server.py
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from alr.models import Privacy, Risk, TaskEnvelope
from alr.pipeline import Pipeline

mcp = FastMCP("adaptive-llm-router")

_pipeline: Optional[Pipeline] = None


def _get_pipeline() -> Pipeline:
    """Lazily built, shared across calls within one server process so
    repeated tool calls in the same session accumulate in one trace
    store instead of each starting a fresh SQLite file.
    """
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


@mcp.tool()
def route_task(
    task: str,
    risk: str = "low",
    privacy: str = "public",
    context_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Route a standalone, self-contained task through the Adaptive LLM
    Router's tool/local/frontier tiering instead of answering it directly
    in-conversation. Best for well-defined sub-tasks that don't need this
    conversation's context — ALR may resolve it for free via a
    deterministic tool or a local model instead of spending a frontier
    call, and always reports real cost/savings for that one response.

    Args:
        task: The task text.
        risk: "low" | "medium" | "high" — high risk skips straight to the
            frontier tier and may require policy approval.
        privacy: "public" | "private" | "restricted" — restricted never
            leaves local infrastructure, regardless of complexity.
        context_text: Optional raw evidence (logs, file contents) to
            attach — compressed via the Context Firewall before it ever
            reaches a frontier call.
    """
    payload = {"context_text": context_text} if context_text else {}
    envelope = TaskEnvelope(
        task=task, risk=Risk(risk), privacy=Privacy(privacy), payload=payload,
    )
    result = _get_pipeline().run(envelope)
    return {
        "route": result.route.value,
        "model": result.model,
        "success": result.success,
        "output": result.output,
        "escalated": result.escalated,
        "escalation_reason": result.escalation_reason,
        "cost": result.cost,
        "cost_saved": result.cost_saved,
        "cost_saved_pct": result.cost_saved_pct,
        "tokens_saved": result.tokens_saved,
        "latency_ms": result.latency_ms,
    }


@mcp.tool()
def get_alr_usage_summary() -> Dict[str, Any]:
    """Return the Adaptive LLM Router's accumulated usage/cost/savings
    rollup from its trace store: how many tasks have been routed through
    it in this server's lifetime, by tier, with total cost and savings.
    """
    return _get_pipeline().trace_store.summary_metrics()


if __name__ == "__main__":
    mcp.run()
