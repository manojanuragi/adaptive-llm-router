"""Tests for Pipeline._compute_savings — the per-response token/cost
savings tracking (baseline_cost, cost_saved, cost_saved_pct, tokens_saved,
tokens_saved_pct) surfaced by /execute and rolled up in evaluation.py /
tracing.py.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.models import ExecutionResult, RouteTier
from alr.pipeline import Pipeline

FRONTIER_RATE = 0.006  # claude's cost_per_1k_tokens in alr/config/models.yaml


class TestComputeSavings(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.pipeline = Pipeline(db_path=self.tmp_db.name)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_local_result_saves_full_frontier_equivalent_cost(self):
        result = ExecutionResult(
            trace_id="t1", route=RouteTier.LOCAL, model="local-coder", output="ok",
            confidence=0.9, success=True, cost=0.0, input_tokens=1000, output_tokens=200,
        )
        self.pipeline._compute_savings(result)
        expected_baseline = round(1200 / 1000 * FRONTIER_RATE, 6)
        self.assertAlmostEqual(result.baseline_cost, expected_baseline)
        self.assertAlmostEqual(result.cost_saved, expected_baseline)
        self.assertEqual(result.cost_saved_pct, 100.0)
        self.assertEqual(result.tokens_saved, 1200)
        self.assertEqual(result.tokens_saved_pct, 100.0)

    def test_tool_result_with_no_tokens_has_zero_savings(self):
        result = ExecutionResult(
            trace_id="t2", route=RouteTier.TOOL, model="git", output="clean",
            confidence=1.0, success=True,
        )
        self.pipeline._compute_savings(result)
        self.assertEqual(result.baseline_cost, 0.0)
        self.assertEqual(result.cost_saved_pct, 0.0)
        self.assertEqual(result.tokens_saved_pct, 0.0)

    def test_escalated_local_result_is_not_treated_as_a_savings_win(self):
        # Escalated means the task ultimately ran on frontier — the final
        # result object represents the frontier execution, so savings
        # should be evaluated on the frontier branch, not the local-avoided
        # branch, even though route metadata originated from a local retry.
        result = ExecutionResult(
            trace_id="t3", route=RouteTier.FRONTIER, model="claude", output="ok",
            confidence=0.9, success=True, escalated=True, cost=0.01,
            input_tokens=500, output_tokens=50,
        )
        self.pipeline._compute_savings(result)
        self.assertEqual(result.baseline_cost, result.cost)
        self.assertEqual(result.cost_saved, 0.0)
        self.assertEqual(result.tokens_saved, 0)

    def test_frontier_result_with_context_compression_saves_the_compressed_tokens(self):
        result = ExecutionResult(
            trace_id="t4", route=RouteTier.FRONTIER, model="claude", output="ok",
            confidence=0.9, success=True, cost=0.01, input_tokens=300, output_tokens=50,
            context_tokens_before=1000, context_tokens_after=200,
        )
        self.pipeline._compute_savings(result)
        self.assertEqual(result.tokens_saved, 800)
        self.assertEqual(result.tokens_saved_pct, 80.0)
        self.assertEqual(result.cost_saved_pct, 80.0)
        expected_baseline = round((300 + 800) / 1000 * FRONTIER_RATE, 6)
        self.assertAlmostEqual(result.baseline_cost, expected_baseline)

    def test_frontier_result_without_context_compression_has_no_savings(self):
        result = ExecutionResult(
            trace_id="t5", route=RouteTier.FRONTIER, model="claude", output="ok",
            confidence=0.9, success=True, cost=0.01, input_tokens=300, output_tokens=50,
        )
        self.pipeline._compute_savings(result)
        self.assertEqual(result.baseline_cost, result.cost)
        self.assertEqual(result.cost_saved, 0.0)
        self.assertEqual(result.tokens_saved, 0)


if __name__ == "__main__":
    unittest.main()
