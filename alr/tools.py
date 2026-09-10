"""Tier 0 — deterministic tools. Section 5 (Tier 0) of the design doc.

No LLM involved. Each function takes the TaskEnvelope payload and returns
a plain result. Add new tools here and register them in TOOL_REGISTRY;
the router only needs the tool name to dispatch, from analyzer.TOOL_PATTERNS.

All filesystem-touching tools go through `_safe_path`, which resolves the
requested path and rejects anything that escapes ALR_WORKSPACE_ROOT (env
var, defaults to cwd). This closes the path-traversal gap: a payload of
{"path": "../../etc/passwd"} or an absolute path outside the workspace
is rejected before any subprocess runs.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict

WORKSPACE_ROOT = Path(os.environ.get("ALR_WORKSPACE_ROOT", ".")).resolve()

# Commands allowed for the test_execution tool. Arbitrary shell strings
# from a task payload are NOT executed — only a fixed allowlist, split
# safely with shlex rather than passed to a shell.
ALLOWED_TEST_COMMANDS = {"pytest", "pytest -q", "npm test", "go test ./..."}


class UnsafePathError(Exception):
    pass


def _safe_path(raw_path: str) -> Path:
    """Resolve `raw_path` relative to WORKSPACE_ROOT and refuse to return
    anything outside it (blocks '../' traversal and absolute-path escape).
    """
    candidate = (WORKSPACE_ROOT / raw_path).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise UnsafePathError(
            f"Path '{raw_path}' resolves outside the workspace root ({WORKSPACE_ROOT})"
        )
    return candidate


def _run(cmd: list) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=str(WORKSPACE_ROOT))
        return out.stdout.strip() or out.stderr.strip()
    except FileNotFoundError:
        return f"error: command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return "error: command timed out after 10s"
    except Exception as e:  # noqa: BLE001 - surface any tool failure to the caller
        return f"error: {e}"


def git_status(payload: Dict[str, Any]) -> str:
    return _run(["git", "status", "--porcelain=v1"])


def git_diff(payload: Dict[str, Any]) -> str:
    return _run(["git", "diff"])


def git_log(payload: Dict[str, Any]) -> str:
    return _run(["git", "log", "--oneline", "-n", str(payload.get("n", 10))])


def file_listing(payload: Dict[str, Any]) -> str:
    safe = _safe_path(payload.get("path", "."))
    return _run(["ls", "-la", str(safe)])


def grep_search(payload: Dict[str, Any]) -> str:
    pattern = payload.get("pattern", "")
    if not pattern:
        return "error: 'pattern' required in payload for grep_search"
    safe = _safe_path(payload.get("path", "."))
    return _run(["grep", "-rn", "--include=*.py", pattern, str(safe)])


def test_execution(payload: Dict[str, Any]) -> str:
    cmd_str = payload.get("command", "pytest -q")
    if cmd_str not in ALLOWED_TEST_COMMANDS:
        return (
            f"error: command '{cmd_str}' is not in the test-command allowlist "
            f"({sorted(ALLOWED_TEST_COMMANDS)}). This tool also requires policy "
            f"approval — see alr/policy.py APPROVAL_REQUIRED_TOOLS."
        )
    return _run(shlex.split(cmd_str))


def json_parse(payload: Dict[str, Any]) -> Any:
    raw = payload.get("raw", "")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"invalid json: {e}"}


def csv_parse(payload: Dict[str, Any]) -> Any:
    import csv
    import io

    raw = payload.get("raw", "")
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)


TOOL_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "file_listing": file_listing,
    "grep_search": grep_search,
    "test_execution": test_execution,
    "json_parse": json_parse,
    "csv_parse": csv_parse,
}


def run_tool(tool_name: str, payload: Dict[str, Any]) -> Any:
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        raise KeyError(f"No Tier-0 tool registered for '{tool_name}'")
    return fn(payload)
