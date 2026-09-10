"""Integration test for the full pipeline (analyze -> route -> execute ->
validate -> escalate -> trace), with the LLM connectors monkeypatched so
this runs with no network access and no API keys. Run:
    python3 -m unittest tests.test_pipeline -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.local_llm import LocalLLMResponse
from alr.frontier_llm import FrontierLLMResponse
from alr.models import RouteTier, TaskEnvelope
from alr.pipeline import Pipeline


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.pipeline = Pipeline(db_path=self.tmp_db.name)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_tool_task_end_to_end(self):
        result = self.pipeline.run(TaskEnvelope(task="git status"))
        self.assertEqual(result.route, RouteTier.TOOL)
        self.assertTrue(result.success)

    @mock.patch("alr.pipeline.call_local_llm")
    def test_local_task_high_confidence_stays_local(self, mock_local):
        mock_local.return_value = LocalLLMResponse(
            text="37 errors found, primary cause: ConnectionTimeout. CONFIDENCE: 0.95",
            raw={},
            self_reported_confidence=0.95,
        )
        result = self.pipeline.run(TaskEnvelope(task="Summarize these deployment logs"))
        self.assertEqual(result.route, RouteTier.LOCAL)
        self.assertFalse(result.escalated)
        mock_local.assert_called_once()

    @mock.patch("alr.pipeline.call_frontier_llm")
    @mock.patch("alr.pipeline.call_local_llm")
    def test_low_confidence_local_result_escalates_to_frontier(self, mock_local, mock_frontier):
        mock_local.return_value = LocalLLMResponse(
            text="I'm not sure, it might be a timeout but it's hard to say. CONFIDENCE: 0.3",
            raw={},
            self_reported_confidence=0.3,
        )
        mock_frontier.return_value = FrontierLLMResponse(
            text="Root cause confirmed: connection pool exhaustion under load.",
            raw={}, input_tokens=500, output_tokens=50,
        )
        result = self.pipeline.run(TaskEnvelope(task="Summarize these deployment logs"))
        self.assertEqual(result.route, RouteTier.FRONTIER)
        self.assertTrue(result.escalated)
        mock_local.assert_called_once()
        mock_frontier.assert_called_once()

    @mock.patch("alr.pipeline.call_frontier_llm")
    def test_architecture_task_goes_straight_to_frontier_no_local_call(self, mock_frontier):
        mock_frontier.return_value = FrontierLLMResponse(
            text="Here is the proposed architecture...", raw={}, input_tokens=800, output_tokens=300,
        )
        result = self.pipeline.run(
            TaskEnvelope(task="Design the architecture for a distributed payments system")
        )
        self.assertEqual(result.route, RouteTier.FRONTIER)
        self.assertFalse(result.escalated)
        mock_frontier.assert_called_once()

    def test_traces_are_recorded_and_summarizable(self):
        self.pipeline.run(TaskEnvelope(task="git status"))
        metrics = self.pipeline.trace_store.summary_metrics()
        self.assertEqual(metrics["total_requests"], 1)
        self.assertEqual(metrics["tool_requests"], 1)


if __name__ == "__main__":
    unittest.main()
