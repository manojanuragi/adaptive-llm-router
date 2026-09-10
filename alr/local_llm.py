"""Tier 1 — local LLM connector. Section 5 (Tier 1) of the design doc.

Talks to an Ollama server over plain urllib so this module has zero
third-party dependencies (works even before you `pip install` anything).
If the local server is unreachable, calls fail closed with
LocalLLMUnavailable so the pipeline can escalate to frontier instead of
crashing — see the "local inference too slow / unavailable" failure mode
in section 22.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import ModelSpec


class LocalLLMUnavailable(Exception):
    """Raised when the local inference server can't be reached."""


@dataclass
class LocalLLMResponse:
    text: str
    raw: Dict[str, Any]
    self_reported_confidence: Optional[float] = None
    input_tokens: int = 0
    output_tokens: int = 0


def call_local_llm(spec: ModelSpec, prompt: str, timeout_s: float = 20.0) -> LocalLLMResponse:
    """Call an Ollama-compatible /api/generate endpoint.

    Ask the model to end its answer with a confidence line so the
    Validator (section 10) has a signal to work with even before you wire
    up real calibration/self-consistency checks.
    """
    if not spec.endpoint:
        raise LocalLLMUnavailable(f"Model spec '{spec.name}' has no endpoint configured")

    system_suffix = (
        "\n\nEnd your response with a line exactly like: "
        "CONFIDENCE: 0.NN (your self-estimated confidence 0-1)."
    )
    url = f"{spec.endpoint.rstrip('/')}/api/generate"
    body = json.dumps({
        "model": spec.model_name,
        "prompt": prompt + system_suffix,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError) as e:
        raise LocalLLMUnavailable(f"Could not reach local LLM at {url}: {e}") from e

    text = data.get("response", "")
    confidence = _extract_confidence(text)
    # Ollama reports these when stream=False: prompt_eval_count = input
    # tokens, eval_count = output tokens. Best-effort — 0 if the server
    # doesn't report them (e.g. a non-Ollama /api/generate-compatible backend).
    return LocalLLMResponse(
        text=text,
        raw=data,
        self_reported_confidence=confidence,
        input_tokens=int(data.get("prompt_eval_count", 0) or 0),
        output_tokens=int(data.get("eval_count", 0) or 0),
    )


def _extract_confidence(text: str) -> Optional[float]:
    import re

    m = re.search(r"CONFIDENCE:\s*([01](?:\.\d+)?)", text)
    if m:
        try:
            return max(0.0, min(1.0, float(m.group(1))))
        except ValueError:
            return None
    return None


# --- Async wrapper -----------------------------------------------------
# Runs the sync urllib call in a worker thread so it doesn't block the
# event loop under FastAPI/uvicorn, wrapped with retry + circuit breaker
# so one flaky call doesn't hang a request for the full timeout and one
# dead Ollama instance doesn't degrade every concurrent request.
import asyncio  # noqa: E402

from .retry import CircuitBreaker, retry_async  # noqa: E402

local_llm_breaker = CircuitBreaker(failure_threshold=5, reset_after_s=30.0)


async def call_local_llm_async(
    spec: ModelSpec, prompt: str, timeout_s: float = 20.0, max_attempts: int = 2
) -> LocalLLMResponse:
    async def attempt():
        return await asyncio.to_thread(call_local_llm, spec, prompt, timeout_s)

    return await retry_async(
        attempt,
        retryable_exceptions=(LocalLLMUnavailable,),
        max_attempts=max_attempts,
        base_delay_s=0.5,
        breaker=local_llm_breaker,
    )
