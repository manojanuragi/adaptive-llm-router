"""Auth — API key verification.

Core logic (`verify_api_key`) is stdlib-only and unit-testable without
FastAPI. `require_api_key` at the bottom is the FastAPI dependency that
wraps it for use in api.py.

This is deliberately simple (static key set via env var), matching the
rest of the MVP's philosophy: correct and honest about what it is, not
gold-plated. For anything beyond a single team's internal use, swap this
for real auth (OAuth/JWT/mTLS) — see README deployment notes.
"""
from __future__ import annotations

import hmac
import os
from typing import Iterable, Optional, Set


def load_api_keys_from_env(var_name: str = "ALR_API_KEYS") -> Set[str]:
    """Comma-separated keys, e.g. ALR_API_KEYS=key1,key2"""
    raw = os.environ.get(var_name, "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def verify_api_key(provided: Optional[str], valid_keys: Iterable[str]) -> bool:
    """Constant-time comparison against each valid key to avoid leaking
    key length/prefix via timing side channels.
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


# --- FastAPI-specific wrapper (only imported by api.py) ---------------
def build_fastapi_dependency():
    from fastapi import Header, HTTPException, status

    valid_keys = load_api_keys_from_env()

    async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
        if not verify_api_key(x_api_key, valid_keys):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
        return x_api_key or "anonymous"

    return require_api_key
