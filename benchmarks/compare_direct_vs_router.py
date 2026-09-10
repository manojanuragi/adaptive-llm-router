"""Direct Claude (no router) vs. the Adaptive LLM Router — cost + accuracy.

Not part of the unittest suite (unittest discover ignores non-`test_*.py`
files, and this makes real Claude CLI calls that cost money) — a
reproducible, standalone comparison script. See the README section
"Benchmark: direct Claude vs. the adaptive router" for a worked run and
its caveats.

Requires: the `claude` CLI on PATH and an active `claude login` session
(see README "Using your existing Claude CLI login"), and optionally
Ollama running with the model configured in alr/config/models.yaml for a
real Tier-1 attempt (falls back to always escalating if unavailable).

Usage:
    python3 benchmarks/compare_direct_vs_router.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alr.frontier_llm import call_frontier_llm, FrontierLLMError
from alr.models import Privacy, Risk, TaskEnvelope
from alr.pipeline import Pipeline
from alr.registry import ModelRegistry

LOG_TASK = """Summarize these logs and extract all the errors with their exception type, as a JSON list of {"line": <text>, "exception_type": <type>}.

2026-09-10 10:00:01 INFO  Starting worker pool with 4 threads
2026-09-10 10:00:02 DEBUG Connected to database at db01.internal:5432
2026-09-10 10:00:05 INFO  Processing batch job_id=8841
2026-09-10 10:00:07 ERROR Failed to fetch user profile: KeyError: 'email'
2026-09-10 10:00:07 DEBUG Retrying fetch, attempt 2
2026-09-10 10:00:09 INFO  Batch job_id=8841 completed with 3 warnings
2026-09-10 10:00:12 INFO  Starting batch job_id=8842
2026-09-10 10:00:15 ERROR Connection to cache server timed out: ConnectionTimeoutError
2026-09-10 10:00:16 INFO  Falling back to direct database read
2026-09-10 10:00:20 DEBUG Cache warm-up scheduled for 10:15
2026-09-10 10:00:22 ERROR Invalid payload received: ValueError: could not convert string to float: 'N/A'
2026-09-10 10:00:23 INFO  Batch job_id=8842 completed with errors
2026-09-10 10:00:30 INFO  Shutting down worker pool
"""
LOG_GROUND_TRUTH = ["KeyError", "ConnectionTimeoutError", "ValueError"]

BUG_TASK = """Debug why this moving_average function is failing to include the last valid window in its output, and give me the corrected code.

def moving_average(nums, window):
    result = []
    for i in range(len(nums) - window):
        result.append(sum(nums[i:i+window]) / window)
    return result
"""
BUG_FIX_PATTERN = re.compile(r"range\(\s*len\(nums\)\s*-\s*window\s*\+\s*1\s*\)")

TASKS = [
    {
        "name": "log_extraction",
        "text": LOG_TASK,
        "grade": lambda out: sum(1 for e in LOG_GROUND_TRUTH if e in out) / len(LOG_GROUND_TRUTH),
    },
    {
        "name": "code_debugging",
        "text": BUG_TASK,
        "grade": lambda out: 1.0 if BUG_FIX_PATTERN.search(out) else 0.0,
    },
]


def run_direct_claude(registry: ModelRegistry, task_text: str) -> dict:
    """Ground truth for "just call Claude, no router" — same connector the
    router itself uses for its frontier tier, called directly."""
    spec = registry.get("claude")
    t0 = time.time()
    try:
        resp = call_frontier_llm(spec, task_text, max_tokens=800, timeout_s=120.0)
        return {
            "output": resp.text, "cost": resp.cost_usd or 0.0,
            "latency_ms": int((time.time() - t0) * 1000),
            "route": "claude_direct", "success": True,
        }
    except FrontierLLMError as e:
        return {
            "output": str(e), "cost": 0.0,
            "latency_ms": int((time.time() - t0) * 1000),
            "route": "claude_direct", "success": False,
        }


def run_router(pipeline: Pipeline, task_text: str) -> dict:
    envelope = TaskEnvelope(task=task_text, risk=Risk.LOW, privacy=Privacy.PUBLIC)
    t0 = time.time()
    result = pipeline.run(envelope)
    return {
        "output": str(result.output), "cost": result.cost or 0.0,
        "latency_ms": int((time.time() - t0) * 1000),
        "route": result.route.value, "escalated": result.escalated, "success": result.success,
    }


def main() -> None:
    registry = ModelRegistry()
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp_db:
        pipeline = Pipeline(db_path=tmp_db.name)

        results = []
        for task in TASKS:
            print(f"\n=== Task: {task['name']} ===")

            print("-- direct Claude (no router) --")
            direct = run_direct_claude(registry, task["text"])
            direct["accuracy"] = task["grade"](direct["output"])
            print(f"route=claude_direct cost=${direct['cost']:.5f} "
                  f"latency={direct['latency_ms']}ms accuracy={direct['accuracy']:.2f}")

            print("-- adaptive router --")
            routed = run_router(pipeline, task["text"])
            routed["accuracy"] = task["grade"](routed["output"])
            print(f"route={routed['route']} escalated={routed.get('escalated')} "
                  f"cost=${routed['cost']:.5f} latency={routed['latency_ms']}ms "
                  f"accuracy={routed['accuracy']:.2f}")

            results.append({"task": task["name"], "direct": direct, "routed": routed})

    print("\n\n========== SUMMARY ==========")
    total_direct_cost = sum(r["direct"]["cost"] for r in results)
    total_routed_cost = sum(r["routed"]["cost"] for r in results)
    total_direct_acc = sum(r["direct"]["accuracy"] for r in results) / len(results)
    total_routed_acc = sum(r["routed"]["accuracy"] for r in results) / len(results)

    for r in results:
        print(f"\n{r['task']}:")
        print(f"  direct : cost=${r['direct']['cost']:.5f}  accuracy={r['direct']['accuracy']:.2f}  "
              f"latency={r['direct']['latency_ms']}ms")
        print(f"  router : route={r['routed']['route']:<10} escalated={r['routed'].get('escalated')}  "
              f"cost=${r['routed']['cost']:.5f}  accuracy={r['routed']['accuracy']:.2f}  "
              f"latency={r['routed']['latency_ms']}ms")

    savings = total_direct_cost - total_routed_cost
    savings_pct = (savings / total_direct_cost * 100) if total_direct_cost else 0.0
    print(f"\nTOTAL direct-Claude cost : ${total_direct_cost:.5f}")
    print(f"TOTAL router cost        : ${total_routed_cost:.5f}")
    print(f"TOTAL savings            : ${savings:.5f} ({savings_pct:.1f}%)")
    print(f"AVG accuracy direct      : {total_direct_acc:.2f}")
    print(f"AVG accuracy router      : {total_routed_acc:.2f}")


if __name__ == "__main__":
    main()
