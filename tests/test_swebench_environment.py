"""Controlled SWE-bench factory and real subprocess-boundary contracts."""
from __future__ import annotations

import copy
import importlib.metadata
import subprocess
import unittest
from unittest.mock import patch

from delivery_agent.swebench_environment import ENVIRONMENT_CLASS, SAFE_ENV, SWEbenchDockerEnvironment
from delivery_agent.swebench_run import _attach_cleanup, _load_factories, _validated_config


IMAGE_ID = "sha256:" + "e" * 64
CONTAINER_ID = "f" * 64
IMAGE_NAME = "docker.io/swebench/sweb.eval.x86_64.org_1776_repo-1:latest"


class DockerDouble:
    def __init__(self):
        self.calls = []
        self.startup_error = None
        self.identity = CONTAINER_ID
        self.missing_image = False
        self.cleanup_failure = False

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[:3] == ["docker", "image", "inspect"]:
            if self.missing_image:
                self.missing_image = False
                return subprocess.CompletedProcess(argv, 1, b"", b"No such image")
            return subprocess.CompletedProcess(argv, 0, (IMAGE_ID + "\n").encode(), b"")
        if argv[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 0, b"pulled\n", b"")
        if argv[:2] == ["docker", "run"]:
            if self.startup_error:
                raise self.startup_error
            return subprocess.CompletedProcess(argv, 0, (self.identity + "\n").encode(), b"")
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, 1 if self.cleanup_failure else 0, b"", b"daemon unavailable")
        raise AssertionError(f"unexpected host command: {argv!r}")


class RunningProcess:
    def __init__(self, argv, *, stdout, stderr, size=0, completed=False):
        self.argv, self.returncode = list(argv), 0 if completed else None
        self.killed = False
        if size:
            stdout.seek(size - 1)
            stdout.write(b"x")
        else:
            stdout.write(b"public output\n")
        stdout.flush()

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self, timeout=None):
        return self.returncode


class SWEEnvironmentCase(unittest.TestCase):
    def setUp(self):
        self.docker = DockerDouble()
        patcher = patch("delivery_agent.swebench_environment.subprocess.run", side_effect=self.docker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def environment(self, **kwargs):
        env = SWEbenchDockerEnvironment(image=IMAGE_NAME, **kwargs)
        self.addCleanup(env.cleanup)
        return env


class SWEEnvironmentTests(SWEEnvironmentCase):
    def test_frozen_image_no_host_mounts_no_network_and_no_credential_forwarding(self):
        env = self.environment()
        argv = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
        self.assertIn(IMAGE_ID, argv)
        self.assertNotIn(IMAGE_NAME, argv)
        self.assertNotIn("--mount", argv)
        self.assertNotIn("-v", argv)
        self.assertNotIn("-e", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("-w") + 1], "/testbed")
        self.assertIn("no-new-privileges", argv)
        self.assertEqual(env.config.forward_env, [])
        self.assertEqual(env.serialize()["info"]["config"]["environment"]["image"], IMAGE_ID)
        self.assertTrue(all(not kwargs.get("shell", False) for _, kwargs in self.docker.calls))

    def test_missing_image_is_pulled_then_resolved_before_startup(self):
        self.docker.missing_image = True
        self.environment()
        operations = [argv[1] for argv, _ in self.docker.calls]
        self.assertEqual(operations, ["image", "pull", "image", "run"])
        self.assertEqual(self.docker.calls[1][0], ["docker", "pull", IMAGE_NAME])

    def test_startup_timeout_terminates_by_name_before_propagating_failure(self):
        self.docker.startup_error = subprocess.TimeoutExpired(["docker", "run"], 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.environment()
        startup = next(argv for argv, _ in self.docker.calls if argv[:2] == ["docker", "run"])
        name = startup[startup.index("--name") + 1]
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", name])

    def test_invalid_startup_identity_is_also_cleaned_by_name(self):
        self.docker.identity = "unexpected output"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.environment()
        self.assertEqual(self.docker.calls[-1][0][:3], ["docker", "rm", "-f"])
        self.assertTrue(self.docker.calls[-1][0][-1].startswith("delivery-swebench-"))

    def test_startup_cleanup_failure_is_explicit(self):
        self.docker.startup_error = subprocess.TimeoutExpired(["docker", "run"], 120)
        self.docker.cleanup_failure = True
        with self.assertRaisesRegex(RuntimeError, "termination could not be confirmed"):
            self.environment()

    def test_command_timeout_closes_container_and_refuses_a_followup(self):
        env = self.environment(timeout=1)
        processes = []

        def start(argv, **kwargs):
            process = RunningProcess(argv, **kwargs)
            processes.append(process)
            return process

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start), \
                patch("delivery_agent.runtime.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                env.execute({"command": "python slow.py"})
        self.assertTrue(processes[0].killed)
        self.assertTrue(env.closed)
        self.assertIsNone(env.container_id)
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", CONTAINER_ID])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            env.execute({"command": "echo retry"})

    def test_output_overflow_closes_container_before_more_work(self):
        env = self.environment()

        def start(argv, **kwargs):
            return RunningProcess(argv, **kwargs, size=8 * 1024 * 1024 + 1)

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "Output limit"):
                env.execute({"command": "yes"})
        self.assertTrue(env.closed)
        self.assertIsNone(env.container_id)

    def test_exec_wait_failure_still_terminates_the_task_container(self):
        env = self.environment(timeout=1)

        class DelayedExit(RunningProcess):
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(self.argv, timeout)

        def start(argv, **kwargs):
            return DelayedExit(argv, **kwargs)

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start), \
                patch("delivery_agent.runtime.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaises(subprocess.TimeoutExpired):
                env.execute({"command": "python slow.py"})
        self.assertTrue(env.closed)
        self.assertIsNone(env.container_id)
        self.assertEqual(self.docker.calls[-1][0], ["docker", "rm", "-f", CONTAINER_ID])

    def test_timeout_with_unconfirmed_cleanup_still_refuses_followup_execution(self):
        env = self.environment(timeout=1)
        self.docker.cleanup_failure = True

        def start(argv, **kwargs):
            return RunningProcess(argv, **kwargs)

        try:
            with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start), \
                    patch("delivery_agent.runtime.time.monotonic", side_effect=[0.0, 2.0]):
                with self.assertRaisesRegex(RuntimeError, "termination"):
                    env.execute({"command": "python slow.py"})
            self.assertFalse(env.closed)  # Termination is honestly unconfirmed.
            with patch("delivery_agent.runtime.subprocess.Popen") as process:
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    env.execute({"command": "echo retry"})
                process.assert_not_called()
        finally:
            self.docker.cleanup_failure = False

    def test_execute_uses_testbed_bash_and_literal_noncredential_environment(self):
        env = self.environment()
        processes = []

        def start(argv, **kwargs):
            process = RunningProcess(argv, **kwargs, completed=True)
            processes.append(process)
            return process

        with patch("delivery_agent.runtime.subprocess.Popen", side_effect=start), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "private-host-value"}, clear=False):
            result = env.execute({"command": "python -m pytest"})
        argv = processes[0].argv
        self.assertEqual(argv[:4], ["docker", "exec", "-w", "/testbed"])
        self.assertEqual(argv[-4:], [CONTAINER_ID, "bash", "-c", "python -m pytest"])
        for key, value in SAFE_ENV.items():
            self.assertIn(f"{key}={value}", argv)
        self.assertNotIn("OPENAI_API_KEY", " ".join(argv))
        self.assertNotIn("private-host-value", " ".join(argv))
        self.assertEqual(result["output"], "public output\n")

    def test_adapter_preserves_controlled_sync_cleanup_and_marks_identity_absent(self):
        env = self.environment()
        before = env.cleanup
        _attach_cleanup(env)
        self.assertEqual(env.cleanup, before)
        env.cleanup()
        self.assertTrue(env.closed)
        self.assertIsNone(env.container_id)

    def test_security_overrides_are_rejected_before_startup(self):
        for fields in ({"cwd": "/host"}, {"executable": "powershell"}, {"forward_env": ["OPENAI_API_KEY"]},
                       {"run_args": ["--network", "host"]}, {"env": {"OPENAI_API_KEY": "secret"}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.environment(**fields)
        self.assertEqual(self.docker.calls, [])


try:
    PINNED_UPSTREAM = importlib.metadata.version("mini-swe-agent") == "2.4.6"
except importlib.metadata.PackageNotFoundError:
    PINNED_UPSTREAM = False


@unittest.skipUnless(PINNED_UPSTREAM, "optional pinned mini-swe-agent is not installed")
class ActualFactoryTests(SWEEnvironmentCase):
    def test_actual_upstream_factory_derives_image_and_loads_controlled_class(self):
        from minisweagent.environments import get_environment
        config = _validated_config({"model": {"model_name": "provider/model"}})
        original = copy.deepcopy(config)
        factory, _ = _load_factories()
        instance = {"instance_id": "org__repo-1", "repo": "org/repo", "base_commit": "a" * 40,
                    "problem_statement": "Fix public code."}
        env = factory(config, instance)
        self.addCleanup(env.cleanup)
        self.assertIsInstance(env, SWEbenchDockerEnvironment)
        self.assertEqual(env.image, IMAGE_NAME)
        self.assertEqual(config, original)
        env.cleanup()
        self.assertIsNone(env.container_id)
        second = get_environment({"environment_class": ENVIRONMENT_CLASS, "image": IMAGE_NAME})
        self.addCleanup(second.cleanup)
        self.assertIsInstance(second, SWEbenchDockerEnvironment)


if __name__ == "__main__":
    unittest.main()
