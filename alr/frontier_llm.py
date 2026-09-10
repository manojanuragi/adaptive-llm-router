"""Tier 2 — frontier LLM connector. Section 5 (Tier 2) of the design doc.

Talks directly to the Anthropic Messages API over urllib (no `anthropic`
SDK dependency required). Provider-agnostic by design per section 5 —
add a sibling module (e.g. openai_llm.py) and register it in the
registry's `provider` field to support another frontier provider without
touching the router.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import ModelSpec

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class FrontierLLMError(Exception):
    pass


@dataclass
class FrontierLLMResponse:
    text: str
    raw: Dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0


def call_frontier_llm(
    spec: ModelSpec,
    prompt: str,
    api_key: Optional[str] = None,
    max_tokens: int = 1024,
    timeout_s: float = 60.0,
) -> FrontierLLMResponse:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise FrontierLLMError(
            "ANTHROPIC_API_KEY not set. Export it or pass api_key= explicitly."
        )

    body = json.dumps({
        "model": spec.model_name,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise FrontierLLMError(f"Anthropic API error {e.code}: {e.read().decode('utf-8')}") from e
    except urllib.error.URLError as e:
        raise FrontierLLMError(f"Could not reach Anthropic API: {e}") from e

    text_parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
    usage = data.get("usage", {})
    return FrontierLLMResponse(
        text="\n".join(text_parts),
        raw=data,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def estimate_cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float:
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
    timeout_s: float = 60.0,
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
