#!/usr/bin/env python3
"""Zero-setup demo. Requires only PyYAML (`pip install pyyaml` if you
don't already have it) — no FastAPI, no Anthropic key, no Ollama needed.

Runs a handful of representative tasks through the router and prints the
routing decision for each, so you can see Tier 0 / 1 / 2 selection and
the escalation logic without standing up any servers.

To also see REAL local/frontier execution (not just routing decisions),
set up Ollama and/or ANTHROPIC_API_KEY and run pipeline_demo() below, or
just hit the FastAPI server (see api.py + README).
"""
from alr.analyzer import TaskAnalyzer
from alr.models import Privacy, Risk, TaskEnvelope
from alr.registry import ModelRegistry
from alr.router import AdaptiveRouter

SAMPLE_TASKS = [
    TaskEnvelope(task="git status"),
    TaskEnvelope(task="Summarize these deployment logs from the last hour"),
    TaskEnvelope(task="Design the architecture for a distributed payments system"),
    TaskEnvelope(task="Investigate why payment-service is returning 500 errors",
                 risk=Risk.HIGH, context_tokens=85_000),
    TaskEnvelope(task="Extract customer names and emails from this support ticket",
                 privacy=Privacy.RESTRICTED),
    TaskEnvelope(task="Explain what this function does"),
]


def main():
    registry = ModelRegistry()
    analyzer = TaskAnalyzer()
    router = AdaptiveRouter(registry)

    print(f"{'TASK':<62} {'ROUTE':<10} {'MODEL':<14} {'CONF':<5}")
    print("-" * 95)
    for env in SAMPLE_TASKS:
        features = analyzer.analyze(env)
        decision = router.route(env, features)
        task_preview = (env.task[:59] + "...") if len(env.task) > 62 else env.task
        print(f"{task_preview:<62} {decision.route.value:<10} {decision.model:<14} {decision.confidence:.2f}")
        for r in decision.reason:
            print(f"    reason: {r}")
    print()
    print("For real execution (actual local/frontier calls) + escalation, run:")
    print("  python3 pipeline_demo.py")


if __name__ == "__main__":
    main()
