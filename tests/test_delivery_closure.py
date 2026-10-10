"""Delivery runner contracts; fixtures here are never model-effect evidence."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from delivery_agent.repository import RepoTask, atomic_json
from scripts import ci_delivery_closure as runner
from scripts.grade_experiments import load_plan


def public_task(image="python:3.8-slim"):
    return RepoTask("closure-unit-contract", "example/repository", "a" * 40, "Visible public requirement.", image,
                   ("python -c 'print(\"old coverage\")'", "python -c 'raise TypeError(\"new contract absent\")'"))


class DeliveryClosureTests(unittest.TestCase):
    def cohort_contract(self):
        args = argparse.Namespace(model="qwen3:4b-instruct", mode="transfer", seed=42, max_output_tokens=1500,
            request_timeout=300, context_tokens=24000, ollama_mode="json", max_steps=24,
            cost_limit=3., wall_seconds=1800, command_timeout=90, profile="retrieval", label="contract")
        image = {"public_base_reference": "python:3.12-slim", "public_base_digest": "python@sha256:" + "b" * 64,
                 "public_base_image_id": "sha256:" + "c" * 64, "inference_image_id": "sha256:" + "d" * 64,
                 "operator_dockerfile_sha256": "e" * 64, "operator_image_tag": "contract-only:tools"}
        tasks = runner.suite_tasks()
        with patch.object(runner, "git", return_value=SimpleNamespace(stdout=b"1" * 40)):
            plan = runner.freeze_cohort(args, tasks, image, {"digest": "a" * 64})
        return args, plan, tasks, image

    def test_cohort_plan_and_blocked_seed_rows_are_accepted_by_real_summary_consumer(self):
        from scripts.summarize_closure_suite import _plan, summarize
        args, plan, tasks, image = self.cohort_contract()
        arms, targets, expected, reference, source = _plan(plan)
        self.assertEqual(len(expected), 10)
        self.assertEqual(sum(row["role"] == "holdout" for row in expected.values()), 4)
        self.assertEqual(len(source), 1)
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary)
            atomic_json(public / "suite-plan.json", plan)
            target = next(row for row in plan["targets"] if row["role"] == "near_transfer")
            arm = arms["verified-source"]
            receipt = runner.blocked_trial(plan, target, arm, 42, public, "blocked-target", "verified_source_unavailable")
            atomic_json(public / "observations.json", {"schema": 1, "kind": "closure_cohort_observations",
                "plan_sha256": runner.file_sha256(public / "suite-plan.json"), "source_attempts": [],
                "runs": [{"instance_id": target["instance_id"], "seed": 42, "arm": arm["id"],
                          "receipt": runner.artifact_ref(public / "blocked-target" / "trial-receipt.json", public)}]})
            report = summarize(public / "suite-plan.json", public / "observations.json")
            self.assertEqual(report["expected_target_attempts"], 10)
            self.assertFalse(report["complete"])
            self.assertIsNone(report["by_arm"]["verified-source"]["delivery_success_rate"])
            self.assertEqual(receipt["attempted_count"], 0)

    def test_cohort_pipeline_setup_failure_is_not_an_actual_model_attempt(self):
        from scripts.summarize_closure_suite import _receipt
        args, plan, tasks, image = self.cohort_contract()
        task, raw = tasks["more-itertools-last-factory.json"]
        arm = next(row for row in plan["arms"] if row["id"] == "baseline")
        target = next(row for row in plan["targets"] if row["instance_id"] == task.instance_id)
        daemon = SimpleNamespace(identity={"digest": "a" * 64}, assert_identity=lambda: None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, public = root / "private", root / "public"
            private.mkdir(); public.mkdir()
            with patch.object(runner, "run_baseline_checks", return_value={"resolved_image_id": image["public_base_image_id"],
                                                                        "feature_gap_confirmed": True}), \
                    patch.object(runner, "preflight_context", return_value={"initial_fits": True, "synthetic_ascii_tool_view_fits": True}), \
                    patch.object(runner, "run_repository", return_value={"status": "failed", "seconds": .1,
                        "error_type": "DockerUnavailable", "provenance": {"model_digest": "a" * 64}}):
                receipt, _, _ = runner.execute_cohort_trial(args, plan, task, raw, image, daemon,
                    private, public, "target", arm=arm, role="near_transfer")
            actual = _receipt(receipt, (task.instance_id, 42, arm["id"]), target, arm, plan, public)
            self.assertTrue(receipt["infrastructure_failed"])
            self.assertIsNone(receipt["delivery_success"])
            self.assertFalse(receipt["attempt_started"])
            self.assertEqual(receipt["model_calls"], 0)
            self.assertEqual(actual["attempted_count"], 0)

    def test_cohort_submitted_and_failed_checks_bind_through_real_summary_consumer(self):
        from scripts.summarize_closure_suite import _receipt
        args, plan, tasks, image = self.cohort_contract()
        task, raw = tasks["more-itertools-last-factory.json"]
        arm = next(row for row in plan["arms"] if row["id"] == "retrieval")
        target = next(row for row in plan["targets"] if row["instance_id"] == task.instance_id)
        daemon = SimpleNamespace(identity={"digest": "a" * 64}, assert_identity=lambda: None)
        for passes in (True, False):
            with self.subTest(passes=passes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                private, public = root / "private", root / "public"
                private.mkdir(); public.mkdir()
                checks = [{"command": command, "returncode": 0 if passes else 1, "passed": passes}
                          for command in task.public_checks]
                def model_boundary(given_task, given_args, directory):
                    (directory / "agent").mkdir(parents=True)
                    (directory / "agent" / "request-views.jsonl").write_text('{"event":"request","call":1}\n')
                    return {"fixture": False, "provider": "ollama_native", "status": "submitted", "exit_status": "Submitted",
                        "model_patch": "diff --git a/new.py b/new.py\n", "patch_valid": True, "seconds": 1.,
                        "public_checks": checks, "verification_status": "passed" if passes else "failed",
                        "delivery": {"model_calls": 1, "usage": {"input_tokens": 100, "output_tokens": 20,
                            "complete": True, "calls_with_usage": 1, "unknown_requests": 0}},
                        "provenance": {"config": {"model": given_args.model}, "model_digest": "a" * 64,
                            "task_hash": given_task.fingerprint(), "implementation_sha256": plan["common"]["implementation_sha256"]}}
                def independent_boundary(manifest, output, **kwargs):
                    loaded = load_plan(manifest)
                    self.assertEqual(loaded[0]["task"].image, image["public_base_digest"])
                    result = {"infrastructure_failures": [], "runs": [{"instance_id": task.instance_id,
                        "model_digest": "a" * 64, "resolved_image": image["public_base_image_id"],
                        "submitted": True, "public_checks": checks, "delivery_success": passes,
                        "verification_status": "passed" if passes else "failed"}]}
                    atomic_json(output, result)
                    return result
                with patch.object(runner, "run_baseline_checks", return_value={"resolved_image_id": image["public_base_image_id"],
                                                                            "feature_gap_confirmed": True}), \
                        patch.object(runner, "preflight_context", return_value={"initial_fits": True, "synthetic_ascii_tool_view_fits": True}), \
                        patch.object(runner, "run_repository", side_effect=model_boundary), \
                        patch.object(runner, "grade", side_effect=independent_boundary):
                    receipt, _, _ = runner.execute_cohort_trial(args, plan, task, raw, image, daemon,
                        private, public, "target", arm=arm, role="near_transfer")
                actual = _receipt(receipt, (task.instance_id, 42, arm["id"]), target, arm, plan, public)
                self.assertFalse(receipt["infrastructure_failed"])
                self.assertIs(actual["delivery_success"], passes)
                self.assertTrue(actual["attempt_started"])
                self.assertEqual(actual["actual_input_tokens"], 100)
                self.assertEqual(actual["actual_output_tokens"], 20)

    def test_source_guard_collects_added_root_and_test_python_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "more_itertools").mkdir(); (root / "tests").mkdir(); (root / ".git").mkdir()
            for name in ("more_itertools/more.py", "tests/test_added.py", "conftest.py", ".git/ignored.py"):
                (root / name).write_text("# bounded contract fixture\n")
            self.assertEqual(set(runner.collect_python_sources(root)),
                             {"more_itertools/more.py", "tests/test_added.py", "conftest.py"})

    def test_source_guard_normalizes_workspace_aliases_before_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "more_itertools").mkdir()
            source = "# public source under a canonical repository root\n"
            (root / "more_itertools" / "more.py").write_text(source, encoding="utf-8")
            aliases = [root, root / "more_itertools" / ".."]
            if os.name == "nt":
                import ctypes
                get_short_path = ctypes.windll.kernel32.GetShortPathNameW
                get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
                get_short_path.restype = ctypes.c_uint32
                size = get_short_path(str(root), None, 0)
                self.assertGreater(size, 0)
                buffer = ctypes.create_unicode_buffer(size)
                written = get_short_path(str(root), buffer, size)
                self.assertGreater(written, 0)
                self.assertLess(written, size)
                # Windows runners may expose TEMP through RUNNER~1 while
                # resolve()/walk() return the expanded profile directory.
                aliases.append(Path(buffer.value))
            for alias in aliases:
                with self.subTest(alias=str(alias)):
                    self.assertEqual(runner.collect_python_sources(alias), {"more_itertools/more.py": source})

    def test_source_reuse_requires_completed_read_with_every_bound_identity(self):
        origin = {"source_id": "k" + "a" * 32, "content_sha256": "b" * 64, "task_id": "source-A",
                  "namespace_sha256": "c" * 64, "verified": True, "source_ref": "run:" + "d" * 64,
                  "verification_sha256": "e" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit.jsonl"
            row = {"event": "action", "outcome": "completed", "returncode": 0,
                   "action_kind": "delivery-knowledge/search", "knowledge_operation": "search",
                   "knowledge_namespace_sha256": origin["namespace_sha256"], "knowledge_records": [origin]}
            audit.write_text(json.dumps(row) + "\n")
            self.assertFalse(runner.observed_source_reuse(audit, origin))
            row.update(action_kind="delivery-knowledge/read", knowledge_operation="read")
            audit.write_text(json.dumps(row) + "\n")
            self.assertTrue(runner.observed_source_reuse(audit, origin))
            row["knowledge_records"] = [{**origin, "verification_sha256": "f" * 64}]
            audit.write_text(json.dumps(row) + "\n")
            self.assertFalse(runner.observed_source_reuse(audit, origin))

    def test_model_usage_and_request_counts_are_actual_and_missing_usage_is_unknown(self):
        usage, calls = runner.normalized_usage({"delivery": {"model_calls": 2, "usage": {
            "input_tokens": 100, "output_tokens": 30, "calls_with_usage": 2, "unknown_requests": 0, "complete": True}}})
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], calls), (100, 30, 2))
        self.assertTrue(usage["complete"])
        self.assertEqual(runner.normalized_usage({})[0]["input_tokens"], None)
        with tempfile.TemporaryDirectory() as temporary:
            trial = Path(temporary)
            self.assertEqual(runner.actual_request_count(trial), 0)
            (trial / "agent").mkdir()
            (trial / "agent" / "request-views.jsonl").write_text(
                '{"event":"request","call":1}\n{"event":"error","call":1}\n')
            self.assertEqual(runner.actual_request_count(trial), 1)

    def test_every_cohort_task_template_fits_with_bounded_inventory_and_large_latest_observation(self):
        from delivery_agent.knowledge import KnowledgeStore
        try:
            importlib.metadata.version("mini-swe-agent")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("actual pinned wheel helper exercises template rendering")
        args, _, tasks, _ = self.cohort_contract()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = KnowledgeStore(root / "empty.sqlite", "closure-contract")
            try:
                with patch("delivery_agent.file_environment.FileEnvironment.inventory", return_value="x" * 1200), \
                        patch("delivery_agent.ollama_model.OllamaModel._http", side_effect=AssertionError("no provider call")):
                    for task, _ in tasks.values():
                        for profile in ("baseline", "recent", "retrieval"):
                            args.profile = profile
                            with self.subTest(instance=task.instance_id, profile=profile):
                                receipt = runner.preflight_context(task, args, root, knowledge=store)
                                self.assertTrue(receipt["initial_fits"])
                                self.assertTrue(receipt["synthetic_ascii_tool_view_fits"])
                                self.assertIsNone(receipt["provider_usage"])
            finally:
                store.close()

    def test_frozen_suite_only_rejects_mutated_task_and_answer_bearing_image(self):
        suite = runner.ROOT / "examples" / "delivery-suite-v1"
        task_path = suite / "requests-2931-public.json"
        actual, raw = runner.load_suite_task(task_path)
        self.assertEqual(actual.image, "python:3.8-slim")
        frozen = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(runner.sha256(raw), next(row["sha256"] for row in frozen["tasks"] if row["path"] == task_path.name))
        with self.assertRaises(ValueError):
            runner.load_suite_task(runner.ROOT / "examples" / "official-smoke" / "psf__requests-2931.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied = root / "examples" / "delivery-suite-v1"
            copied.mkdir(parents=True)
            manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
            atomic_json(copied / "manifest.json", manifest)
            (copied / task_path.name).write_bytes(raw + b"\n")
            with patch.object(runner, "ROOT", root), self.assertRaises(ValueError):
                runner.load_suite_task(copied / task_path.name)
            unsafe = replace(actual, image="swebench/sweb.eval.x86_64.psf_1776_requests-2931:latest")
            atomic_json(copied / task_path.name, asdict(unsafe))
            row = next(row for row in manifest["tasks"] if row["path"] == task_path.name)
            row.update(sha256=runner.sha256((copied / task_path.name).read_bytes()), fingerprint=unsafe.fingerprint())
            atomic_json(copied / "manifest.json", manifest)
            with patch.object(runner, "ROOT", root), self.assertRaises(ValueError):
                runner.load_suite_task(copied / task_path.name)

    def test_official_export_uses_real_result_and_keeps_failed_attempt_without_private_paths(self):
        original = RepoTask.parse(json.loads((runner.ROOT / "examples" / "official-smoke" / "psf__requests-2931.json").read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, public = root / "private", root / "public"
            private.mkdir()
            public.mkdir()
            public_patch = public / "model.patch"
            public_patch.write_bytes(b"")
            atomic_json(private / "result.json", {"instance_id": original.instance_id, "fixture": False,
                "provider": "ollama_native", "model_name_or_path": "ollama_chat/qwen2.5-coder:7b",
                "status": "stopped", "exit_status": "LimitsExceeded", "model_patch": "",
                "provenance": {"task": asdict(original), "model_digest": "a" * 64}})
            exported = runner.export_official_prediction(original, private, public, "qwen2.5-coder:7b",
                model_digest="a" * 64, public_patch=public_patch)
            self.assertEqual((exported["attempted_count"], exported["submitted_count"]), (1, 0))
            self.assertEqual((public / "official-predictions.jsonl").read_bytes(), b"")
            sidecar = json.loads((public / "official-predictions.jsonl.attempts.json").read_text(encoding="utf-8"))
            self.assertFalse(sidecar["attempts"][0]["submitted"])
            self.assertEqual(sidecar["predictions_sha256"], runner.sha256(b""))
            self.assertNotIn(str(private), json.dumps(exported))
            rejected = runner.export_official_prediction(replace(original, problem_statement="different issue"), private, public,
                "qwen2.5-coder:7b", model_digest="a" * 64, public_patch=public_patch)
            self.assertEqual(rejected["status"], "not_original_official_issue")
            public_patch.write_bytes(b"unrelated changed patch")
            with self.assertRaises(ValueError):
                runner.export_official_prediction(original, private, public, "qwen2.5-coder:7b",
                    model_digest="a" * 64, public_patch=public_patch)

    def test_image_preparation_keeps_python_base_and_every_non_image_task_field(self):
        for image in ("python:3.8-slim", "python:3.12-slim"):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as temporary:
                task, private = public_task(image), Path(temporary)
                portable = "python@sha256:" + "b" * 64
                calls = []

                def docker(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["docker", "image", "inspect"]:
                        info = {"Id": "sha256:" + ("c" if argv[-1] == image else "d") * 64,
                                "RepoDigests": [portable] if argv[-1] == image else []}
                        return SimpleNamespace(returncode=0, stdout=json.dumps(info).encode())
                    return SimpleNamespace(returncode=0, stdout=b"")

                with patch.object(runner.subprocess, "run", side_effect=docker):
                    effective, exported, metadata = runner.prepare_task_image(task, private)
                self.assertEqual(effective.image, "sha256:" + "d" * 64)
                self.assertEqual(exported.image, portable)
                self.assertEqual(replace(effective, image=task.image), task)
                self.assertEqual(replace(exported, image=task.image), task)
                self.assertEqual(metadata["public_base_reference"], image)
                text = (private / "operator-image-context" / "Dockerfile").read_text()
                self.assertTrue(text.startswith("FROM " + portable + "\n"))
                self.assertIn("git ripgrep", text)
                self.assertNotIn(task.problem_statement, text)
                self.assertEqual(runner.sha256(text), metadata["operator_dockerfile_sha256"])
                self.assertTrue(any(call[:2] == ["docker", "build"] for call in calls))

    def test_public_action_audit_bounds_and_redacts_previews_preserving_source_binding_and_incompleteness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, public = root / "private.jsonl", root / "public.jsonl"
            source = {"source_id": "k" + "a" * 32, "content_sha256": "b" * 64, "task_id": "source-A",
                      "namespace_sha256": "c" * 64, "verified": True, "source_ref": "run:" + "d" * 64,
                      "verification_sha256": "e" * 64, "text": "private-output-secret"}
            row = {"schema": 1, "event": "action", "call": 1, "ordinal": 1, "tool_call_id": "ollama_example_0",
                   "assistant_source_id": "e000003", "action_kind": "knowledge_read", "command_sha256": "f" * 64,
                   "command_chars": 700, "command_preview": "python key = 'ordinary public source'; " + "x" * 700, "outcome": "completed",
                   "returncode": 0, "output_sha256": "a" * 64, "output_chars": 1234,
                   "output_preview": "TypeError: 公开需求尚未实现", "elapsed_seconds": 0.1,
                   "knowledge_operation": "read", "knowledge_source_ids": [source["source_id"]],
                   "knowledge_namespace_sha256": "c" * 64, "knowledge_records": [source]}
            private.write_text(json.dumps(row) + "\n" + json.dumps({"event": "audit_limit", "complete": False}) + "\n")
            receipt = runner.export_action_audit(private, public)
            self.assertFalse(receipt["complete"])
            self.assertEqual(receipt["actions"], 1)
            text = public.read_text(encoding="utf-8")
            self.assertNotIn("private-output-secret", text)
            actual = json.loads(text)
            self.assertEqual(len(actual["command_preview"]), 500)
            self.assertEqual(actual["output_preview"], row["output_preview"])
            self.assertFalse(actual["preview_redacted"])
            self.assertEqual(actual["assistant_source_id"], "e000003")
            self.assertEqual(actual["knowledge_records"][0]["verification_sha256"], "e" * 64)
            row["command_preview"] = "Authorization: Bearer " + "a" * 30
            row["output_preview"] = str(runner.ROOT / "runs" / "private")
            private.write_text(json.dumps(row) + "\n")
            runner.export_action_audit(private, public)
            actual = json.loads(public.read_text(encoding="utf-8"))
            self.assertEqual(actual["command_preview"], "[REDACTED]")
            self.assertEqual(actual["output_preview"], "[REDACTED]")
            self.assertTrue(actual["preview_redacted"])
            self.assertEqual(actual["command_sha256"], "f" * 64)
            row["knowledge_records"][0]["source_ref"] = "C:/company/secret"
            private.write_text(json.dumps(row) + "\n")
            with self.assertRaises(ValueError):
                runner.export_action_audit(private, public)

    def test_actual_action_audit_tool_taxonomy_is_accepted_by_public_export(self):
        from delivery_agent.upstream import ActionAuditEnvironment
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = SimpleNamespace(execute=lambda *args, **kwargs: {"returncode": 0, "output": "public skill result"})
            audit = ActionAuditEnvironment(environment, root / "private.jsonl", lambda: 1)
            audit.execute({"command": "delivery-skill read python-regression", "tool_call_id": ""})
            receipt = runner.export_action_audit(root / "private.jsonl", root / "public.jsonl")
            record = json.loads((root / "public.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["action_kind"], "delivery-skill/read")
            self.assertEqual(receipt["actions"], 1)

    def test_genuine_default_agent_template_preflight_issues_no_provider_or_tool_call(self):
        try:
            version = importlib.metadata.version("mini-swe-agent")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("optional real pinned upstream not installed; actual-wheel helper exercises this contract")
        self.assertEqual(version, "2.4.6")
        args = argparse.Namespace(model="qwen2.5-coder:7b", seed=42, max_output_tokens=1500,
            request_timeout=300, context_tokens=16000, ollama_mode="json", max_steps=24,
            cost_limit=3., wall_seconds=1800, profile="recent")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "public_source.py").write_text("# public source inventory fixture\n")
            with patch("delivery_agent.ollama_model.OllamaModel._http", side_effect=AssertionError("must not call provider")) as http:
                receipt = runner.preflight_context(public_task(), args, workspace)
            http.assert_not_called()
        self.assertEqual(receipt["default_agent_module"], "minisweagent.agents.default")
        self.assertEqual(receipt["llm_calls"], 0)
        self.assertIsNone(receipt["provider_usage"])
        self.assertTrue(receipt["initial_fits"])
        self.assertEqual(receipt["available_input_budget"], 13500)
        self.assertEqual(receipt["estimate_method"], "utf8_bytes_plus_message_framing")

    def test_baseline_receipt_is_model_free_and_dependency_fault_is_not_a_feature_gap(self):
        class Environment:
            image_id = "sha256:" + "b" * 64
            def __init__(self, *args, **kwargs):
                self.index = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def execute(self, action):
                self.index += 1
                return {"returncode": 0 if self.index == 1 else 1,
                        "output": "old coverage passed\n" if self.index == 1 else error}
        for error, confirmed in (("TypeError: new contract absent\n", True),
                                 ("ModuleNotFoundError: unavailable dependency\n", False)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(runner, "checkout", return_value=Path(temporary)), \
                    patch.object(runner, "DockerSession", Environment):
                receipt = runner.run_baseline_checks(public_task(), temporary)
                self.assertEqual(receipt["feature_gap_confirmed"], confirmed)
                self.assertTrue(receipt["model_free"])
                self.assertEqual(receipt["llm_calls"], 0)
                self.assertTrue(receipt["initial_check_passed"])
                self.assertEqual(len(receipt["checks"]), 2)

    def test_submission_does_not_mask_failed_independent_verification_and_export_is_grader_compatible(self):
        with tempfile.TemporaryDirectory(dir=runner.ROOT / "examples", prefix="closure-contract-") as temporary:
            root, task = Path(temporary), public_task()
            task_file = root / "task.json"
            value = asdict(task)
            value["public_checks"] = list(task.public_checks)
            atomic_json(task_file, value)
            effective, exported = replace(task, image="sha256:" + "d" * 64), replace(task, image="python@sha256:" + "b" * 64)
            image = {"public_base_reference": task.image, "public_base_digest": exported.image,
                     "public_base_image_id": "sha256:" + "c" * 64, "inference_image_id": effective.image,
                     "operator_dockerfile_sha256": "e" * 64, "operator_image_tag": "owned-test-image:contract"}
            args = argparse.Namespace(task=str(task_file), output=str(root / "private"), evidence=str(root / "public"),
                ollama="unused-fixture-binary", model="qwen2.5-coder:7b", ollama_mode="json", profile="recent",
                label="contract", max_steps=24, context_tokens=16000, max_output_tokens=1500, wall_seconds=1800,
                request_timeout=300, command_timeout=90, seed=42, cost_limit=3.)

            class Daemon:
                identity = {"digest": "a" * 64, "name": args.model}
                def __init__(self, *args, **kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def assert_identity(self):
                    pass

            def model_boundary(given_task, given_args, directory):
                self.assertEqual(given_task, effective)
                self.assertEqual(given_args.backend, "docker")
                self.assertEqual(given_args.provider, "ollama_native")
                self.assertIsNone(given_args.knowledge_db)
                (directory / "agent").mkdir(parents=True)
                (directory / "agent" / "request-views.jsonl").write_text('{"event":"request","call":1}\n')
                return {"fixture": False, "provider": "ollama_native", "status": "submitted", "exit_status": "Submitted",
                    "model_patch": "diff --git a/new.py b/new.py\n", "patch_valid": True, "seconds": 1.,
                    "public_checks": [{"command": command, "returncode": 0, "passed": True} for command in task.public_checks],
                    "verification_status": "passed", "delivery": {"usage": {"calls": 1}},
                    "provenance": {"config": {"model": given_args.model}, "model_digest": "a" * 64,
                                   "task_hash": effective.fingerprint(), "implementation_sha256": "f" * 64}}

            def independent_boundary(manifest, output, **kwargs):
                runs = load_plan(manifest)  # Real strict consumer, not a mirrored schema.
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["task"].image, exported.image)
                self.assertEqual(runs[0]["evidence"]["verification_status"], "deferred")
                self.assertTrue(kwargs["pull_images"])
                result = {"infrastructure_failures": [], "runs": [{"delivery_success": False}]}
                atomic_json(output, result)
                return result

            with patch.object(runner, "prepare_task_image", return_value=(effective, exported, image)), \
                    patch.object(runner, "load_suite_task", return_value=(task, task_file.read_bytes())), \
                    patch.object(runner, "run_baseline_checks", return_value={"resolved_image_id": image["public_base_image_id"],
                                                                             "feature_gap_confirmed": True}), \
                    patch.object(runner, "preflight_context", return_value={"initial_fits": True}), \
                    patch.object(runner, "OwnedOllama", Daemon), \
                    patch.object(runner, "hardware_metadata", return_value={"system": "contract-fixture"}), \
                    patch.object(runner, "run_repository", side_effect=model_boundary) as run_model, \
                    patch.object(runner, "grade", side_effect=independent_boundary), \
                    patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                # Source Git HEAD is a genuine local read, isolated from the Docker mock.
                with patch.object(runner, "git", return_value=SimpleNamespace(stdout=b"1" * 40)):
                    summary = runner.run_proof(args)
                    failed_args = argparse.Namespace(**vars(args))
                    failed_args.output, failed_args.evidence = str(root / "private-failure"), str(root / "public-failure")
                    run_model.side_effect = RuntimeError("controlled model boundary interruption")
                    with self.assertRaises(RuntimeError):
                        runner.run_proof(failed_args)
                    failure = json.loads((root / "public-failure" / "summary.json").read_text(encoding="utf-8"))
                    self.assertEqual(failure["attempted_count"], 0)
                    self.assertFalse(failure["attempt_started"])
                    self.assertEqual(failure["model_status"], "unknown_started")
                    self.assertIsNone(failure["delivery_success"])
                    self.assertIsNone(failure["usage"])
                    self.assertFalse(failure["complete"])
            self.assertTrue(summary["internal_independent_checks_passed"])
            self.assertFalse(summary["external_independent_checks_passed"])
            self.assertFalse(summary["delivery_success"])
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["attempted_count"], 1)
            self.assertEqual(summary["model_digest"], "a" * 64)
            self.assertIsNone(summary["official_resolved"])
            self.assertFalse((root / "public" / "spec.json").exists())
            self.assertFalse((root / "public" / "runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
