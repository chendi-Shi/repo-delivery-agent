"""Container lifecycle contract checks; no Docker daemon or generated host code."""
from __future__ import annotations

import importlib.metadata
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.runtime import DockerSession


IMAGE_ID = "sha256:" + "a" * 64


class DockerDouble:
    """Model the Docker CLI result boundaries, without executing a shell."""

    def __init__(self):
        self.calls = []
        self.startup_error = None
        self.removal_returncode = 0

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(argv, 0, b"28.0.0\n", b"")
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, (IMAGE_ID + "\n").encode(), b"")
        if argv[:2] == ["docker", "run"]:
            if self.startup_error is not None:
                raise self.startup_error
            return subprocess.CompletedProcess(argv, 0, b"container-123\n", b"")
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, self.removal_returncode, b"", b"cleanup failed")
        raise AssertionError(f"Unexpected host command: {argv!r}")


class ProcessDouble:
    def __init__(self, argv, *, stdout, stderr, payload=b"", running=False, sparse_size=0):
        self.argv = list(argv)
        self.returncode = None if running else 0
        self.killed = False
        self.waited = False
        if sparse_size:
            stdout.seek(sparse_size - 1)
            stdout.write(b"x")
        else:
            stdout.write(payload)
        stdout.flush()

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "workspace"
        self.work.mkdir()
        (self.work / ".git").mkdir()
        self.docker = DockerDouble()
        self.run_patch = patch("delivery_agent.runtime.subprocess.run", side_effect=self.docker)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def session(self, **kwargs):
        session = DockerSession(self.work, "python:mutable-tag", **kwargs)
        self.addCleanup(session.close)
        return session

    def execute(self, session, *, payload=b"result\n", running=False, sparse_size=0,
                returncode=None, command="python -m pytest"):
        processes = []

        def start(argv, **kwargs):
            process = ProcessDouble(argv, **kwargs, payload=payload, running=running, sparse_size=sparse_size)
            if returncode is not None:
                process.returncode = returncode
            processes.append(process)
            return process

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start):
            result = session.execute({"command": command})
        return result, processes[0]


class RuntimeTests(RuntimeCase):
    def test_linux_bind_mount_uses_host_identity_with_dropped_capabilities(self):
        with patch("delivery_agent.runtime.os.getuid", return_value=1001, create=True), \
                patch("delivery_agent.runtime.os.getgid", return_value=1002, create=True):
            self.session()
        argv = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
        self.assertEqual(argv[argv.index("--user") + 1], "1001:1002")
        self.assertIn("HOME=/tmp", argv)

    def test_container_is_isolated_and_uses_resolved_image_with_readonly_metadata(self):
        session = self.session()
        argv = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--read-only", argv)
        self.assertIn("ALL", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertIn(f"type=bind,source={self.work.resolve()},target=/workspace", argv)
        self.assertIn(f"type=bind,source={(self.work / '.git').resolve()},target=/workspace/.git,readonly", argv)
        self.assertIn(IMAGE_ID, argv)
        self.assertNotIn("python:mutable-tag", argv)
        self.assertEqual(session.get_template_vars()["image"], IMAGE_ID)
        self.assertTrue(all(not kwargs.get("shell", False) for _, kwargs in self.docker.calls))

    def test_persistent_container_has_no_idle_deadline_and_context_exit_removes_it(self):
        with self.session(timeout=3600) as session:
            argv = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
            self.assertEqual(argv[-3:], [IMAGE_ID, "sleep", "infinity"])
            self.assertFalse(any(argv[:2] == ["docker", "rm"] for argv, _ in self.docker.calls))
            self.assertFalse(session.closed)
        self.assertTrue(session.closed)
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", "container-123"])
        session.close()
        self.assertEqual(sum(argv[:2] == ["docker", "rm"] for argv, _ in self.docker.calls), 1)

    def test_workspace_without_git_metadata_never_starts_a_container(self):
        (self.work / ".git").rmdir()
        with self.assertRaisesRegex(ValueError, "Git metadata"):
            DockerSession(self.work, "python:mutable-tag")
        self.assertFalse(any(argv[:2] == ["docker", "run"] for argv, _ in self.docker.calls))

    def test_startup_timeout_removes_the_named_container_even_without_an_id(self):
        self.docker.startup_error = subprocess.TimeoutExpired(["docker", "run"], 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            DockerSession(self.work, "python:mutable-tag")
        startup = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
        expected_name = startup[startup.index("--name") + 1]
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", expected_name])

    def test_startup_cleanup_failure_is_reported_as_unconfirmed_termination(self):
        self.docker.startup_error = subprocess.TimeoutExpired(["docker", "run"], 30)
        self.docker.removal_returncode = 9
        with self.assertRaises(RuntimeError):
            DockerSession(self.work, "python:mutable-tag")

    def test_commands_only_reach_docker_exec_and_preserve_complete_evidence(self):
        session = self.session()
        payload = b"begin\n" + b"details\n" * 10000 + b"end diagnostic\n"
        result, process = self.execute(session, payload=payload,
                                       command="printf 'data; $(literal)' && python -m pytest")
        self.assertEqual(process.argv[:2], ["docker", "exec"])
        self.assertEqual(process.argv[-3:-1], ["sh", "-c"])
        self.assertEqual(process.argv[-1], "printf 'data; $(literal)' && python -m pytest")
        self.assertEqual(result["output"], payload.decode())
        self.assertEqual(result["returncode"], 0)

    def test_command_timeout_kills_exec_and_removes_the_entire_container(self):
        session = self.session(timeout=1)
        processes = []

        def start(argv, **kwargs):
            process = ProcessDouble(argv, **kwargs, payload=b"partial", running=True)
            processes.append(process)
            return process

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start), \
                patch("delivery_agent.runtime.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                session.execute({"command": "python slow.py"})
        self.assertTrue(processes[0].killed)
        self.assertTrue(processes[0].waited)
        self.assertTrue(session.closed)
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", "container-123"])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            session.execute({"command": "echo unsafe retry"})

    def test_running_output_overflow_kills_exec_and_closes_session(self):
        session = self.session()
        processes = []

        def start(argv, **kwargs):
            process = ProcessDouble(argv, **kwargs, running=True, sparse_size=8 * 1024 * 1024 + 1)
            processes.append(process)
            return process

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "Output limit"):
                session.execute({"command": "yes large-output"})
        self.assertTrue(processes[0].killed)
        self.assertTrue(session.closed)

    def test_completed_output_overflow_is_rejected_and_session_closed(self):
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, "Output limit"):
            self.execute(session, sparse_size=8 * 1024 * 1024 + 1)
        self.assertTrue(session.closed)

    def test_cleanup_failure_propagates_and_does_not_claim_container_closed(self):
        session = self.session()
        self.docker.removal_returncode = 9
        try:
            with self.assertRaisesRegex(RuntimeError, "termination"):
                session.close()
            self.assertFalse(session.closed)
        finally:
            self.docker.removal_returncode = 0

    def test_submission_marker_in_middle_or_nonzero_command_is_normal_output(self):
        session = self.session()
        for payload, code in [(b"warning\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", 0),
                              (b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT_EXTRA\n", 0),
                              (b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", 1)]:
            with self.subTest(payload=payload, code=code):
                result, _ = self.execute(session, payload=payload, returncode=code)
                self.assertEqual(result["output"], payload.decode())
                self.assertEqual(result["returncode"], code)

    def test_invalid_commands_are_rejected_before_any_exec(self):
        session = self.session()
        with patch("delivery_agent.runtime.subprocess.Popen") as process:
            for command in (None, "", "  ", "x" * 100001):
                with self.subTest(command_type=type(command).__name__), self.assertRaises(ValueError):
                    session.execute({"command": command})
            process.assert_not_called()


try:
    PINNED_UPSTREAM = importlib.metadata.version("mini-swe-agent") == "2.4.6"
except importlib.metadata.PackageNotFoundError:
    PINNED_UPSTREAM = False


@unittest.skipUnless(PINNED_UPSTREAM, "optional pinned mini-swe-agent is not installed")
class SubmissionTests(RuntimeCase):
    # Share setup helpers but do not duplicate the base contract tests.
    def test_exact_submission_marker_raises_actual_upstream_submitted(self):
        from minisweagent.exceptions import Submitted
        session = self.session()
        with self.assertRaises(Submitted) as raised:
            self.execute(session, payload=b"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nsubmitted details\n")
        message = raised.exception.messages[0]
        self.assertEqual(message["role"], "exit")
        self.assertEqual(message["extra"]["exit_status"], "Submitted")
        self.assertEqual(message["extra"]["submission"], "submitted details\n")


if __name__ == "__main__":
    unittest.main()
