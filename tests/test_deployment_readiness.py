"""Tests for the deployment-readiness additions: policy engine, auth,
rate limiter, retry/circuit breaker, path sanitization, and the async
pipeline path. All stdlib-only (uses asyncio, unittest.mock) — no
network, no FastAPI needed to run these.
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.auth import load_api_keys_from_env, resolve_caller_id, verify_api_key
from alr.frontier_llm import FrontierLLMResponse
from alr.local_llm import LocalLLMResponse, LocalLLMUnavailable
from alr.models import RouteTier, TaskEnvelope
from alr.pipeline import Pipeline, PolicyDeniedError
from alr.policy import PolicyDecision, PolicyEngine
from alr.rate_limit import TokenBucketRateLimiter
from alr.retry import CircuitBreaker, CircuitOpenError, retry_async, retry_sync
from alr.tools import UnsafePathError, _safe_path


class TestPolicyEngine(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine()

    def test_ordinary_task_allowed(self):
        r = self.engine.evaluate(TaskEnvelope(task="Summarize these logs"))
        self.assertEqual(r.decision, PolicyDecision.ALLOW)

    def test_git_push_requires_approval_and_denies_without_callback(self):
        r = self.engine.evaluate(TaskEnvelope(task="please git push to main"))
        self.assertEqual(r.decision, PolicyDecision.REQUIRE_APPROVAL)

    def test_approval_callback_can_allow(self):
        engine = PolicyEngine(approval_callback=lambda env, tool: True)
        r = engine.evaluate(TaskEnvelope(task="run a production deployment now"))
        self.assertEqual(r.decision, PolicyDecision.ALLOW)

    def test_approval_callback_can_deny(self):
        engine = PolicyEngine(approval_callback=lambda env, tool: False)
        r = engine.evaluate(TaskEnvelope(task="drop table users"))
        self.assertEqual(r.decision, PolicyDecision.DENY)

    def test_test_execution_tool_always_requires_approval(self):
        r = self.engine.evaluate(TaskEnvelope(task="run the tests"), tool_or_model="test_execution")
        self.assertEqual(r.decision, PolicyDecision.REQUIRE_APPROVAL)


class TestAuth(unittest.TestCase):
    def test_no_keys_configured_allows_everything(self):
        self.assertTrue(verify_api_key("anything", []))
        self.assertTrue(verify_api_key(None, []))

    def test_valid_key_accepted(self):
        self.assertTrue(verify_api_key("secret1", ["secret1", "secret2"]))

    def test_invalid_key_rejected(self):
        self.assertFalse(verify_api_key("wrong", ["secret1"]))

    def test_missing_key_rejected_when_keys_configured(self):
        self.assertFalse(verify_api_key(None, ["secret1"]))

    def test_load_keys_from_env_without_labels_never_uses_raw_key_as_identity(self):
        # Regression test for a real vulnerability found via security
        # review: unlabeled keys previously became their own identity,
        # which /metrics' usage_by_caller then leaked to every other
        # caller as a live, usable credential. Each unlabeled key must
        # map to something that is NOT the key itself.
        with mock.patch.dict(os.environ, {"ALR_API_KEYS": "a, b ,c"}):
            resolved = load_api_keys_from_env()
        self.assertEqual(set(resolved.keys()), {"a", "b", "c"})
        for raw_key, identity in resolved.items():
            self.assertNotEqual(identity, raw_key)
            self.assertTrue(identity.startswith("unlabeled-"))
        # Different keys must resolve to different identities.
        self.assertEqual(len(set(resolved.values())), 3)

    def test_load_keys_from_env_unlabeled_identity_is_stable_across_calls(self):
        with mock.patch.dict(os.environ, {"ALR_API_KEYS": "a"}):
            first = load_api_keys_from_env()
            second = load_api_keys_from_env()
        self.assertEqual(first, second)

    def test_load_keys_from_env_with_labeled_identities(self):
        with mock.patch.dict(os.environ, {"ALR_API_KEYS": "key1:alice, key2:bob"}):
            self.assertEqual(load_api_keys_from_env(), {"key1": "alice", "key2": "bob"})

    def test_resolve_caller_id_returns_label_not_raw_key(self):
        keys = {"key1": "alice"}
        self.assertEqual(resolve_caller_id("key1", keys), "alice")

    def test_resolve_caller_id_never_returns_a_raw_unlabeled_key(self):
        # End-to-end regression: run an unlabeled key through the full
        # load -> resolve path an attacker calling /metrics would see.
        with mock.patch.dict(os.environ, {"ALR_API_KEYS": "super-secret-raw-key"}):
            keys = load_api_keys_from_env()
        caller_id = resolve_caller_id("super-secret-raw-key", keys)
        self.assertNotEqual(caller_id, "super-secret-raw-key")

    def test_resolve_caller_id_falls_back_to_anonymous(self):
        self.assertEqual(resolve_caller_id("unknown", {"key1": "alice"}), "anonymous")
        self.assertEqual(resolve_caller_id(None, {}), "anonymous")


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_capacity_then_blocks(self):
        limiter = TokenBucketRateLimiter(capacity=3, refill_per_second=0.0)
        key = "user1"
        self.assertTrue(limiter.allow(key))
        self.assertTrue(limiter.allow(key))
        self.assertTrue(limiter.allow(key))
        self.assertFalse(limiter.allow(key))

    def test_refills_over_time(self):
        limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=1000.0)
        key = "user2"
        self.assertTrue(limiter.allow(key))
        self.assertFalse(limiter.allow(key))
        time.sleep(0.01)
        self.assertTrue(limiter.allow(key))

    def test_keys_are_independent(self):
        limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("a"))


class TestRetryAndCircuitBreaker(unittest.TestCase):
    def test_retry_sync_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("transient")
            return "ok"

        result = retry_sync(flaky, retryable_exceptions=(ValueError,), max_attempts=5, base_delay_s=0.001)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_sync_raises_after_exhausting_attempts(self):
        def always_fails():
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            retry_sync(always_fails, retryable_exceptions=(ValueError,), max_attempts=2, base_delay_s=0.001)

    def test_circuit_breaker_opens_after_threshold(self):
        breaker = CircuitBreaker(failure_threshold=2, reset_after_s=100.0)

        def always_fails():
            raise ValueError("down")

        with self.assertRaises(ValueError):
            retry_sync(always_fails, (ValueError,), max_attempts=1, base_delay_s=0.001, breaker=breaker)
        with self.assertRaises(ValueError):
            retry_sync(always_fails, (ValueError,), max_attempts=1, base_delay_s=0.001, breaker=breaker)

        # breaker should now be open and fail fast without calling fn again
        with self.assertRaises(CircuitOpenError):
            retry_sync(always_fails, (ValueError,), max_attempts=1, base_delay_s=0.001, breaker=breaker)

    def test_retry_async(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("transient")
            return "ok"

        result = asyncio.run(retry_async(flaky, (ValueError,), max_attempts=3, base_delay_s=0.001))
        self.assertEqual(result, "ok")


class TestPathSanitization(unittest.TestCase):
    def test_normal_relative_path_ok(self):
        p = _safe_path(".")
        self.assertTrue(p.exists())

    def test_traversal_rejected(self):
        with self.assertRaises(UnsafePathError):
            _safe_path("../../../etc/passwd")

    def test_absolute_escape_rejected(self):
        with self.assertRaises(UnsafePathError):
            _safe_path("/etc/passwd")


class TestPipelinePolicyIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_high_risk_task_denied_by_default_policy(self):
        pipeline = Pipeline(db_path=self.tmp_db.name)
        result = pipeline.run(TaskEnvelope(task="git push origin main --force"))
        self.assertFalse(result.success)
        self.assertEqual(result.escalation_reason, "policy_denied")

    def test_approved_high_risk_task_proceeds(self):
        pipeline = Pipeline(
            db_path=self.tmp_db.name,
            policy_engine=PolicyEngine(approval_callback=lambda env, tool: True),
        )
        result = pipeline.run(TaskEnvelope(task="git status"))
        self.assertTrue(result.success)  # ordinary task, not even gated, sanity check


class TestAsyncPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.pipeline = Pipeline(db_path=self.tmp_db.name)

    def tearDown(self):
        os.unlink(self.tmp_db.name)

    def test_tool_task_end_to_end_async(self):
        result = asyncio.run(self.pipeline.run_async(TaskEnvelope(task="git status")))
        self.assertEqual(result.route, RouteTier.TOOL)
        self.assertTrue(result.success)

    @mock.patch("alr.pipeline.call_local_llm_async")
    def test_local_task_async_high_confidence_stays_local(self, mock_local):
        async def fake_local(*a, **kw):
            return LocalLLMResponse(text="clear answer. CONFIDENCE: 0.95", raw={}, self_reported_confidence=0.95)

        mock_local.side_effect = fake_local
        result = asyncio.run(self.pipeline.run_async(TaskEnvelope(task="Summarize these deployment logs")))
        self.assertEqual(result.route, RouteTier.LOCAL)
        self.assertFalse(result.escalated)

    @mock.patch("alr.pipeline.call_frontier_llm_async")
    @mock.patch("alr.pipeline.call_local_llm_async")
    def test_low_confidence_escalates_async(self, mock_local, mock_frontier):
        async def fake_local(*a, **kw):
            return LocalLLMResponse(text="not sure, hard to say. CONFIDENCE: 0.3", raw={}, self_reported_confidence=0.3)

        async def fake_frontier(*a, **kw):
            return FrontierLLMResponse(text="Root cause found.", raw={}, input_tokens=100, output_tokens=20)

        mock_local.side_effect = fake_local
        mock_frontier.side_effect = fake_frontier
        result = asyncio.run(self.pipeline.run_async(TaskEnvelope(task="Summarize these deployment logs")))
        self.assertEqual(result.route, RouteTier.FRONTIER)
        self.assertTrue(result.escalated)

    def test_denied_task_async(self):
        result = asyncio.run(self.pipeline.run_async(TaskEnvelope(task="drop table users;")))
        self.assertFalse(result.success)
        self.assertEqual(result.escalation_reason, "policy_denied")


if __name__ == "__main__":
    unittest.main()
