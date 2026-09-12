"""Auth — API key verification, with per-caller identity for usage tracking.

Core logic (`verify_api_key`, `load_api_keys_from_env`) is stdlib-only
and unit-testable without FastAPI. `require_api_key` at the bottom is the
FastAPI dependency that wraps it for use in api.py.

This is deliberately simple (static key set via env var), matching the
rest of the MVP's philosophy: correct and honest about what it is, not
gold-plated. For anything beyond a single team's internal use, swap this
for real auth (OAuth/JWT/mTLS) — see README deployment notes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Dict, Iterable, Optional


def load_api_keys_from_env(var_name: str = "ALR_API_KEYS") -> Dict[str, str]:
    """Comma-separated keys, each optionally labeled with a caller
    identity: `ALR_API_KEYS=key1:alice,key2:bob`. A plain key with no
    `:label` (`ALR_API_KEYS=key1,key2`, the original format) is still
    accepted, but its identity is a short, non-reversible hash of the
    key — never the key itself.

    That distinction matters: the resolved identity is echoed in
    `/execute` responses, persisted into the trace store, and returned in
    full via `GET /metrics`'s `usage_by_caller` to any caller holding a
    single valid key (see "Per-caller usage tracking" in the README —
    that endpoint is intentionally a shared team dashboard, not scoped
    per requester). Using the raw key as the identity would leak one
    caller's live credential to every other caller through that
    dashboard — a real vulnerability found via security review, not a
    theoretical one.

    The label is a per-caller identity used only for usage tracking (see
    `ExecutionResult.caller_id` / `TraceStore` — it lets you see
    `requests_by_caller`/cost-by-caller breakdowns without every caller
    sharing one anonymous bucket). Pick a non-sensitive label — a name, a
    GitHub username, a team name — never something secret, since it's
    stored in trace data and returned in API responses.

    Returns a dict of {key: identity} rather than a bare set so callers
    can look up who a validated key belongs to.
    """
    raw = os.environ.get(var_name, "")
    keys: Dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, identity = entry.partition(":")
        key = key.strip()
        if sep and identity.strip():
            keys[key] = identity.strip()
        else:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
            keys[key] = f"unlabeled-{digest}"
    return keys


def verify_api_key(provided: Optional[str], valid_keys: Iterable[str]) -> bool:
    """Constant-time comparison against each valid key to avoid leaking
    key length/prefix via timing side channels. `valid_keys` may be a
    dict (as returned by `load_api_keys_from_env`) — iterating a dict
    yields its keys, so this works unchanged either way.
    """
    valid_keys = list(valid_keys)
    if not valid_keys:
        # No keys configured at all -> auth is off. This is intentional
        # for local dev (see README) but MUST be set in any real deployment;
        # api.py logs a startup warning when this is the case.
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, k) for k in valid_keys)


def resolve_caller_id(provided: Optional[str], valid_keys: Dict[str, str]) -> str:
    """The label to attach to this request's trace/usage data. Never
    returns the raw secret key itself — only its configured identity
    label, or "anonymous" when auth is off or the key carries no label.
    """
    if not valid_keys or not provided:
        return "anonymous"
    return valid_keys.get(provided, "anonymous")


# --- FastAPI-specific wrapper (only imported by api.py) ---------------
def build_fastapi_dependency():
    from fastapi import Header, HTTPException, status

    valid_keys = load_api_keys_from_env()

    async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
        if not verify_api_key(x_api_key, valid_keys):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
        return resolve_caller_id(x_api_key, valid_keys)

    return require_api_key
