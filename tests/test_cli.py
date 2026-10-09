"""Repository-run artifact contracts using trusted in-process adapters."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from delivery_agent import cli
from delivery_agent.repository import RepoTask


IMAGE_ID = "sha256:" + "b" * 64


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = RepoTask.parse({
            "instance_id": "owner__repo-1", "repo": "owner/repo", "base_commit": "a" * 40,
            "problem_statement": "Fix the incorrect module value.", "image": "python:mutable-tag",
            "public_checks": ["python -m pytest tests/test_module.py"],
        })
        self.args = SimpleNamespace(model="test/model", profile="retrieval", max_steps=3,
            wall_seconds=60, context_tokens=6000, max_output_tokens=1000, request_timeout=10,
            command_timeout=5, seed=42, cost_limit=1.0, api_base=None)
        self.sessions = []
        self.patch_text = "diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"

    def checkout(self, task, destination, source=None):
        destination = Path(destination)
        destination.mkdir(parents=True)
        (destination / ".git").mkdir()
        (destination / "module.py").write_text("value = 1\n", encoding="utf-8")
        return destination

    def capture_patch(self, work, base_commit):
        self.assertEqual(base_commit, self.task.base_commit)
        self.assertEqual((work / "module.py").read_text(encoding="utf-8"), "value = 2\n")
        blob = self.patch_text.encode()
        return {"model_patch": self.patch_text, "changed_files": ["module.py"],
                "patch_sha256": hashlib.sha256(blob).hexdigest(), "patch_bytes": len(blob)}

    def apply_fresh(self, task, model_patch, destination, source=None):
        self.assertEqual(model_patch, self.patch_text)
        self.assertEqual(source, self.root / "run" / "workspace")
        work = self.checkout(task, destination)
        (work / "module.py").write_text("value = 2\n", encoding="utf-8")
        return work

    def session_class(self):
        sessions = self.sessions

        class SessionDouble:
            def __init__(self, workspace, image, timeout):
                self.workspace, self.image, self.timeout = Path(workspace), image, timeout
                self.image_id = IMAGE_ID
                self.commands = []
                self.closed = False
                sessions.append(self)

            def execute(self, action):
                if self.closed:
                    raise AssertionError("test adapter used after close")
                self.commands.append(action)
                return {"output": "1 passed\n", "returncode": 0, "exception_info": ""}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

        return SessionDouble

    def run_with(self, agent, *, validator=None):
        # Supply only the model factory module imported by run_repository. These
        # doubles make no provider request and are not performance evidence.
        package = types.ModuleType("minisweagent")
        package.__path__ = []
        models = types.ModuleType("minisweagent.models")
        models.get_model = Mock(return_value=object())
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"minisweagent": package, "minisweagent.models": models}))
            stack.enter_context(patch.dict("os.environ", {}, clear=False))
            stack.enter_context(patch("delivery_agent.upstream.require_upstream", return_value=object))
            stack.enter_context(patch("delivery_agent.upstream.run_agent", side_effect=agent))
            stack.enter_context(patch.object(cli, "checkout", side_effect=self.checkout))
            stack.enter_context(patch.object(cli, "capture_patch", side_effect=self.capture_patch))
            stack.enter_context(patch.object(cli, "apply_fresh", side_effect=validator or self.apply_fresh))
            stack.enter_context(patch.object(cli, "DockerSession", self.session_class()))
            # Public checks must pass through the isolated session, never a host
            # subprocess. A regression to host execution fails immediately.
            stack.enter_context(patch.object(cli.subprocess, "run", side_effect=AssertionError("host execution forbidden")))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return cli.run_repository(self.task, self.args, self.root / "run")

    def submitted(self, model, environment, **kwargs):
        (environment.workspace / "module.py").write_text("value = 2\n", encoding="utf-8")
        return {"exit_status": "Submitted", "delivery": {"usage": {"complete": True}}}

    def test_failed_model_run_preserves_partial_patch_without_claiming_submission(self):
        def failing(model, environment, **kwargs):
            (environment.workspace / "module.py").write_text("value = 2\n", encoding="utf-8")
            raise RuntimeError("sensitive provider diagnostic must not be saved here")

        result = self.run_with(failing)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_status"])
        self.assertIsNone(result["patch_valid"])
        self.assertIsNone(result["official_resolved"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["model_patch"], self.patch_text)
        self.assertEqual((self.root / "run" / "model.patch").read_text(), self.patch_text)
        saved = (self.root / "run" / "result.json").read_text(encoding="utf-8")
        self.assertNotIn("sensitive provider diagnostic", saved)
        self.assertEqual(json.loads(saved)["status"], "failed")
        self.assertEqual(len(self.sessions), 1)
        self.assertTrue(self.sessions[0].closed)

    def test_public_verification_uses_fresh_checkout_and_resolved_image_only(self):
        result = self.run_with(self.submitted)
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["patch_valid"])
        self.assertIsNone(result["official_resolved"])
        self.assertEqual(len(self.sessions), 2)
        agent, verifier = self.sessions
        self.assertEqual(agent.workspace, self.root / "run" / "workspace")
        self.assertEqual(verifier.workspace, self.root / "run" / "validation")
        self.assertEqual(verifier.image, IMAGE_ID)
        self.assertEqual(verifier.commands, [{"command": self.task.public_checks[0]}])
        self.assertTrue(agent.closed and verifier.closed)
        self.assertEqual(result["public_checks"][0]["returncode"], 0)
        self.assertTrue(result["public_checks"][0]["passed"])

    def test_invalid_patch_skips_public_checks_and_retains_submission_status(self):
        def invalid(*args, **kwargs):
            raise RuntimeError("cannot apply submitted patch")

        result = self.run_with(self.submitted, validator=invalid)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["patch_valid"])
        self.assertEqual(result["public_checks"], [])
        self.assertEqual(len(self.sessions), 1)
        self.assertIsNone(result["official_resolved"])

    def test_completed_directory_is_not_overwritten(self):
        output = self.root / "run"
        output.mkdir()
        marker = output / "result.json"
        marker.write_text("existing trial evidence", encoding="utf-8")
        with patch("delivery_agent.upstream.require_upstream", return_value=object), \
                patch.dict("sys.modules", {"minisweagent.models": types.SimpleNamespace(get_model=Mock())}):
            with self.assertRaisesRegex(ValueError, "fresh output"):
                cli.run_repository(self.task, self.args, output)
        self.assertEqual(marker.read_text(), "existing trial evidence")

    def test_invalid_budgets_rejected_before_task_read_or_model_execution(self):
        common = ["run", "--task", "absent-task.json", "--output", "unused-output", "--model", "test/model"]
        for options in [["--max-steps", "0"], ["--command-timeout", "0"],
                        ["--context-tokens", "3000", "--max-output-tokens", "2048"],
                        ["--cost-limit", "-1"], ["--cost-limit", "nan"], ["--cost-limit", "inf"]]:
            with self.subTest(options=options), patch.object(cli, "run_repository") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(common + options)
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_provider_endpoint_cannot_embed_credentials_before_task_read(self):
        common = ["run", "--task", "absent-task.json", "--output", "unused-output", "--model", "test/model"]
        for endpoint in ("https://user:secret@example.invalid", "https://example.invalid?api_key=secret",
                         "file:///private", "https://example.invalid#secret"):
            with self.subTest(endpoint=endpoint), patch.object(cli, "run_repository") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(common + ["--api-base", endpoint])
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
