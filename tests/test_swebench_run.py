import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from delivery_agent.swebench_run import run_swebench_instance


class EnvironmentDouble:
    """Pinned DockerEnvironment execute/config/container protocol, without Docker."""
    def __init__(self, heads=None):
        self.container_id = "c" * 64
        self.config = SimpleNamespace(cwd="/testbed", image="sweb.eval.example:latest", executable="docker")
        self.commands, self.heads = [], list(heads or ["a" * 40, "a" * 40])
        self.cleanup = Mock(side_effect=AssertionError("upstream shell cleanup must be replaced"))

    def execute(self, action, cwd="", **kwargs):
        self.commands.append((action["command"], cwd))
        command = action["command"]
        if command == "git rev-parse HEAD":
            output = self.heads.pop(0)
        elif command == "git status --porcelain" or command == "git add -N .":
            output = ""
        elif command.startswith("git diff --binary"):
            output = "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n"
        elif command.startswith("git diff --name-only"):
            output = "code.py\x00"
        else:
            raise AssertionError(f"unexpected inspection: {command}")
        return {"returncode": 0, "output": output, "exception_info": ""}


class SWEbenchRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.instance = {"instance_id": "org__repo-1", "repo": "org/repo", "base_commit": "a" * 40,
                         "problem_statement": "Repair the parser."}
        self.config = {"model": {"model_name": "provider/model", "model_kwargs": {"temperature": 0, "max_tokens": 1024}},
                       "agent": {"system_template": "Fix code in /testbed; never change HEAD.", "step_limit": 20},
                       "environment": {"timeout": 30}}

    def invoke(self, env=None, *, outcome=None, model_error=None, cleanup_code=0):
        env = env or EnvironmentDouble()
        environment_factory = Mock(return_value=env)
        model_factory = Mock(return_value=SimpleNamespace(config=SimpleNamespace(model_name="provider/model")))
        if model_error:
            model_factory.side_effect = model_error
        docker_calls = []

        def docker(argv, **kwargs):
            docker_calls.append((argv, kwargs))
            if argv[1] == "inspect":
                return SimpleNamespace(returncode=0, stdout=("sha256:" + "d" * 64 + "\n").encode())
            self.assertEqual(argv[:3], ["docker", "rm", "-f"])
            return SimpleNamespace(returncode=cleanup_code, stdout=b"")

        with patch("delivery_agent.swebench_run._load_factories", return_value=(environment_factory, model_factory)), \
                patch("delivery_agent.swebench_run.run_agent", return_value=outcome or {"exit_status": "Submitted", "submission": "untrusted agent text"}) as agent, \
                patch("delivery_agent.swebench_run.subprocess.run", side_effect=docker):
            result = run_swebench_instance(self.instance, self.config, self.root / "run")
        return result, env, environment_factory, model_factory, agent, docker_calls

    def test_success_uses_public_task_exact_base_real_patch_and_sync_cleanup(self):
        result, env, factory, model, agent, docker_calls = self.invoke()
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["model_name_or_path"], "provider/model")
        self.assertIsNone(result["patch_valid"])
        self.assertIsNone(result["official_resolved"])
        self.assertFalse(result["fixture"])
        self.assertEqual(result["provider"], "litellm")
        self.assertIn("diff --git", result["model_patch"])
        self.assertNotIn("untrusted agent text", result["model_patch"])
        self.assertEqual(result["changed_files"], ["code.py"])
        self.assertTrue(result["cleanup_confirmed"])
        self.assertIsNone(env.container_id)
        self.assertEqual(docker_calls[-1][0], ["docker", "rm", "-f", "c" * 64])
        self.assertFalse(docker_calls[-1][1].get("shell", False))
        used_config, used_task = factory.call_args.args
        self.assertEqual(used_task, self.instance)
        self.assertEqual(used_config["environment"]["cwd"], "/testbed")
        self.assertEqual(used_config["run"], {})
        self.assertEqual(used_config["environment"]["forward_env"], [])
        self.assertEqual(agent.call_args.kwargs["task"], self.instance["problem_statement"])
        self.assertTrue(all(cwd == "/testbed" for _, cwd in env.commands))
        self.assertIn(("git add -N .", "/testbed"), env.commands)
        saved = json.loads((self.root / "run/result.json").read_text())
        self.assertEqual(saved, result)
        spec = json.loads((self.root / "run/spec.json").read_text())
        self.assertEqual(spec["resolved_commit"], self.instance["base_commit"])
        self.assertEqual(spec["resolved_image"], "sha256:" + "d" * 64)
        self.assertNotIn("system_template", spec["config"]["agent_limits"])

    def test_gold_answers_and_credentials_are_rejected_before_factories(self):
        for key in ("patch", "test_patch", "hints_text", "FAIL_TO_PASS", "PASS_TO_PASS"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                run_swebench_instance({**self.instance, key: "hidden answer"}, self.config, self.root / key)
        for model in ({"model_name": "provider/model", "model_kwargs": {"api_key": "secret"}},
                      {"model_name": "provider/model", "model_kwargs": {"headers": {"Authorization": "secret"}}},
                      {"model_name": "provider/model", "model_kwargs": {"api_base": "https://user:secret@example.com"}},
                      {"model_name": "provider/model", "cost_tracking": "ignore"},
                      {"model_name": "provider/model", "model_class": "deterministic"}):
            with self.subTest(model=model), self.assertRaises(ValueError):
                run_swebench_instance(self.instance, {"model": model}, self.root / "bad")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_provider_failure_saves_type_only_and_still_removes_container(self):
        result, env, factory, model, agent, docker_calls = self.invoke(model_error=RuntimeError("Bearer sensitive-key"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["model_patch"], "")
        self.assertTrue(result["cleanup_confirmed"])
        agent.assert_not_called()
        for name in ("result.json", "spec.json", "outcome.json"):
            self.assertNotIn("sensitive-key", (self.root / "run" / name).read_text())

    def test_changed_or_wrong_head_never_exports_submission(self):
        for index, heads in enumerate((["b" * 40], ["a" * 40, "b" * 40])):
            with self.subTest(heads=heads):
                self.root = Path(self.temp.name) / str(index)
                result, env, _, _, agent, calls = self.invoke(EnvironmentDouble(heads))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["model_patch"], "")
                self.assertTrue(result["cleanup_confirmed"])
                self.assertFalse(any(command.startswith("git diff") for command, _ in env.commands))
                if len(heads) == 1:
                    agent.assert_not_called()

    def test_stopped_run_and_cleanup_failure_are_not_submitted(self):
        result, *_ = self.invoke(outcome={"exit_status": "LimitsExceeded"})
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["exit_status"], "LimitsExceeded")
        self.root = Path(self.temp.name) / "cleanup"
        result, *_ = self.invoke(cleanup_code=1)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup_confirmed"])

    def test_missing_pinned_dependency_produces_reviewable_failed_artifact(self):
        with patch("delivery_agent.swebench_run._load_factories", side_effect=RuntimeError("missing pinned package")):
            result = run_swebench_instance(self.instance, self.config, self.root / "run")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertTrue((self.root / "run/task.json").is_file())
        self.assertTrue((self.root / "run/result.json").is_file())


if __name__ == "__main__":
    unittest.main()
