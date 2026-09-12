"""Tests for the dual frontier transport (CLI vs. raw Anthropic API) added
to alr/frontier_llm.py. No real network access or `claude` CLI needed —
urllib is monkeypatched.
"""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alr.frontier_llm import (
    FrontierAuthError,
    FrontierLLMError,
    TRANSPORT_ENV_VAR,
    _call_frontier_llm_cli,
    _resolve_transport,
    call_frontier_llm,
    validate_frontier_auth,
)
from alr.registry import ModelSpec


def _spec(transport=None):
    return ModelSpec(
        name="claude", provider="anthropic", model_name="claude-sonnet-5",
        capabilities=[], cost_per_1k_tokens=0.006, latency_ms_p50=2500,
        privacy="cloud", transport=transport,
    )


class _FakeRegistry:
    """Duck-types just enough of ModelRegistry for validate_frontier_auth,
    without needing a real models.yaml file.
    """
    def __init__(self, cloud_models):
        self._cloud_models = cloud_models

    def cloud_capable_models(self):
        return self._cloud_models


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestResolveTransport(unittest.TestCase):
    def setUp(self):
        os.environ.pop(TRANSPORT_ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(TRANSPORT_ENV_VAR, None)

    def test_defaults_to_cli(self):
        self.assertEqual(_resolve_transport(_spec()), "cli")

    def test_env_var_selects_api(self):
        os.environ[TRANSPORT_ENV_VAR] = "api"
        self.assertEqual(_resolve_transport(_spec()), "api")

    def test_model_spec_transport_overrides_env(self):
        os.environ[TRANSPORT_ENV_VAR] = "api"
        self.assertEqual(_resolve_transport(_spec(transport="cli")), "cli")


class TestApiTransport(unittest.TestCase):
    def setUp(self):
        os.environ.pop(TRANSPORT_ENV_VAR, None)
        os.environ[TRANSPORT_ENV_VAR] = "api"

    def tearDown(self):
        os.environ.pop(TRANSPORT_ENV_VAR, None)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_requires_api_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(FrontierLLMError):
            call_frontier_llm(_spec(), "hello")

    @mock.patch("alr.frontier_llm.urllib.request.urlopen")
    def test_successful_call_parses_text_and_usage(self, mock_urlopen):
        mock_urlopen.return_value = _FakeHTTPResponse({
            "content": [{"type": "text", "text": "Hello from the API"}],
            "usage": {"input_tokens": 42, "output_tokens": 7},
        })
        resp = call_frontier_llm(_spec(), "hello", api_key="sk-test")
        self.assertEqual(resp.text, "Hello from the API")
        self.assertEqual(resp.input_tokens, 42)
        self.assertEqual(resp.output_tokens, 7)
        self.assertIsNone(resp.cost_usd)  # API doesn't report cost; pipeline falls back to estimate_cost

    @mock.patch("alr.frontier_llm.urllib.request.urlopen")
    def test_http_error_raises_frontier_llm_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages", code=401, msg="unauthorized",
            hdrs=None, fp=mock.Mock(read=lambda: b'{"error": "invalid api key"}'),
        )
        with self.assertRaises(FrontierLLMError):
            call_frontier_llm(_spec(), "hello", api_key="bad-key")


class TestCliTransportErrorReporting(unittest.TestCase):
    """Regression test: the claude CLI reports auth/API errors (e.g. an
    invalid/expired CLAUDE_CODE_OAUTH_TOKEN) as an `is_error` JSON payload
    on stdout even when it exits non-zero — stderr is often empty. The
    original code only looked at stderr on a non-zero exit, so real
    failures surfaced as an unhelpful "claude CLI exited 1: " with no
    detail. Reproduced live: `CLAUDE_CODE_OAUTH_TOKEN=<invalid>` produces
    exactly the stdout payload asserted below.
    """

    @mock.patch("alr.frontier_llm.subprocess.run")
    def test_invalid_oauth_token_error_is_extracted_from_stdout(self, mock_run):
        stdout_payload = json.dumps({
            "is_error": True,
            "api_error_status": 401,
            "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid.",
        })
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout=stdout_payload, stderr="",
        )
        with self.assertRaises(FrontierLLMError) as ctx:
            _call_frontier_llm_cli(_spec(), "hello")
        self.assertIn("OAuth access token is invalid", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    @mock.patch("alr.frontier_llm.subprocess.run")
    def test_non_zero_exit_with_unparseable_stdout_falls_back_to_stderr(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="permission denied",
        )
        with self.assertRaises(FrontierLLMError) as ctx:
            _call_frontier_llm_cli(_spec(), "hello")
        self.assertIn("permission denied", str(ctx.exception))


class TestValidateFrontierAuth(unittest.TestCase):
    """Startup-time auth check — refuse to serve traffic with no usable
    frontier credential configured, instead of deferring to a confusing
    per-request failure later.
    """

    def setUp(self):
        for var in (TRANSPORT_ENV_VAR, "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ.pop(var, None)

    tearDown = setUp

    def test_no_cloud_models_needs_no_auth(self):
        validate_frontier_auth(_FakeRegistry([]))  # must not raise

    def test_api_transport_without_key_raises(self):
        os.environ[TRANSPORT_ENV_VAR] = "api"
        with self.assertRaises(FrontierAuthError):
            validate_frontier_auth(_FakeRegistry([_spec()]))

    def test_api_transport_with_key_passes(self):
        os.environ[TRANSPORT_ENV_VAR] = "api"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        validate_frontier_auth(_FakeRegistry([_spec()]))  # must not raise

    @mock.patch("alr.frontier_llm.shutil.which", return_value=None)
    def test_cli_transport_without_binary_raises(self, _mock_which):
        with self.assertRaises(FrontierAuthError):
            validate_frontier_auth(_FakeRegistry([_spec()]))

    @mock.patch("alr.frontier_llm.sys.stdin")
    @mock.patch("alr.frontier_llm.shutil.which", return_value="/usr/local/bin/claude")
    def test_cli_transport_headless_with_no_token_raises(self, _mock_which, mock_stdin):
        mock_stdin.isatty.return_value = False
        with self.assertRaises(FrontierAuthError):
            validate_frontier_auth(_FakeRegistry([_spec()]))

    @mock.patch("alr.frontier_llm.sys.stdin")
    @mock.patch("alr.frontier_llm.shutil.which", return_value="/usr/local/bin/claude")
    def test_cli_transport_headless_with_oauth_token_passes(self, _mock_which, mock_stdin):
        mock_stdin.isatty.return_value = False
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-test"
        validate_frontier_auth(_FakeRegistry([_spec()]))  # must not raise

    @mock.patch("alr.frontier_llm.sys.stdin")
    @mock.patch("alr.frontier_llm.shutil.which", return_value="/usr/local/bin/claude")
    def test_cli_transport_interactive_terminal_passes_without_token(self, _mock_which, mock_stdin):
        mock_stdin.isatty.return_value = True
        validate_frontier_auth(_FakeRegistry([_spec()]))  # must not raise


if __name__ == "__main__":
    unittest.main()
