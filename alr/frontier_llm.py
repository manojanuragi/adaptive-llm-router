"""Tier 2 — frontier LLM connector. Section 5 (Tier 2) of the design doc.

Shells out to the `claude` CLI (Claude Code) in non-interactive print mode
so Tier-2 calls reuse the user's existing Claude Code login/subscription
instead of requiring a separate ANTHROPIC_API_KEY and raw Messages-API
billing. `--restricted` disables Claude Code's own Bash/file tools so this
is a plain single-turn completion, not an agent run. Provider-agnostic by
design per section 5 — add a sibling module (e.g. openai_llm.py) and
register it in the registry's `provider` field to support another
frontier provider without touching the router.

Requires the `claude` binary on PATH and an active `claude login` session.
For containerized/headless deployments where interactive login isn't an
option, swap this module for a raw-HTTP Messages-API connector using
ANTHROPIC_API_KEY instead (same function signatures, so pipeline.py
doesn't change).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import ModelSpec

CLAUDE_CLI_BIN = "claude"


class FrontierLLMError(Exception):
    pass


@dataclass
class FrontierLLMResponse:
    text: str
    raw: Dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None  # authoritative cost from the CLI, when available


def call_frontier_llm(
    spec: ModelSpec,
    prompt: str,
    api_key: Optional[str] = None,  # unused by the CLI transport; kept for interface stability
    max_tokens: int = 1024,
    timeout_s: float = 120.0,
) -> FrontierLLMResponse:
    if shutil.which(CLAUDE_CLI_BIN) is None:
        raise FrontierLLMError(
            f"'{CLAUDE_CLI_BIN}' CLI not found on PATH. Install Claude Code "
            "and run `claude login`, or swap this module for a raw "
            "Messages-API connector (see the module docstring)."
        )

    cmd = [
        CLAUDE_CLI_BIN, "-p", prompt,
        "--output-format", "json",
        "--model", spec.model_name,
        "--restricted",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as e:
        raise FrontierLLMError(f"claude CLI timed out after {timeout_s}s") from e

    if proc.returncode != 0:
        raise FrontierLLMError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FrontierLLMError(f"Could not parse claude CLI output: {e}\n{proc.stdout[:500]}") from e

    if data.get("is_error"):
        raise FrontierLLMError(f"claude CLI reported an error: {data.get('result')}")

    usage = data.get("usage", {})
    return FrontierLLMResponse(
        text=data.get("result", ""),
        raw=data,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cost_usd=data.get("total_cost_usd"),
    )


def estimate_cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    """Fallback estimate from the registry's blended rate — only used when
    a connector doesn't report authoritative cost (e.g. FrontierLLMResponse.cost_usd
    is None). The CLI transport reports real cost directly.
    """
    total_1k = (input_tokens + output_tokens) / 1000.0
    return round(total_1k * spec.cost_per_1k_tokens, 6)


# --- Async wrapper -----------------------------------------------------
import asyncio  # noqa: E402

from .retry import CircuitBreaker, retry_async  # noqa: E402

frontier_llm_breaker = CircuitBreaker(failure_threshold=5, reset_after_s=30.0)


async def call_frontier_llm_async(
    spec: ModelSpec,
    prompt: str,
    api_key: Optional[str] = None,
    max_tokens: int = 1024,
    timeout_s: float = 120.0,
    max_attempts: int = 3,
) -> FrontierLLMResponse:
    async def attempt():
        return await asyncio.to_thread(call_frontier_llm, spec, prompt, api_key, max_tokens, timeout_s)

    return await retry_async(
        attempt,
        retryable_exceptions=(FrontierLLMError,),
        max_attempts=max_attempts,
        base_delay_s=1.0,
        breaker=frontier_llm_breaker,
    )
