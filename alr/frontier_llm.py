"""Tier 2 — frontier LLM connector. Section 5 (Tier 2) of the design doc.

Two transports, selectable per environment without touching pipeline.py
(same public functions/signatures either way):

  - "cli" (default) — shells out to the `claude` CLI (Claude Code) in
    non-interactive print mode, reusing the user's existing Claude Code
    login/subscription instead of a separate ANTHROPIC_API_KEY. Best for
    running the router directly on a machine with an interactive
    `claude login` session. `--restricted` disables Claude Code's own
    Bash/file tools so this is a plain single-turn completion, not an
    agent run.
  - "api" — calls the Anthropic Messages API directly over HTTPS
    (stdlib `urllib`, no SDK dependency) using ANTHROPIC_API_KEY. This is
    the transport for headless environments with no interactive login —
    Docker, Kubernetes, CI/scheduled jobs (see
    .github/workflows/benchmark.yml).

Pick the transport with the ALR_FRONTIER_TRANSPORT env var ("cli" or
"api"), or set a specific model's `transport:` field in models.yaml to
pin it regardless of the env var. Provider-agnostic by design per
section 5 — add a sibling module (e.g. openai_llm.py) and register it in
the registry's `provider` field to support another frontier provider
without touching the router.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .registry import ModelRegistry, ModelSpec

CLAUDE_CLI_BIN = "claude"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
TRANSPORT_ENV_VAR = "ALR_FRONTIER_TRANSPORT"
DEFAULT_TRANSPORT = "cli"


class FrontierLLMError(Exception):
    pass


class FrontierAuthError(FrontierLLMError):
    """Raised by validate_frontier_auth() at startup, not per-request —
    lets a deployer fail fast with a clear message instead of discovering
    a missing/invalid credential on the first real frontier-tier call.
    """


@dataclass
class FrontierLLMResponse:
    text: str
    raw: Dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None  # authoritative cost, when the transport reports one


def _resolve_transport(spec: ModelSpec) -> str:
    """`spec.transport` (per-model override in models.yaml) wins; otherwise
    ALR_FRONTIER_TRANSPORT; otherwise "cli" for backwards compatibility.
    """
    if spec.transport:
        return spec.transport.lower()
    return os.environ.get(TRANSPORT_ENV_VAR, DEFAULT_TRANSPORT).lower()


def validate_frontier_auth(registry: ModelRegistry) -> None:
    """Fail fast, at startup, instead of letting a missing/invalid
    frontier credential surface as a confusing per-request error later —
    exactly the failure mode this project hit in practice (an opaque
    "claude CLI exited 1:" with no detail; see _call_frontier_llm_cli's
    stdout-parsing fix). Each deployer is expected to configure their own
    auth — no credentials ship with this repo.

    Only checks transports actually reachable from the registry: a
    deployment with no cloud-capable model registered at all (pure
    local/tool-only, e.g. for privacy=restricted workloads) needs no
    Claude auth and is left alone — this must not turn into a hard
    requirement for setups that intentionally never call the frontier tier.

    Raises FrontierAuthError with an actionable message; callers (e.g.
    alr/api.py's lifespan) decide whether that's fatal.
    """
    cloud_models = registry.cloud_capable_models()
    if not cloud_models:
        return

    transports = {_resolve_transport(m) for m in cloud_models}

    if "api" in transports and not os.environ.get("ANTHROPIC_API_KEY"):
        raise FrontierAuthError(
            "ALR_FRONTIER_TRANSPORT=api (or a model's transport: api in models.yaml) "
            "is configured, but ANTHROPIC_API_KEY is not set. Set it, or switch to "
            "the default 'cli' transport (see README 'Two ways to run Tier 2')."
        )

    if "cli" in transports:
        if shutil.which(CLAUDE_CLI_BIN) is None:
            raise FrontierAuthError(
                f"The '{CLAUDE_CLI_BIN}' CLI is required for the default 'cli' "
                "frontier transport but isn't on PATH. Install Claude Code, or set "
                "ALR_FRONTIER_TRANSPORT=api with ANTHROPIC_API_KEY instead."
            )
        has_oauth_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        is_interactive = sys.stdin.isatty()
        if not has_oauth_token and not is_interactive:
            # Headless (no TTY — a container, systemd unit, CI runner) with
            # neither an OAuth token nor an interactive session to fall
            # back on: there is no way this will authenticate. An
            # interactive TTY is allowed through without a hard guarantee
            # a `claude login` session actually exists, since that can
            # only be confirmed with a real network call.
            raise FrontierAuthError(
                "No frontier auth configured for this headless environment. Each "
                "deployer needs their own auth — set CLAUDE_CODE_OAUTH_TOKEN (from "
                "`claude setup-token`, billed against your own Claude Pro/Max "
                "subscription) or ANTHROPIC_API_KEY with ALR_FRONTIER_TRANSPORT=api. "
                "See README 'Two ways to run Tier 2'."
            )


def call_frontier_llm(
    spec: ModelSpec,
    prompt: str,
    api_key: Optional[str] = None,
    max_tokens: int = 1024,
    timeout_s: float = 120.0,
) -> FrontierLLMResponse:
    transport = _resolve_transport(spec)
    if transport == "api":
        return _call_frontier_llm_api(
            spec, prompt,
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            max_tokens=max_tokens, timeout_s=timeout_s,
        )
    if transport == "cli":
        return _call_frontier_llm_cli(spec, prompt, max_tokens=max_tokens, timeout_s=timeout_s)
    raise FrontierLLMError(
        f"Unknown frontier transport {transport!r} (expected 'cli' or 'api') — "
        f"check {TRANSPORT_ENV_VAR} or the model's `transport` field in models.yaml."
    )


def _call_frontier_llm_cli(
    spec: ModelSpec,
    prompt: str,
    max_tokens: int = 1024,
    timeout_s: float = 120.0,
) -> FrontierLLMResponse:
    if shutil.which(CLAUDE_CLI_BIN) is None:
        raise FrontierLLMError(
            f"'{CLAUDE_CLI_BIN}' CLI not found on PATH. Install Claude Code "
            "and run `claude login`, or set ALR_FRONTIER_TRANSPORT=api with "
            "ANTHROPIC_API_KEY for a headless environment."
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

    # The CLI reports auth/API errors (e.g. an invalid or expired
    # CLAUDE_CODE_OAUTH_TOKEN) as an `is_error` JSON payload on *stdout*
    # even when it exits non-zero — stderr is often empty in that case.
    # Parse stdout first regardless of return code so that detail isn't
    # lost; only fall back to the bare exit code + stderr if stdout isn't
    # parseable JSON at all.
    data = None
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = None

    if proc.returncode != 0:
        if data is not None and data.get("is_error"):
            status = data.get("api_error_status")
            suffix = f" (HTTP {status})" if status else ""
            raise FrontierLLMError(f"claude CLI reported an error{suffix}: {data.get('result')}")
        raise FrontierLLMError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")

    if data is None:
        raise FrontierLLMError(f"Could not parse claude CLI output: {proc.stdout[:500]}")

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


def _call_frontier_llm_api(
    spec: ModelSpec,
    prompt: str,
    api_key: Optional[str],
    max_tokens: int = 1024,
    timeout_s: float = 120.0,
) -> FrontierLLMResponse:
    """Raw Anthropic Messages API call — stdlib urllib only, no `anthropic`
    SDK dependency, consistent with local_llm.py's Ollama connector. Used
    for headless environments (containers, CI) where an interactive
    `claude login` session isn't available.
    """
    if not api_key:
        raise FrontierLLMError(
            f"{TRANSPORT_ENV_VAR}=api requires ANTHROPIC_API_KEY to be set "
            "(or pass api_key explicitly)."
        )

    body = json.dumps({
        "model": spec.model_name,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise FrontierLLMError(f"Anthropic API returned {e.code}: {detail[:500]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise FrontierLLMError(f"Could not reach Anthropic API: {e}") from e
    except json.JSONDecodeError as e:
        raise FrontierLLMError(f"Could not parse Anthropic API response: {e}") from e

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    usage = data.get("usage", {})
    return FrontierLLMResponse(
        text=text,
        raw=data,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        # The Messages API doesn't return a billed-dollars figure the way
        # the Claude CLI's JSON output does — pipeline.py falls back to
        # estimate_cost() below whenever cost_usd is None.
        cost_usd=None,
    )


def estimate_cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    """Fallback estimate from the registry's blended rate — used whenever a
    connector doesn't report authoritative cost (always true for the "api"
    transport; only true for "cli" if total_cost_usd is ever missing).
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
