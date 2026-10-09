"""Frozen real-model artifacts, fresh Git application, and honest denominators."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from delivery_agent.repository import RepoTask, checkout, git

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("grade_experiments", ROOT / "scripts/grade_experiments.py")
grading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grading)


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "source"
        self.base.mkdir()
        git(self.base, "init", "-b", "main")
        (self.base / "module.py").write_text("value = 1\n", encoding="utf-8")
        git(self.base, "add", "module.py")
        git(self.base, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
        self.commit = git(self.base, "rev-parse", "HEAD").stdout.decode().strip()
        self.task = {"instance_id": "public-feature", "repo": "example/project", "base_commit": self.commit,
            "problem_statement": "Set value to two", "image": "python:3.12-slim",
            "public_checks": ["python -c 'import module; assert module.value == 2'"]}
        (self.base / "module.py").write_text("value = 2\n", encoding="utf-8")
        self.patch = git(self.base, "diff", self.commit, "--").stdout
        self.manifest = self.root / "experiments" / "repository-ablation.json"
        self.manifest.parent.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def create_run(self, name, *, status="submitted", profile="retrieval", patch=None, model_digest="sha256:model"):
        directory = self.manifest.parent / "model-runs" / name
        directory.mkdir(parents=True)
        blob = self.patch if patch is None else patch
        (directory / "task.json").write_text(json.dumps(self.task), encoding="utf-8")
        (directory / "model.patch").write_bytes(blob)
        evidence = {"fixture": False, "provider": "ollama_native", "status": status,
            "exit_status": "Submitted" if status == "submitted" else "ModelError", "profile": profile,
            "model_name": "ollama_chat/model", "model_digest": model_digest,
            "patch_sha256": hashlib.sha256(blob).hexdigest(), "usage": {"completion_tokens": 100},
            "seconds": 2.5, "context_requests": 2, "verification_status": "deferred"}
        (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        return {"id": name, "task": f"model-runs/{name}/task.json", "patch": f"model-runs/{name}/model.patch",
                "evidence": f"model-runs/{name}/evidence.json"}

    def plan(self, runs):
        self.manifest.write_text(json.dumps({"schema": 1, "kind": "real_model_repository_ablation", "runs": runs}),
                                 encoding="utf-8")

    def local_checkout(self, task, destination):
        return checkout(task, destination, source=self.base)

    def fake_session(self, check_passes=True):
        outer = self

        class VerifierSession:
            image_id = "sha256:verified-image"

            def __init__(self, work, image, timeout):
                self.work = Path(work)
                outer.assertEqual((self.work / "module.py").read_text(), "value = 2\n")

            def __enter__(self):
                return self

            def execute(self, action):
                return {"output": "public check output", "returncode": 0 if check_passes else 1}

            def __exit__(self, *args):
                pass

        return VerifierSession

    def verifier(self, run, **kwargs):
        return grading.verify_one(run, checkout_factory=self.local_checkout, session_factory=self.fake_session(), **kwargs)

    def test_patch_hash_and_frozen_task_load(self):
        self.plan([self.create_run("one")])
        run = grading.load_plan(self.manifest)[0]
        self.assertEqual(run["task"].base_commit, self.commit)
        self.assertEqual(run["patch"], self.patch)

    def test_manifest_retains_bounded_limitations(self):
        self.plan([self.create_run("one")])
        plan = json.loads(self.manifest.read_text())
        plan["limitations"] = ["Small public task sample; no official benchmark claim"]
        self.manifest.write_text(json.dumps(plan))
        self.assertEqual(len(grading.load_plan(self.manifest)), 1)
        plan["limitations"] = ["x" * 2001]
        self.manifest.write_text(json.dumps(plan))
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)

    def test_tampered_patch_rejected(self):
        self.plan([self.create_run("one")])
        (self.manifest.parent / "model-runs/one/model.patch").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)

    def test_answer_fields_and_escaped_artifacts_rejected(self):
        item = self.create_run("one")
        self.plan([{**item, "task": "../source/module.py"}])
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)
        self.plan([item])
        path = self.manifest.parent / item["task"]
        path.write_text(json.dumps({**self.task, "patch": "gold answer"}))
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)

    def test_submitted_requires_exit_protocol_and_model_identity(self):
        self.plan([self.create_run("one", model_digest=None)])
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)
        evidence_path = self.manifest.parent / "model-runs/one/evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence.update(model_digest="sha256:model", exit_status="StepLimitExceeded")
        evidence_path.write_text(json.dumps(evidence))
        with self.assertRaises(ValueError):
            grading.load_plan(self.manifest)

    def test_failed_unresolved_model_identity_retained(self):
        self.plan([self.create_run("one", status="failed", model_digest=None)])
        run = grading.load_plan(self.manifest)[0]
        with tempfile.TemporaryDirectory() as directory:
            report = self.verifier(run, directory=directory)
        self.assertEqual(report["model_identity_status"], "unresolved")
        self.assertFalse(report["delivery_success"])

    def test_fresh_checkout_applies_model_patch_before_public_verifier(self):
        self.plan([self.create_run("one")])
        report = grading.grade(self.manifest, self.root / "output" / "report.json", verifier=self.verifier)
        self.assertEqual(report["delivery_rate_all_attempts"], 1)
        self.assertEqual(report["runs"][0]["resolved_image"], "sha256:verified-image")
        self.assertIsNone(report["official_resolved"])

    def test_failed_partial_patch_can_pass_check_but_not_delivery(self):
        self.plan([self.create_run("one", status="failed")])
        report = grading.grade(self.manifest, self.root / "report.json", verifier=self.verifier)
        self.assertTrue(report["runs"][0]["public_checks"][0]["passed"])
        self.assertEqual(report["delivery_successes"], 0)
        self.assertEqual(report["attempts"], 1)

    def test_invalid_patch_is_model_failure_without_infrastructure_error(self):
        self.plan([self.create_run("one", patch=b"this is not a Git patch")])
        report = grading.grade(self.manifest, self.root / "report.json", verifier=self.verifier)
        self.assertFalse(report["runs"][0]["patch_valid"])
        self.assertEqual(report["infrastructure_failures"], [])
        self.assertEqual(report["delivery_rate_all_attempts"], 0)

    def test_denominator_keeps_failures_and_profiles_separate(self):
        self.plan([self.create_run("success", profile="retrieval"),
                   self.create_run("failure-1", status="failed", profile="recent"),
                   self.create_run("failure-2", status="stopped", profile="baseline")])
        report = grading.grade(self.manifest, self.root / "report.json", verifier=self.verifier)
        self.assertEqual(report["attempts"], 3)
        self.assertEqual(report["submitted"], 1)
        self.assertAlmostEqual(report["delivery_rate_all_attempts"], 1 / 3)
        self.assertEqual(report["by_profile"]["baseline"]["delivery_rate_all_attempts"], 0)

    def test_infrastructure_failure_written_and_rate_unknown(self):
        self.plan([self.create_run("one")])

        def fail(*args, **kwargs):
            raise RuntimeError("private infrastructure diagnostic")

        report = grading.grade(self.manifest, self.root / "report.json", verifier=fail)
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(report["submitted"], 1)
        self.assertEqual(report["verified_attempts"], 0)
        self.assertIsNone(report["delivery_rate_all_attempts"])
        self.assertNotIn("private", json.dumps(report))
        self.assertEqual(report["infrastructure_failures"][0]["error_type"], "RuntimeError")

    def test_tasks_without_public_checks_are_unknown_and_outside_public_delivery_denominator(self):
        public_success = self.create_run("public-success", profile="retrieval")
        public_failure = self.create_run("public-failure", status="stopped", profile="retrieval")
        official_only = self.create_run("official-only", profile="retrieval")
        official_task = self.manifest.parent / official_only["task"]
        official_task.write_text(json.dumps({**self.task, "instance_id": "official-task", "public_checks": []}), encoding="utf-8")
        self.plan([public_success, public_failure, official_only])
        report = grading.grade(self.manifest, self.root / "report.json", verifier=self.verifier)
        self.assertEqual(report["attempts"], 3)
        self.assertEqual(report["public_check_eligible_attempts"], 2)
        self.assertEqual(report["public_delivery_denominator"], 2)
        self.assertEqual(report["no_public_checks_attempts"], 1)
        self.assertEqual(report["public_delivery_unknown_attempts"], 1)
        self.assertEqual(report["delivery_successes"], 1)
        self.assertEqual(report["public_delivery_failures"], 1)
        self.assertEqual(report["public_delivery_rate"], 0.5)
        self.assertIsNone(report["delivery_rate_all_attempts"])
        self.assertEqual(report["by_profile"]["retrieval"]["public_delivery_rate"], 0.5)
        official = next(row for row in report["runs"] if row["id"] == "official-only")
        self.assertEqual(official["verification_status"], "no_public_checks")
        self.assertEqual(official["public_delivery_status"], "unknown_no_public_checks")
        self.assertFalse(official["public_check_eligible"])
        self.assertIsNone(official["delivery_success"])
        # Even a submitted/applicable patch cannot claim a public-check outcome
        # when the task defines no checks. A fully ineligible experiment has no
        # public delivery rate at all, while its real attempt remains counted.
        self.plan([official_only])
        no_checks = grading.grade(self.manifest, self.root / "no-checks.json", verifier=self.verifier)
        self.assertEqual(no_checks["attempts"], 1)
        self.assertEqual(no_checks["public_delivery_denominator"], 0)
        self.assertIsNone(no_checks["public_delivery_rate"])
        self.assertIsNone(no_checks["delivery_rate_all_attempts"])

    def test_actual_failed_public_check_does_not_count_success(self):
        self.plan([self.create_run("one")])

        def verifier(run, **kwargs):
            return grading.verify_one(run, checkout_factory=self.local_checkout,
                                      session_factory=self.fake_session(False), **kwargs)

        report = grading.grade(self.manifest, self.root / "report.json", verifier=verifier)
        self.assertFalse(report["runs"][0]["public_checks"][0]["passed"])
        self.assertEqual(report["delivery_successes"], 0)

    def test_bounded_context_audit_list_is_accepted(self):
        self.plan([self.create_run("one")])
        path = self.manifest.parent / "model-runs/one/evidence.json"
        evidence = json.loads(path.read_text())
        evidence["context_requests"] = [{"selected": ["e0001"], "usage": {"tokens": 10}}]
        path.write_text(json.dumps(evidence))
        self.assertEqual(len(grading.load_plan(self.manifest)), 1)


if __name__ == "__main__":
    unittest.main()
