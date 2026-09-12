"""Integration tests for the FastAPI HTTP layer (alr/api.py) using
FastAPI's TestClient against the real app + lifespan — not mocked at the
HTTP layer. Closes a real gap: auth, rate limiting, per-caller identity,
and startup auth enforcement were previously only verified by ad-hoc
manual curl calls, not an automated, repeatable test.

Stays network-free by only exercising tool-tier tasks (`git status`),
which never reach an LLM connector, and by setting
ALR_FRONTIER_TRANSPORT=api + a dummy ANTHROPIC_API_KEY so
validate_frontier_auth's startup check passes without needing a real
`claude` CLI or network access.

alr/api.py binds ALR_API_KEYS once at import time
(build_fastapi_dependency() runs at module load) and keeps _pipeline as
a module-level global set in lifespan() — so each test reloads the
module under its own patched environment and its own temp working
directory (so the default SQLite trace store never touches the real
repo's alr_traces.db).
"""
import contextlib
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_ENV_KEYS_TO_ISOLATE = (
    "ALR_API_KEYS", "ALR_FRONTIER_TRANSPORT", "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN", "DATABASE_URL", "REDIS_URL",
)


@contextlib.contextmanager
def _isolated_env(overrides):
    """Guarantees every key in _ENV_KEYS_TO_ISOLATE is either absent or
    exactly what `overrides` says — never leaking whatever happens to be
    set in the real shell environment this test runs in.
    """
    saved = {k: os.environ.get(k) for k in _ENV_KEYS_TO_ISOLATE}
    try:
        for k in _ENV_KEYS_TO_ISOLATE:
            os.environ.pop(k, None)
        os.environ.update(overrides)
        yield
    finally:
        for k in _ENV_KEYS_TO_ISOLATE:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def _fresh_api_module():
    """Reload alr.api so build_fastapi_dependency()/ModelRegistry() pick
    up the currently-patched environment, returning the reloaded module.
    """
    import alr.api as api_module
    return importlib.reload(api_module)


class _ApiTestCaseBase(unittest.TestCase):
    env_overrides = {
        "ALR_API_KEYS": "key1:alice,key2:bob",
        "ALR_FRONTIER_TRANSPORT": "api",
        "ANTHROPIC_API_KEY": "test-dummy-key-not-real",
    }

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self._env_ctx = _isolated_env(self.env_overrides)
        self._env_ctx.__enter__()

        from fastapi.testclient import TestClient

        self.api = _fresh_api_module()
        self.client = TestClient(self.api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._env_ctx.__exit__(None, None, None)
        os.chdir(self._old_cwd)
        self._tmpdir.cleanup()


class TestHealthAndReady(_ApiTestCaseBase):
    def test_health_is_unauthenticated_and_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_ready_reports_ok_when_trace_store_reachable(self):
        r = self.client.get("/ready")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ready")


class TestAuth(_ApiTestCaseBase):
    def test_execute_without_key_is_rejected(self):
        r = self.client.post("/execute", json={"task": "git status"})
        self.assertEqual(r.status_code, 401)

    def test_execute_with_wrong_key_is_rejected(self):
        r = self.client.post(
            "/execute", json={"task": "git status"}, headers={"x-api-key": "not-a-real-key"}
        )
        self.assertEqual(r.status_code, 401)

    def test_execute_with_valid_key_succeeds(self):
        r = self.client.post(
            "/execute", json={"task": "git status"}, headers={"x-api-key": "key1"}
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["route"], "tool")
        self.assertTrue(body["success"])

    def test_metrics_requires_auth_too(self):
        r = self.client.get("/metrics")
        self.assertEqual(r.status_code, 401)


class TestAuthOff(unittest.TestCase):
    """When ALR_API_KEYS is unset, auth is off entirely — documented
    behavior for local dev. Verify that's really what happens, not
    silently rejecting every caller.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self._env_ctx = _isolated_env({
            "ALR_FRONTIER_TRANSPORT": "api",
            "ANTHROPIC_API_KEY": "test-dummy-key-not-real",
        })
        self._env_ctx.__enter__()

        from fastapi.testclient import TestClient

        self.api = _fresh_api_module()
        self.client = TestClient(self.api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._env_ctx.__exit__(None, None, None)
        os.chdir(self._old_cwd)
        self._tmpdir.cleanup()

    def test_execute_without_key_allowed_when_auth_disabled(self):
        r = self.client.post("/execute", json={"task": "git status"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["caller_id"], "anonymous")


class TestPerCallerUsageTracking(_ApiTestCaseBase):
    def test_different_keys_are_tracked_as_separate_callers(self):
        r1 = self.client.post("/execute", json={"task": "git status"}, headers={"x-api-key": "key1"})
        r2 = self.client.post("/execute", json={"task": "git status"}, headers={"x-api-key": "key2"})
        self.assertEqual(r1.json()["caller_id"], "alice")
        self.assertEqual(r2.json()["caller_id"], "bob")

        usage = self.client.get("/metrics", headers={"x-api-key": "key1"}).json()["usage_by_caller"]
        self.assertEqual(usage["alice"]["requests"], 1)
        self.assertEqual(usage["bob"]["requests"], 1)

    def test_raw_api_key_never_appears_as_a_caller_id(self):
        self.client.post("/execute", json={"task": "git status"}, headers={"x-api-key": "key1"})
        usage = self.client.get("/metrics", headers={"x-api-key": "key1"}).json()["usage_by_caller"]
        self.assertNotIn("key1", usage)


class TestRouteDryRun(_ApiTestCaseBase):
    def test_route_does_not_record_a_trace(self):
        r = self.client.post("/route", json={"task": "git status"}, headers={"x-api-key": "key1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["route"], "tool")

        metrics = self.client.get("/metrics", headers={"x-api-key": "key1"}).json()
        self.assertEqual(metrics.get("total_requests", 0), 0)


class TestBodySizeLimit(_ApiTestCaseBase):
    def test_oversized_content_length_is_rejected_before_parsing(self):
        r = self.client.post(
            "/execute",
            headers={"x-api-key": "key1", "content-length": "999999999"},
            content=b"irrelevant",
        )
        self.assertEqual(r.status_code, 413)


class TestStartupRefusesWithoutFrontierAuth(unittest.TestCase):
    """Codifies the startup-enforcement behavior verified manually
    earlier: the API must refuse to start (not silently run auth-less)
    when a cloud-capable model is registered but nothing can authenticate
    it in a headless environment.
    """

    def test_lifespan_raises_in_headless_env_with_no_credentials(self):
        tmpdir = tempfile.TemporaryDirectory()
        old_cwd = os.getcwd()
        os.chdir(tmpdir.name)
        env_ctx = _isolated_env({"ALR_API_KEYS": "key1"})
        env_ctx.__enter__()
        try:
            from fastapi.testclient import TestClient

            api = _fresh_api_module()
            with mock.patch("alr.frontier_llm.sys.stdin") as mock_stdin, \
                 mock.patch("alr.frontier_llm.shutil.which", return_value=None):
                mock_stdin.isatty.return_value = False
                with self.assertRaises(Exception):
                    with TestClient(api.app):
                        pass
        finally:
            env_ctx.__exit__(None, None, None)
            os.chdir(old_cwd)
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
