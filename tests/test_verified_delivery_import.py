"""Artifact-contract fixtures only: no model/Docker success is claimed by these tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery_agent.knowledge import KnowledgeStore
from scripts import ingest_verified_delivery as importing
from scripts.grade_experiments import load_plan


class VerifiedDeliveryImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "repository-ablation.json"
        self.report = self.root / "verification.json"
        self.guard_path = self.root / "source-guard.json"
        self.database = self.root / "seed.sqlite"
        self.package = self.root / "seed-package.json"
        self.source, *self.targets = importing.frozen_tasks()
        self.task = json.loads((importing.ROOT / "examples/delivery-suite-v1" / importing.FROZEN_NAMES[0]).read_bytes())
        self.patch = b"diff --git a/more_itertools/more.py b/more_itertools/more.py\n# controlled contract fixture\n"
        self.evidence = {"fixture": False, "provider": "codex_cli", "status": "submitted", "exit_status": "Submitted",
            "profile": "baseline", "model_name": "fixture-model", "model_digest": None,
            "model_identity": {"kind": "codex_cli", "model": "fixture-model", "cli_version": "codex-cli 0.1.0",
                "binary_sha256": "a" * 64, "weight_digest_available": False, "reasoning_effort": "low"},
            "patch_sha256": importing.digest(self.patch), "patch_valid": True,
            "task_hash": self.source[0].fingerprint(), "usage": {"completion_tokens": 1},
            "seconds": 1.0, "context_requests": 1, "verification_status": "deferred", "inference_backend": "files"}
        self.write(self.root / "task.json", self.task)
        (self.root / "model.patch").write_bytes(self.patch)
        self.write(self.root / "evidence.json", self.evidence)
        self.write(self.manifest, {"schema": 1, "kind": "real_model_repository_ablation", "runs": [
            {"id": "source-fixture", "task": "task.json", "patch": "model.patch", "evidence": "evidence.json"}]})
        self.build_verification()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def build_verification(self):
        run = load_plan(self.manifest)[0]
        self.checked = {"id": run["id"], "instance_id": run["task"].instance_id, "profile": self.evidence["profile"],
            "provider": self.evidence["provider"], "model_name": self.evidence["model_name"], "model_digest": None,
            "model_identity": self.evidence["model_identity"], "model_status": self.evidence["status"],
            "patch_sha256": self.evidence["patch_sha256"], "task_sha256": run["task_sha256"],
            "evidence_sha256": run["evidence_sha256"], "submitted": True, "patch_valid": True,
            "public_check_eligible": True, "delivery_success": True, "verification_status": "passed",
            "public_delivery_status": "passed", "resolved_image": "sha256:" + "b" * 64,
            "public_checks": [{"command": command, "returncode": 0, "passed": True,
                "output_sha256": importing.digest("fixture check output"), "output_preview": "fixture check output"}
                for command in run["task"].public_checks]}
        self.report_value = {"schema": 1, "kind": "independent_public_repository_verification",
            "manifest_sha256": importing.digest(self.manifest.read_bytes()), "attempts": 1, "verified_attempts": 1,
            "submitted": 1, "delivery_successes": 1, "runs": [self.checked], "infrastructure_failures": []}
        self.guard = {"schema": 1, "kind": "source_seed_leakage_guard", "eligible": True,
            "manifest_sha256": importing.digest(self.manifest.read_bytes()), "source_run_id": run["id"],
            "source_task_sha256": self.source[1], "source_evidence_sha256": run["evidence_sha256"],
            "source_patch_sha256": self.evidence["patch_sha256"], "resolved_image": self.checked["resolved_image"],
            "violations": [], "omitted_violations": 0,
            "targets": [{"function": function, "old_ast_sha256": "c" * 64, "new_ast_sha256": "c" * 64}
                        for function in ("last", "split_after")],
            "functional_targets": [{"instance_id": task.instance_id, "task_sha256": task_sha,
                "command_sha256": importing.digest(task.public_checks[-1]), "returncode": 1,
                "output_sha256": importing.digest("KeyError"), "exception_types": ["KeyError"],
                "target_feature_still_missing": True} for task, task_sha in self.targets]}
        self.save_receipts()

    def save_receipts(self):
        self.write(self.report, self.report_value)
        self.guard["verification_sha256"] = importing.digest(self.report.read_bytes())
        self.write(self.guard_path, self.guard)

    def ingest(self):
        return importing.ingest_verified_delivery(self.manifest, self.report, self.guard_path,
                                                  self.database, "test:external-source", self.package)

    def assert_rejected(self):
        with self.assertRaises(ValueError):
            self.ingest()
        self.assertFalse(self.database.exists())
        self.assertFalse(self.package.exists())

    def test_valid_artifacts_produce_verified_machine_derived_record_without_mutation(self):
        # External fresh-checkout application is authoritative; a failed local
        # patch preflight remains recorded, never rewritten as a local success.
        self.evidence["patch_valid"] = False
        self.write(self.root / "evidence.json", self.evidence)
        self.build_verification()
        originals = {path: path.read_bytes() for path in
                     (self.manifest, self.report, self.guard_path, self.root / "evidence.json", self.root / "model.patch")}
        package = self.ingest()
        record, origin = package["record"], package["origin"]
        self.assertIsNone(package["internal_checks_passed"])
        self.assertEqual(record["verification"]["internal_verification_status"], "deferred")
        self.assertEqual(record["verification"]["kind"], "external_independent_public_checks")
        self.assertIs(record["verification"]["source_patch_valid"], False)
        self.assertIs(record["verification"]["patch_valid"], True)
        self.assertIn(self.task["problem_statement"], record["body"])
        self.assertIn(self.patch.decode(), record["body"])
        self.assertIn("fixture check output", record["body"])
        self.assertEqual(origin["content_sha256"], importing.digest(record["body"]))
        self.assertEqual(origin["verification_sha256"], importing.digest(importing.canonical(record["verification"])))
        identity = {key: record["verification"][key] for key in ("run_id", "task_sha256", "evidence_sha256", "patch_sha256")}
        self.assertEqual(record["source_ref"], "run:" + importing.digest(importing.canonical(identity)))
        store = KnowledgeStore(self.database, "test:external-source")
        self.assertTrue(store.read(origin["source_id"])["verified"])
        store.close()
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        before_db, before_package = self.database.read_bytes(), self.package.read_bytes()
        with self.assertRaises(ValueError):
            self.ingest()
        self.assertEqual(self.database.read_bytes(), before_db)
        self.assertEqual(self.package.read_bytes(), before_package)

    def test_patch_manifest_and_report_hash_tampering_reject_before_database_creation(self):
        with self.subTest("patch bytes"):
            (self.root / "model.patch").write_bytes(self.patch + b"tamper")
            self.assert_rejected()
        (self.root / "model.patch").write_bytes(self.patch)
        with self.subTest("manifest binding"):
            self.report_value["manifest_sha256"] = "d" * 64
            self.save_receipts()
            self.assert_rejected()
        self.report_value["manifest_sha256"] = importing.digest(self.manifest.read_bytes())
        self.save_receipts()
        with self.subTest("report bytes"):
            self.report.write_bytes(self.report.read_bytes() + b"\n")
            self.assert_rejected()

    def test_false_or_nonboolean_success_and_changed_public_commands_reject(self):
        for field, value in (("delivery_success", False), ("submitted", 1), ("patch_valid", False)):
            with self.subTest(field=field):
                self.checked[field] = value
                self.save_receipts()
                self.assert_rejected()
                self.checked[field] = True
        check = self.checked["public_checks"][0]
        for field, value in (("passed", False), ("returncode", False), ("command", "echo fake success")):
            with self.subTest(field=field):
                previous = check[field]
                check[field] = value
                self.save_receipts()
                self.assert_rejected()
                check[field] = previous

    def test_unsubmitted_source_cannot_be_promoted_by_successful_external_report(self):
        self.evidence.update(status="stopped", exit_status="LimitsExceeded")
        self.write(self.root / "evidence.json", self.evidence)
        self.build_verification()
        self.assert_rejected()

    def test_source_pollution_and_missing_or_replaced_target_checks_reject(self):
        for field, value in (("eligible", False), ("violations", [{"kind": "protected_definition_changed"}])):
            with self.subTest(field=field):
                original = self.guard[field]
                self.guard[field] = value
                self.save_receipts()
                self.assert_rejected()
                self.guard[field] = original
        row = self.guard["targets"][0]
        row["new_ast_sha256"] = "d" * 64
        self.save_receipts()
        self.assert_rejected()
        row["new_ast_sha256"] = row["old_ast_sha256"]
        functional = self.guard["functional_targets"][0]
        for field, value in (("target_feature_still_missing", False), ("returncode", 0),
                             ("exception_types", []), ("command_sha256", "d" * 64), ("task_sha256", "d" * 64)):
            with self.subTest(field=field):
                original = functional[field]
                functional[field] = value
                self.save_receipts()
                self.assert_rejected()
                functional[field] = original

    def test_independent_report_task_evidence_and_image_bindings_reject_mismatch(self):
        for field, value in (("task_sha256", "d" * 64), ("evidence_sha256", "d" * 64),
                             ("resolved_image", "sha256:" + "d" * 64)):
            with self.subTest(field=field):
                previous = self.checked[field]
                self.checked[field] = value
                self.save_receipts()
                self.assert_rejected()
                self.checked[field] = previous


if __name__ == "__main__":
    unittest.main()
