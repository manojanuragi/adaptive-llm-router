"""Stdlib-only unit tests. Run with: python3 -m unittest discover -s tests -v

These cover the parts of the design doc that don't need a live Ollama
server or Anthropic API key: task analysis, rule-based routing, the
confidence/escalation logic, the context firewall, and Tier-0 tools.
Pipeline-level integration with mocked LLM calls is in test_pipeline.py.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.analyzer import TaskAnalyzer
from alr.context import ContextManager
from alr.models import Privacy, Risk, RouteTier, TaskEnvelope
from alr.registry import ModelRegistry
from alr.router import AdaptiveRouter
from alr.tools import run_tool
from alr.validator import Validator


class TestAnalyzer(unittest.TestCase):
    def setUp(self):
        self.analyzer = TaskAnalyzer()

    def test_git_status_matches_tool(self):
        env = TaskEnvelope(task="run git status on the repo")
        f = self.analyzer.analyze(env)
        self.assertEqual(f.matched_tool, "git_status")
        self.assertEqual(f.complexity, 0.0)

    def test_architecture_task_is_high_complexity(self):
        env = TaskEnvelope(task="Design a distributed system architecture for payments")
        f = self.analyzer.analyze(env)
        self.assertIsNone(f.matched_tool)
        self.assertGreaterEqual(f.complexity, 0.85)

    def test_summarization_is_low_complexity(self):
        env = TaskEnvelope(task="Summarize these deployment logs")
        f = self.analyzer.analyze(env)
        self.assertLess(f.complexity, 0.4)

    def test_high_risk_bumps_complexity(self):
        env = TaskEnvelope(task="Summarize these logs", risk=Risk.HIGH)
        f = self.analyzer.analyze(env)
        env2 = TaskEnvelope(task="Summarize these logs", risk=Risk.LOW)
        f2 = self.analyzer.analyze(env2)
        self.assertGreater(f.complexity, f2.complexity)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.registry = ModelRegistry()
        self.router = AdaptiveRouter(self.registry)
        self.analyzer = TaskAnalyzer()

    def test_tool_task_routes_to_tool_tier(self):
        env = TaskEnvelope(task="git status")
        f = self.analyzer.analyze(env)
        d = self.router.route(env, f)
        self.assertEqual(d.route, RouteTier.TOOL)
        self.assertEqual(d.model, "git_status")

    def test_architecture_task_routes_to_frontier(self):
        env = TaskEnvelope(task="Design the architecture for a distributed payments system")
        f = self.analyzer.analyze(env)
        d = self.router.route(env, f)
        self.assertEqual(d.route, RouteTier.FRONTIER)
        self.assertEqual(d.model, "claude")

    def test_summarization_routes_to_local(self):
        env = TaskEnvelope(task="Summarize these deployment logs")
        f = self.analyzer.analyze(env)
        d = self.router.route(env, f)
        self.assertEqual(d.route, RouteTier.LOCAL)
        self.assertEqual(d.model, "local-coder")

    def test_restricted_privacy_never_goes_cloud_even_if_complex(self):
        env = TaskEnvelope(
            task="Design the architecture for a distributed payments system",
            privacy=Privacy.RESTRICTED,
        )
        f = self.analyzer.analyze(env)
        d = self.router.route(env, f)
        self.assertEqual(d.route, RouteTier.LOCAL)
        self.assertIn("privacy=restricted", d.reason[0])

    def test_high_risk_forces_frontier_even_if_keywords_look_simple(self):
        env = TaskEnvelope(task="Summarize these logs", risk=Risk.HIGH)
        f = self.analyzer.analyze(env)
        d = self.router.route(env, f)
        self.assertEqual(d.route, RouteTier.FRONTIER)


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.validator = Validator(ModelRegistry())

    def test_confident_clean_output_not_escalated(self):
        v = self.validator.validate_local_result(
            text="The primary error is a connection timeout in payment/client.py.",
            model_name="local-coder",
            category="log_analysis",
            self_reported_confidence=0.93,
        )
        self.assertFalse(v.should_escalate)

    def test_hedging_output_escalates(self):
        v = self.validator.validate_local_result(
            text="I'm not sure, it might be a timeout but it's hard to say for certain.",
            model_name="local-coder",
            category="complex_reasoning",
            self_reported_confidence=0.4,
        )
        self.assertTrue(v.should_escalate)

    def test_empty_output_escalates(self):
        v = self.validator.validate_local_result(
            text="", model_name="local-coder", category="coding", self_reported_confidence=None
        )
        self.assertTrue(v.should_escalate)


class TestContextFirewall(unittest.TestCase):
    def test_compress_extracts_errors_and_hides_raw_by_default(self):
        raw = "\n".join([
            "INFO starting up",
            "ERROR ConnectionTimeout in payment/client.py",
            "INFO retry 1",
            "ERROR ConnectionTimeout in payment/client.py",
            "INFO retry 2",
        ] * 20)
        cm = ContextManager()
        evidence = cm.compress(raw, reference_id="req-8231")
        self.assertEqual(evidence.summary["error_line_count"], 40)
        self.assertLessEqual(len(evidence.summary["sample_errors"]), 10)
        self.assertNotIn("raw_text", evidence.summary)  # frontier never sees raw by default

    def test_request_more_evidence_progressive_disclosure(self):
        cm = ContextManager()
        evidence = cm.compress("normal line\nERROR: pool exhausted at request 8231\nnormal", "req-8231")
        slice_ = evidence.request_more_evidence("pool exhausted")
        self.assertIn("pool exhausted", slice_)


class TestTier0Tools(unittest.TestCase):
    def test_git_status_runs(self):
        out = run_tool("git_status", {})
        self.assertIsInstance(out, str)

    def test_json_parse_tool(self):
        out = run_tool("json_parse", {"raw": '{"a": 1}'})
        self.assertEqual(out, {"a": 1})

    def test_unknown_tool_raises(self):
        with self.assertRaises(KeyError):
            run_tool("does_not_exist", {})


if __name__ == "__main__":
    unittest.main()
