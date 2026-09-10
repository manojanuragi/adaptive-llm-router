"""Policy Engine — section 21 of the design doc.

"Never allow an LLM to bypass the permission layer." This sits between
routing/execution and the outside world: it classifies operations by
risk and returns ALLOW / DENY / REQUIRE_APPROVAL *before* anything runs.

This is the piece that was completely missing before — without it, the
router happily executes whatever tool or LLM output tells it to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional

from .models import TaskEnvelope


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reasons: List[str]


# High-risk operation patterns — section 21's explicit list:
# "git push, production deployment, deleting files, database migrations,
#  secrets access, infrastructure changes"
HIGH_RISK_PATTERNS = [
    (r"\bgit\s+push\b", "git push"),
    (r"\bdeploy(ment)?\b.*\bprod(uction)?\b|\bprod(uction)?\b.*\bdeploy", "production deployment"),
    (r"\brm\s+-rf\b|\bdelete\s+(all\s+)?files?\b|\bunlink\b", "file deletion"),
    (r"\bmigrat(e|ion)\b.*\bdatabase\b|\bdb\s+migrat", "database migration"),
    (r"\bsecrets?\b.*\baccess\b|\bapi[_\s]?key\b.*\b(read|expose|print|leak)\b", "secrets access"),
    (r"\binfrastructure\b.*\bchange\b|\bterraform\s+apply\b|\bkubectl\s+apply\b", "infrastructure change"),
    (r"\bdrop\s+table\b|\btruncate\s+table\b", "destructive database operation"),
]

# Tool names that are ALWAYS denied outright — never even reach approval,
# because this MVP has no execution sandbox to make them safe.
DENIED_TOOLS = {
    # currently empty by default; add tool names here to hard-block them
    # regardless of approval, e.g. "shell_exec" if you ever add one.
}

# Tool names that always require human approval, independent of text match.
APPROVAL_REQUIRED_TOOLS = {
    "test_execution",  # runs arbitrary configured commands — section 21
}


class PolicyEngine:
    """Callers should treat REQUIRE_APPROVAL as "do not execute automatically."
    The MVP has no approval-workflow UI; `approval_callback` lets you wire
    one in (Slack, a queue, a human clicking a button) without touching
    the pipeline. If no callback is provided, REQUIRE_APPROVAL == DENY.
    """

    def __init__(self, approval_callback: Optional[Callable[[TaskEnvelope, str], bool]] = None):
        self.approval_callback = approval_callback

    def evaluate(self, envelope: TaskEnvelope, tool_or_model: Optional[str] = None) -> PolicyResult:
        reasons: List[str] = []
        text = envelope.task.lower()

        if tool_or_model in DENIED_TOOLS:
            return PolicyResult(PolicyDecision.DENY, [f"tool '{tool_or_model}' is hard-denied by policy"])

        matched_labels = [label for pattern, label in HIGH_RISK_PATTERNS if re.search(pattern, text)]
        requires_approval = bool(matched_labels) or tool_or_model in APPROVAL_REQUIRED_TOOLS

        if not requires_approval:
            return PolicyResult(PolicyDecision.ALLOW, ["no high-risk pattern matched"])

        if matched_labels:
            reasons.append(f"matched high-risk pattern(s): {', '.join(matched_labels)}")
        if tool_or_model in APPROVAL_REQUIRED_TOOLS:
            reasons.append(f"tool '{tool_or_model}' always requires approval")

        if self.approval_callback is not None:
            approved = self.approval_callback(envelope, tool_or_model or "")
            if approved:
                reasons.append("approved by approval_callback")
                return PolicyResult(PolicyDecision.ALLOW, reasons)
            reasons.append("rejected by approval_callback")
            return PolicyResult(PolicyDecision.DENY, reasons)

        reasons.append("no approval_callback configured -> defaulting to deny")
        return PolicyResult(PolicyDecision.REQUIRE_APPROVAL, reasons)
