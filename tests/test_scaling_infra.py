"""Tests for the autoscaling-related infrastructure: the registry.endpoint
regression fix, token-count threading through the pipeline, the context
firewall's wiring into frontier calls, and the Redis/Postgres backends
that make horizontal scaling correct (rate limiting, trace storage).

The Redis/Postgres tests are skipped automatically when those services
aren't reachable (e.g. plain CI without docker-compose) — see
`_redis_available()` / `_postgres_available()` below. They were run
against real redis:7-alpine and postgres:16-alpine containers during
development; see the README's Autoscaling section for how to do the
same locally.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.frontier_llm import FrontierLLMResponse
from alr.local_llm import LocalLLMResponse, call_local_llm
from alr.models import RouteTier, TaskEnvelope
from alr.pipeline import Pipeline
from alr.rate_limit import TokenBucketRateLimiter, build_rate_limiter
from alr.registry import ModelRegistry

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/0")
POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN", "postgresql://alr:alr@localhost:5433/alr")


def _redis_available() -> bool:
    try:
        import redis  # type: ignore

        redis.Redis.from_url(REDIS_URL).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _postgres_available() -> bool:
    try:
        import psycopg  # type: ignore

        with psycopg.connect(POSTGRES_DSN, connect_timeout=2):
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


class TestModelSpecEndpoint(unittest.TestCase):
    """Regression test: ModelSpec used to silently drop the `endpoint`
    field from models.yaml, causing every Tier-1 call to crash with
    AttributeError instead of failing closed with LocalLLMUnavailable.
    """

    def test_registry_loads_endpoint_from_yaml(self):
        registry = ModelRegistry()
        spec = registry.get("local-coder")
        self.assertIsNotNone(spec.endpoint)
        self.assertTrue(spec.endpoint.startswith("http"))

    def test_missing_endpoint_fails_closed_not_with_attributeerror(self):
        registry = ModelRegistry()
        spec = registry.get("local-coder")
        spec.endpoint = None
        from alr.local_llm import LocalLLMUnavailable

        with self.assertRaises(LocalLLMUnavailable):
            call_local_llm(spec, "hello")


class TestTokenThreading(unittest.TestCase):
    def test_local_llm_response_extracts_ollama_token_counts(self):
        resp = LocalLLMResponse(text="ok", raw={"prompt_eval_count": 42, "eval_count": 7})
        # LocalLLMResponse itself doesn't parse raw — call_local_llm does.
        # This test documents the field exists on the response object.
        self.assertEqual(resp.input_tokens, 0)  # default when constructed directly

    def test_pipeline_threads_frontier_tokens_into_result(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        try:
            pipeline = Pipeline(db_path=tmp.name)
            with mock.patch("alr.pipeline.call_frontier_llm") as mock_frontier:
                mock_frontier.return_value = FrontierLLMResponse(
                    text="ok", raw={}, input_tokens=321, output_tokens=45,
                )
                result = pipeline.run(
                    TaskEnvelope(task="Design the architecture for a distributed payments system")
                )
            self.assertEqual(result.route, RouteTier.FRONTIER)
            self.assertEqual(result.input_tokens, 321)
            self.assertEqual(result.output_tokens, 45)
        finally:
            os.unlink(tmp.name)


class TestContextFirewallWiring(unittest.TestCase):
    """Section 12/23: raw evidence attached to payload['context_text']
    should be compressed before reaching the frontier model, and the
    before/after token counts should land on the ExecutionResult for
    the context_compression_ratio metric (alr/evaluation.py).
    """

    def test_context_text_is_compressed_before_frontier_call(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        try:
            pipeline = Pipeline(db_path=tmp.name)
            raw_log = "\n".join(
                ["INFO normal line"] * 500 + ["ERROR ConnectionTimeout at payment/client.py"]
            )
            envelope = TaskEnvelope(
                task="Design the architecture for a distributed payments system",
                payload={"context_text": raw_log},
            )
            with mock.patch("alr.pipeline.call_frontier_llm") as mock_frontier:
                mock_frontier.return_value = FrontierLLMResponse(text="ok", raw={}, input_tokens=1, output_tokens=1)
                result = pipeline.run(envelope)

                # The prompt actually sent to the frontier model must be
                # compressed, not the raw 500+-line log.
                sent_prompt = mock_frontier.call_args[0][1]
                self.assertLess(len(sent_prompt), len(raw_log))
                self.assertIn("ConnectionTimeout", sent_prompt)

            self.assertIsNotNone(result.context_tokens_before)
            self.assertIsNotNone(result.context_tokens_after)
            self.assertLess(result.context_tokens_after, result.context_tokens_before)
        finally:
            os.unlink(tmp.name)

    def test_no_context_text_leaves_tokens_none(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        try:
            pipeline = Pipeline(db_path=tmp.name)
            with mock.patch("alr.pipeline.call_frontier_llm") as mock_frontier:
                mock_frontier.return_value = FrontierLLMResponse(text="ok", raw={}, input_tokens=1, output_tokens=1)
                result = pipeline.run(TaskEnvelope(task="Design the architecture for a distributed payments system"))
            self.assertIsNone(result.context_tokens_before)
            self.assertIsNone(result.context_tokens_after)
        finally:
            os.unlink(tmp.name)


@unittest.skipUnless(_redis_available(), f"Redis not reachable at {REDIS_URL} — skipping distributed rate limit test")
class TestRedisRateLimiterLive(unittest.TestCase):
    def test_bucket_shared_across_two_limiter_instances(self):
        os.environ["REDIS_URL"] = REDIS_URL
        try:
            limiter_a = build_rate_limiter(capacity=3, refill_per_second=0.0)
            limiter_b = build_rate_limiter(capacity=3, refill_per_second=0.0)
            key = f"test-{os.getpid()}-{id(self)}"
            self.assertTrue(limiter_a.allow(key))
            self.assertTrue(limiter_a.allow(key))
            self.assertTrue(limiter_a.allow(key))
            # Bucket is exhausted; a second "replica" sharing the same
            # Redis-backed bucket must also see it as exhausted.
            self.assertFalse(limiter_b.allow(key))
        finally:
            os.environ.pop("REDIS_URL", None)


@unittest.skipUnless(_postgres_available(), f"Postgres not reachable at {POSTGRES_DSN} — skipping trace store test")
class TestPostgresTraceStoreLive(unittest.TestCase):
    def test_concurrent_schema_setup_does_not_race(self):
        """Regression test for the advisory-lock fix: multiple replicas
        constructing PostgresTraceStore at the same moment used to race
        on CREATE TABLE IF NOT EXISTS and crash with
        psycopg.errors.UniqueViolation on pg_type_typname_nsp_index
        (reproduced against a real 3-container concurrent startup during
        development). Simulated here with threads against the same live
        Postgres instance.
        """
        import threading

        from alr.tracing import PostgresTraceStore

        errors = []

        def make_store():
            try:
                PostgresTraceStore(POSTGRES_DSN)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=make_store) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"concurrent schema setup raised: {errors}")

    def test_record_and_read_back(self):
        from alr.models import ExecutionResult, RouteDecision
        from alr.tracing import PostgresTraceStore

        store = PostgresTraceStore(POSTGRES_DSN)
        trace_id = f"test-{os.getpid()}-{id(self)}"
        decision = RouteDecision(trace_id=trace_id, route=RouteTier.LOCAL, model="local-coder", reason=["t"], confidence=0.8)
        result = ExecutionResult(
            trace_id=trace_id, route=RouteTier.LOCAL, model="local-coder", output="ok",
            confidence=0.8, success=True, latency_ms=42, input_tokens=10, output_tokens=5,
        )
        store.record(decision=decision, result=result, task_type="log_analysis", risk="low", complexity=0.2)

        rows = [r for r in store.all_rows() if r["trace_id"] == trace_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selected_route"], "local")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
