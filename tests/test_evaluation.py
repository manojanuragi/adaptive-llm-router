"""Tests for alr/evaluation.py — sections 16 (Observability), 17
(Evaluation Framework) and 18 (False-Local Rate) of the design doc.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.evaluation import (
    compare_baselines,
    compute_evaluation_metrics,
    pass_at_k,
    pass_at_k_from_attempts,
    run_baseline,
)
from alr.frontier_llm import FrontierLLMResponse
from alr.local_llm import LocalLLMResponse
from alr.models import RouteTier, TaskEnvelope
from alr.pipeline import Pipeline
from alr.registry import ModelRegistry


def _row(**overrides):
    base = dict(
        trace_id="t", task_type="log_analysis", complexity=0.3, risk="low",
        selected_route="local", selected_model="local-coder", estimated_quality=0.9,
        confidence=0.9, success=True, escalated=False, escalation_reason=None,
        actual_cost=0.0, latency_ms=100, input_tokens=0, output_tokens=0,
        context_tokens_before=None, context_tokens_after=None,
    )
    base.update(overrides)
    return base


class TestComputeEvaluationMetrics(unittest.TestCase):
    def test_empty_rows(self):
        m = compute_evaluation_metrics([])
        self.assertEqual(m.total_requests, 0)
        self.assertIsNone(m.task_success_rate)
        self.assertEqual(m.requests_by_route, {})

    def test_basic_aggregates(self):
        rows = [
            _row(trace_id="1", selected_route="tool", selected_model="git_status",
                 success=True, latency_ms=10, actual_cost=0.0),
            _row(trace_id="2", selected_route="local", success=True, latency_ms=200, actual_cost=0.0,
                 input_tokens=100, output_tokens=20),
            _row(trace_id="3", selected_route="frontier", selected_model="claude", success=True,
                 latency_ms=2000, actual_cost=0.05, input_tokens=500, output_tokens=100),
            _row(trace_id="4", selected_route="frontier", selected_model="claude", success=False,
                 latency_ms=3000, actual_cost=0.06, input_tokens=600, output_tokens=0),
        ]
        m = compute_evaluation_metrics(rows)

        self.assertEqual(m.total_requests, 4)
        self.assertEqual(m.requests_by_route, {"tool": 1, "local": 1, "frontier": 2})
        self.assertAlmostEqual(m.task_success_rate, 0.75)
        self.assertAlmostEqual(m.local_success_rate, 1.0)
        self.assertAlmostEqual(m.frontier_success_rate, 0.5)
        # model_failure_rate is over local+frontier only (excludes tool tier)
        self.assertAlmostEqual(m.model_failure_rate, 1 / 3, places=4)
        self.assertAlmostEqual(m.total_cost, 0.11)
        self.assertEqual(m.frontier_tokens_total, 500 + 100 + 600 + 0)
        self.assertEqual(m.local_tokens_total, 120)
        # cost_per_successful_task: total_cost / successes(3)
        self.assertAlmostEqual(m.cost_per_successful_task, 0.11 / 3, places=5)

    def test_latency_percentiles(self):
        rows = [_row(trace_id=str(i), latency_ms=v) for i, v in enumerate([100, 200, 300, 400, 500])]
        m = compute_evaluation_metrics(rows)
        self.assertEqual(m.latency_p50_ms, 300)
        self.assertEqual(m.latency_p95_ms, 500)

    def test_escalation_rate(self):
        rows = [
            _row(trace_id="1", escalated=False),
            _row(trace_id="2", escalated=True),
            _row(trace_id="3", escalated=True),
            _row(trace_id="4", escalated=False),
        ]
        m = compute_evaluation_metrics(rows)
        self.assertAlmostEqual(m.escalation_rate, 0.5)

    def test_context_compression_ratio(self):
        rows = [
            _row(trace_id="1", selected_route="frontier", context_tokens_before=1000, context_tokens_after=100),
            _row(trace_id="2", selected_route="frontier", context_tokens_before=2000, context_tokens_after=400),
            _row(trace_id="3", selected_route="local"),  # no context fields -> excluded
        ]
        m = compute_evaluation_metrics(rows)
        self.assertEqual(m.context_samples, 2)
        self.assertAlmostEqual(m.context_compression_ratio, (0.1 + 0.2) / 2)

    def test_quality_per_dollar_and_second(self):
        rows = [
            _row(trace_id="1", success=True, actual_cost=0.10, latency_ms=1000),
            _row(trace_id="2", success=False, actual_cost=0.10, latency_ms=1000),
        ]
        m = compute_evaluation_metrics(rows)
        # task_success_rate=0.5, avg_cost_per_task=0.10, avg_latency_s=1.0
        self.assertAlmostEqual(m.quality_per_dollar, 5.0)
        self.assertAlmostEqual(m.quality_per_second, 0.5)

    def test_false_local_rate_proxy(self):
        rows = [
            _row(trace_id="1", selected_route="local", success=True, escalated=False),
            _row(trace_id="2", selected_route="local", success=False, escalated=False),
            _row(trace_id="3", selected_route="local", success=True, escalated=True),
        ]
        m = compute_evaluation_metrics(rows)
        # 2 of 3 local rows either failed or were escalated
        self.assertAlmostEqual(m.false_local_rate, 2 / 3, places=4)

    def test_false_frontier_rate_without_registry_is_none(self):
        rows = [_row(trace_id="1", selected_route="frontier", selected_model="claude", risk="low", complexity=0.2)]
        m = compute_evaluation_metrics(rows, registry=None)
        self.assertIsNone(m.false_frontier_rate)

    def test_false_frontier_rate_with_registry(self):
        registry = ModelRegistry()
        # log_analysis has a high local prior (0.90) in the shipped models.yaml,
        # well above the 0.70 confidence threshold, and this row wasn't a hard
        # constraint (risk=low, complexity below threshold) -> false-frontier candidate.
        rows = [
            _row(trace_id="1", selected_route="frontier", selected_model="claude",
                 task_type="log_analysis", risk="low", complexity=0.2),
            # architecture has a low local prior (0.50) -> not a false-frontier candidate
            _row(trace_id="2", selected_route="frontier", selected_model="claude",
                 task_type="architecture", risk="low", complexity=0.2),
            # risk=high is a hard constraint -> correct by policy regardless of prior
            _row(trace_id="3", selected_route="frontier", selected_model="claude",
                 task_type="log_analysis", risk="high", complexity=0.9),
        ]
        m = compute_evaluation_metrics(rows, registry=registry)
        self.assertAlmostEqual(m.false_frontier_rate, 1 / 3, places=4)


class TestPassAtK(unittest.TestCase):
    def test_all_pass(self):
        self.assertEqual(pass_at_k(n=5, c=5, k=1), 1.0)

    def test_none_pass(self):
        self.assertEqual(pass_at_k(n=5, c=0, k=1), 0.0)

    def test_partial_pass_k1(self):
        # n=5,c=1,k=1: probability a single random draw is the one success = 1/5
        self.assertAlmostEqual(pass_at_k(n=5, c=1, k=1), 0.2)

    def test_from_attempts_averages_across_tasks(self):
        result = pass_at_k_from_attempts(
            {"t1": [True, False, False], "t2": [False, False, False]}, k=1
        )
        # t1: pass_at_k(3,1,1)=1-C(2,1)/C(3,1)=1/3 ; t2: pass_at_k(3,0,1)=0
        self.assertAlmostEqual(result, (1 / 3 + 0.0) / 2, places=4)

    def test_from_attempts_skips_tasks_with_too_few_attempts(self):
        result = pass_at_k_from_attempts({"t1": [True]}, k=2)
        self.assertIsNone(result)


class TestRunBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.pipeline = Pipeline(db_path=self.tmp_db.name)
        self.envelopes = [
            TaskEnvelope(task="git status"),
            TaskEnvelope(task="Summarize these deployment logs"),
        ]

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    @mock.patch("alr.pipeline.call_local_llm")
    def test_local_only_forces_non_tool_tasks_to_local(self, mock_local):
        mock_local.return_value = LocalLLMResponse(text="ok CONFIDENCE: 0.95", raw={}, self_reported_confidence=0.95)
        metrics = run_baseline(self.pipeline, self.envelopes, mode="local_only")
        self.assertEqual(metrics.requests_by_route.get("tool"), 1)
        self.assertEqual(metrics.requests_by_route.get("local"), 1)
        self.assertNotIn("frontier", metrics.requests_by_route)

    @mock.patch("alr.pipeline.call_frontier_llm")
    def test_frontier_only_forces_non_tool_tasks_to_frontier(self, mock_frontier):
        mock_frontier.return_value = FrontierLLMResponse(text="ok", raw={}, input_tokens=10, output_tokens=5)
        metrics = run_baseline(self.pipeline, self.envelopes, mode="frontier_only")
        self.assertEqual(metrics.requests_by_route.get("tool"), 1)
        self.assertEqual(metrics.requests_by_route.get("frontier"), 1)
        self.assertNotIn("local", metrics.requests_by_route)

    def test_run_baseline_does_not_touch_trace_store(self):
        run_baseline(self.pipeline, [TaskEnvelope(task="git status")], mode="adaptive")
        self.assertEqual(self.pipeline.trace_store.summary_metrics(), {"total_requests": 0})

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            run_baseline(self.pipeline, self.envelopes, mode="not_a_real_mode")

    @mock.patch("alr.pipeline.call_frontier_llm")
    @mock.patch("alr.pipeline.call_local_llm")
    def test_compare_baselines_returns_all_requested_modes(self, mock_local, mock_frontier):
        mock_local.return_value = LocalLLMResponse(text="ok CONFIDENCE: 0.95", raw={}, self_reported_confidence=0.95)
        mock_frontier.return_value = FrontierLLMResponse(text="ok", raw={}, input_tokens=10, output_tokens=5)
        results = compare_baselines(self.pipeline, self.envelopes)
        self.assertEqual(set(results.keys()), {"frontier_only", "local_only", "adaptive"})
        for metrics in results.values():
            self.assertEqual(metrics.total_requests, 2)


if __name__ == "__main__":
    unittest.main()
