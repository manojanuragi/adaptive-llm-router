"""Task Analyzer — section 8 of the design doc.

Phase-1 MVP implementation: keyword/heuristic feature extraction, exactly
as the doc recommends ("Start here... rule-based, explainable, easy to
debug"). Swap analyze() for an embedding+classifier model in Phase 3
without touching the router, since it only depends on TaskFeatures.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .models import TaskEnvelope, TaskFeatures

# Tier-0 deterministic-tool patterns (section 5, Tier 0).
# (regex, tool_name, category)
TOOL_PATTERNS: List[Tuple[str, str, str]] = [
    (r"\bgit\s+status\b", "git_status", "tool_execution"),
    (r"\bgit\s+diff\b", "git_diff", "tool_execution"),
    (r"\bgit\s+log\b", "git_log", "tool_execution"),
    (r"\b(list|read)\s+files?\b", "file_listing", "tool_execution"),
    (r"\bgrep\b|\bsearch\s+for\s+(the\s+)?function\b", "grep_search", "retrieval"),
    (r"\brun\s+tests?\b", "test_execution", "tool_execution"),
    (r"\bparse\s+json\b", "json_parse", "data_extraction"),
    (r"\bparse\s+csv\b", "csv_parse", "data_extraction"),
]

# Category keyword weights used to estimate complexity when no tool matches.
# Higher weight => more likely to need frontier-tier reasoning.
CATEGORY_KEYWORDS: Dict[str, Tuple[List[str], float]] = {
    "architecture": (["architecture", "design a system", "distributed system", "system design"], 0.90),
    "security_review": (["security", "vulnerability", "exploit", "cve", "auth bypass"], 0.85),
    "code_debugging": (["debug", "root cause", "race condition", "why is", "failing", "bug"], 0.75),
    "code_generation": (["implement", "write code", "generate a patch", "refactor"], 0.65),
    "planning": (["plan", "roadmap", "break down", "multi-step"], 0.60),
    "log_analysis": (["logs", "log analysis", "stack trace"], 0.35),
    "summarization": (["summarize", "summary", "tl;dr"], 0.25),
    "classification": (["classify", "categorize", "label"], 0.25),
    "documentation": (["document", "docstring", "readme"], 0.20),
    "retrieval": (["find", "locate", "search"], 0.20),
    "data_extraction": (["extract", "convert to json", "structured"], 0.20),
}

CODE_HINTS = ["def ", "class ", "function", "```", "import ", "traceback", ".py", ".js", ".ts", "stack trace"]


class TaskAnalyzer:
    def analyze(self, envelope: TaskEnvelope) -> TaskFeatures:
        text = envelope.task.lower()

        # 1) Tier-0 tool match takes priority — "prefer deterministic tools
        #    whenever possible" (section 5).
        for pattern, tool_name, category in TOOL_PATTERNS:
            if re.search(pattern, text):
                return TaskFeatures(
                    category=category,
                    complexity=0.0,
                    reasoning_depth=0.0,
                    context_size=envelope.context_tokens,
                    is_code=True,
                    matched_tool=tool_name,
                )

        # 2) Otherwise score against category keywords.
        category, base_complexity = self._classify(text, envelope.task_type)

        complexity = base_complexity
        # Context size and declared risk both push complexity up — a long,
        # high-risk task is less likely to be safely handled locally even
        # if the keywords look mundane.
        if envelope.context_tokens > 50_000:
            complexity = min(1.0, complexity + 0.15)
        if envelope.risk.value == "high":
            complexity = min(1.0, complexity + 0.20)
        elif envelope.risk.value == "medium":
            complexity = min(1.0, complexity + 0.08)

        return TaskFeatures(
            category=category,
            complexity=round(complexity, 2),
            reasoning_depth=round(complexity, 2),  # simple MVP proxy; split out later
            context_size=envelope.context_tokens,
            is_code=any(h in text for h in CODE_HINTS),
            matched_tool=None,
        )

    def _classify(self, text: str, declared_type: Optional[str]) -> Tuple[str, float]:
        if declared_type and declared_type in {k for k in CATEGORY_KEYWORDS}:
            return declared_type, CATEGORY_KEYWORDS[declared_type][1]

        best_category, best_score, best_weight = "code_generation", 0, 0.5
        for category, (keywords, weight) in CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > best_score:
                best_category, best_score, best_weight = category, score, weight

        if best_score == 0:
            # Nothing matched — default to a moderate-complexity generic
            # reasoning task rather than silently assuming "easy".
            return "general_reasoning", 0.55
        return best_category, best_weight
