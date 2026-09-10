"""Context Manager / Context Firewall — sections 12 and 23.

Instead of dumping raw logs/files/git history at the frontier model,
compress evidence into a small structured object. This is the primary
differentiator called out in section 32-C and section 39-4.

Phase-1 MVP: deterministic truncation + regex extraction of obviously
important lines (errors, exceptions, tracebacks). Swap the `summarize`
step for a real Tier-1 local-LLM call once you're comfortable with the
extra hop's cost/latency — the interface (raw text in, Evidence out)
doesn't change.

`request_more_evidence` implements the progressive-disclosure loop from
section 23: the frontier model can ask for a specific raw slice back.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

ERROR_PATTERNS = [
    r".*\bError\b.*",
    r".*\bException\b.*",
    r".*\bTraceback\b.*",
    r".*\btimeout\b.*",
    r".*\bfailed\b.*",
    r".*\brefused\b.*",
]

MAX_RAW_CHARS_KEPT = 4000  # raw text is still stored, just not sent to the frontier


@dataclass
class Evidence:
    summary: Dict[str, object]
    raw_reference_id: str
    raw_store: Dict[str, str] = field(default_factory=dict)  # in-memory for MVP; swap for a real store

    def request_more_evidence(self, query: str) -> str:
        """Progressive disclosure — section 23.

        MVP: return the raw text if it contains the query term, else a
        capped slice around the first match. Swap for real retrieval
        (grep / vector search over the raw store) as the raw corpus grows.
        """
        raw = self.raw_store.get(self.raw_reference_id, "")
        idx = raw.lower().find(query.lower())
        if idx == -1:
            return "No matching raw evidence found for that query."
        start = max(0, idx - 200)
        end = min(len(raw), idx + 200)
        return raw[start:end]


class ContextManager:
    def compress(self, raw_text: str, reference_id: str) -> Evidence:
        lines = raw_text.splitlines()
        error_lines: List[str] = [
            ln for ln in lines if any(re.match(p, ln, re.IGNORECASE) for p in ERROR_PATTERNS)
        ]

        summary = {
            "total_lines": len(lines),
            "error_line_count": len(error_lines),
            "sample_errors": error_lines[:10],
            "raw_reference_id": reference_id,
            "note": "Raw evidence available on demand via request_more_evidence().",
        }

        evidence = Evidence(summary=summary, raw_reference_id=reference_id)
        evidence.raw_store[reference_id] = raw_text[: max(MAX_RAW_CHARS_KEPT, len(raw_text))]
        return evidence
