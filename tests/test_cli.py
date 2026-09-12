"""Tests for alr_cli.py and bin/alr — previously these had zero
automated coverage, only manual runs. Drives them as real subprocesses
(not importing and calling functions directly) so argument parsing,
stdin handling, and exit codes are actually exercised end-to-end.

Uses `git push` phrasing to deterministically force a policy denial
(REQUIRE_APPROVAL with no approval_callback -> DENY -> success=False)
for the failure-path tests, rather than depending on whether Ollama or
network access happens to be available in the environment running this.
"""
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALR_CLI = os.path.join(REPO_ROOT, "alr_cli.py")
ALR_BIN = os.path.join(REPO_ROOT, "bin", "alr")
PYTHON = sys.executable


def _run(args, input_text=None, cwd=None, db_path=None):
    """Runs alr_cli.py as a real subprocess. Always pins --db to an
    explicit path (a temp file by default) so tests never write
    alr_traces.db into whatever cwd they run from, including this repo's
    own root when a test needs cwd=REPO_ROOT for `git status` to succeed
    against a real repo.
    """
    full_args = list(args)
    if "--db" not in full_args:
        full_args = full_args + ["--db", db_path or os.path.join(tempfile.gettempdir(), "alr_test_traces.db")]
    return subprocess.run(
        [PYTHON, ALR_CLI, *full_args],
        input=input_text, capture_output=True, text=True, timeout=30, cwd=cwd,
    )


class TestAlrCliArgTask(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "traces.db")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_tool_task_succeeds_with_exit_zero(self):
        proc = _run(["git status"], cwd=REPO_ROOT, db_path=self._db_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("route: tool", proc.stdout)
        self.assertIn("success: True", proc.stdout)

    def test_denied_task_exits_nonzero(self):
        proc = _run(
            ["please git push to main now"], cwd=self._tmpdir.name, db_path=self._db_path,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("success: False", proc.stdout)
        self.assertIn("Blocked by policy engine", proc.stdout)

    def test_no_task_and_empty_stdin_errors(self):
        proc = _run([], input_text="", cwd=self._tmpdir.name, db_path=self._db_path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("No task provided", proc.stderr)

    def test_context_file_is_read_and_attached(self):
        context_path = os.path.join(self._tmpdir.name, "evidence.log")
        with open(context_path, "w") as f:
            f.write("ERROR something broke at line 42\n")
        # A denied task still exercises the arg-parsing/file-reading path
        # without depending on network/local-model availability.
        proc = _run(
            ["please git push to main now", "--context-file", context_path],
            cwd=self._tmpdir.name, db_path=self._db_path,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("success: False", proc.stdout)

    def test_missing_context_file_errors_clearly(self):
        proc = _run(
            ["git status", "--context-file", "/nonexistent/path.log"],
            cwd=self._tmpdir.name, db_path=self._db_path,
        )
        self.assertNotEqual(proc.returncode, 0)


class TestAlrCliStdin(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_task_from_stdin(self):
        proc = _run(
            [], input_text="git status\n", cwd=REPO_ROOT,
            db_path=os.path.join(self._tmpdir.name, "traces.db"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("route: tool", proc.stdout)


class TestBinAlrWrapper(unittest.TestCase):
    """Regression coverage for the symlink-resolution bug found while
    building this: a naive `dirname "${BASH_SOURCE[0]}"` resolved to the
    symlink's own directory instead of the real repo location.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_bin_alr(self, path, cwd, db_path):
        return subprocess.run(
            [path, "git status", "--db", db_path],
            capture_output=True, text=True, timeout=30, cwd=cwd,
        )

    def test_direct_invocation_from_repo_root(self):
        proc = self._run_bin_alr(
            ALR_BIN, cwd=REPO_ROOT, db_path=os.path.join(self._tmpdir.name, "traces.db"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("route: tool", proc.stdout)

    def test_invocation_through_a_symlink_from_an_unrelated_directory(self):
        symlink_path = os.path.join(self._tmpdir.name, "alr")
        os.symlink(ALR_BIN, symlink_path)
        proc = self._run_bin_alr(
            symlink_path, cwd=self._tmpdir.name, db_path=os.path.join(self._tmpdir.name, "traces.db"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("route: tool", proc.stdout)


if __name__ == "__main__":
    unittest.main()
