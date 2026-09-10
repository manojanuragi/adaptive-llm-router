#!/usr/bin/env python3
"""Runs the FULL pipeline with real execution: analyze -> route -> execute
-> validate -> escalate -> trace.

Needs at least one of:
  - Ollama running locally with the model set in alr/config/models.yaml
    (`ollama pull qwen2.5-coder:32b` or edit the yaml to a model you have)
  - ANTHROPIC_API_KEY set in your environment

If neither is available for a given task's chosen tier, the pipeline
returns success=False with the error in `output` rather than crashing —
check that before assuming something's broken.
"""
import os

from alr.models import Risk, TaskEnvelope
from alr.pipeline import Pipeline

TASKS = [
    TaskEnvelope(task="git status"),
    TaskEnvelope(task="Summarize this log excerpt: ERROR ConnectionTimeout at payment/client.py:42, "
                       "retried 3 times, connection pool exhausted."),
    TaskEnvelope(task="What is the time complexity of quicksort in the average case?"),
]


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("(ANTHROPIC_API_KEY not set — frontier-tier tasks will fail gracefully. "
              "export it to see real Claude calls.)\n")

    pipeline = Pipeline()
    for env in TASKS:
        result = pipeline.run(env)
        print(f"task: {env.task[:70]}")
        print(f"  route={result.route.value} model={result.model} success={result.success} "
              f"confidence={result.confidence:.2f} escalated={result.escalated} "
              f"latency_ms={result.latency_ms} cost=${result.cost}")
        print(f"  output: {str(result.output)[:200]}")
        print()

    print("Rollup metrics:", pipeline.trace_store.summary_metrics())


if __name__ == "__main__":
    main()
