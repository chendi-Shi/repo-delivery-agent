"""Controlled saved-result fixtures; real export, no provider or network call."""
from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from scripts import recover_codex_export as recovery
from scripts import summarize_codex_transfer as summary


class CodexExportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="export-recovery-")
        self.root = summary.extended_path(self.temporary.name)
        shutil.copyfile(summary.ROOT / "experiments/codex-transfer-v1/plan.json", self.root / "plan.json")
        self.plan, _, rows = summary._plan(self.root)
        self.label = "split-baseline-empty-r2"
        self.wanted = next(row for row in rows if row["label"] == self.label)
        self.task = self.wanted["task"]
        self.public = self.root / "targets" / self.label
        self.trial = self.root / "private" / "trial"
        self.trial.mkdir(parents=True)
        self.identity = {"kind": "codex_cli", "model": "gpt-6-astra", "cli_version": "codex-cli fixture",
            "binary_sha256": "b" * 64, "weight_digest_available": False, "reasoning_effort": "low"}
        self.usage = {"complete": False, "input_tokens": 0, "output_tokens": 0,
                      "calls_with_usage": 0, "unknown_requests": 1}
        namespace = summary.digest("codex-transfer-v1")
        self.package = {"schema": 1, "kind": "frozen_codex_trial_knowledge_input", "tools_enabled": True,
            "mode": "empty", "namespace_sha256": namespace, "source_database_sha256": None,
            "records": [], "target_records_reused": False}
        self.write(self.public / "knowledge-input.json", self.package)
        receipt = {"tools_enabled": True, "mode": "empty", "record_count": 0, "namespace_sha256": namespace,
            "package": "knowledge-input.json", "package_sha256": summary.digest((self.public / "knowledge-input.json").read_bytes()),
            "source_database_sha256": None, "origins": []}
        config = {"model": "gpt-6-astra", "profile": "baseline", "provider": "codex_cli", "backend": "files",
            "context_tokens": 64000, "max_steps": 24, "wall_seconds": 1800, "request_timeout": 120,
            "reasoning_effort": "low", "knowledge_namespace_sha256": namespace}
        self.spec = {"task": asdict(self.task), "task_hash": self.task.fingerprint(),
            "implementation_sha256": "c" * 64, "upstream_version": "fixture", "upstream_commit": "d" * 40,
            "config": config}
        self.write(self.trial / "spec.json", self.spec)
        self.result = {"instance_id": self.task.instance_id, "model_name_or_path": "gpt-6-astra",
            "fixture": False, "provider": "codex_cli", "status": "failed", "exit_status": None,
            "seconds": 3.5, "patch_valid": None, "patch_sha256": summary.digest(b""), "model_patch": "",
            "error_type": "CodexProtocolError", "delivery": {"model_calls": 1, "usage": self.usage},
            "provenance": {**copy.deepcopy(self.spec), "model_identity": self.identity}}
        self.write(self.trial / "result.json", self.result)
        (self.trial / "model.patch").write_bytes(b"")
        (self.trial / "agent").mkdir()
        (self.trial / "agent/request-views.jsonl").write_text(
            json.dumps({"event": "request", "call": 1}) + "\n" +
            json.dumps({"event": "error", "call": 1, "exception_type": "CodexProtocolError"}) + "\n", encoding="utf-8")
        self.original = {"schema": 1, "kind": "real_codex_source_trial", "fixture": False,
            "source_commit": "a" * 40, "source_tracked_tree_clean": False,
            "source_task_sha256": self.wanted["frozen_task_sha256"], "source_task_fingerprint": self.task.fingerprint(),
            "model_name": "gpt-6-astra", "provider": "codex_cli", "profile": "baseline", "label": self.label,
            "model_call_unit": "cli_invocations", "underlying_transport_request_count": None,
            "usage_scope": "controlled fixture", "complete": False, "delivery_success": None, "official_resolved": None,
            "attempt_started": None, "attempted_count": None, "context_requests": None, "model_calls": 1,
            "usage": copy.deepcopy(self.usage), "knowledge_input": receipt, "knowledge_observation": None,
            "budgets": {"max_steps": 24, "selected_context_byte_budget": 64000, "wall_seconds": 1800,
                "request_timeout": 120, "generation_token_cap_enforced": False, "seed_enforced": False,
                "cli_prompt_overhead_in_selected_context_budget": False, "output_and_protocol_reserve": 3048,
                "reasoning_effort": "low"},
            "model_status": "failed", "exit_status": None, "model_identity": self.identity,
            "seconds": 3.5, "patch_sha256": summary.digest(b""), "patch_valid": None,
            "error_type": "CodexProtocolError", "submitted": False, "export_error_type": "PermissionError"}
        self.write(self.public / "summary.json", self.original)

    def tearDown(self):
        self.temporary.name = str(summary.extended_path(self.temporary.name))
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def recover(self):
        with mock.patch.object(recovery.exporter, "run_repository", side_effect=AssertionError("inference prohibited")) as model:
            result = recovery.recover(self.root, self.label, self.trial)
            model.assert_not_called()
        return result

    def refresh_generated_hashes(self):
        path = self.public / recovery.RECEIPT
        value = json.loads(path.read_bytes())
        for item in value["generated_files"]:
            raw = (self.public / item["path"]).read_bytes()
            item.update(sha256=summary.digest(raw), bytes=len(raw))
        self.write(path, value)

    def test_real_export_of_missing_action_audit_preserves_original_and_one_failed_attempt(self):
        original = (self.public / "summary.json").read_bytes()
        knowledge = (self.public / "knowledge-input.json").read_bytes()
        private = recovery._private_snapshot(self.trial)
        result = self.recover()
        self.assertEqual(result["model_calls_added"], 0)
        self.assertEqual(result["original_model_calls"], 1)
        self.assertEqual((self.public / "summary.json").read_bytes(), original)
        self.assertEqual((self.public / "knowledge-input.json").read_bytes(), knowledge)
        self.assertEqual(recovery._private_snapshot(self.trial), private)
        audit = self.public / "model-runs/trial/action-audit.jsonl"
        self.assertEqual(audit.read_bytes(), b"")
        receipt = json.loads((self.public / recovery.RECEIPT).read_bytes())
        self.assertEqual(len(receipt["generated_files"]), 7)
        self.assertIsNone(receipt["recovery"]["private_inputs"]["action_audit"])
        run, value, entry, refs, measured = summary._trial(self.public, "baseline", self.task,
            self.wanted["frozen_task_sha256"], self.plan, self.label)
        self.assertEqual(value["export_error_type"], "PermissionError")
        self.assertEqual(value["source_commit"], "a" * 40)
        self.assertFalse(value["source_tracked_tree_clean"])
        self.assertFalse(value["submitted"])
        self.assertEqual((value["model_calls"], value["context_requests"], value["attempted_count"]), (1, 1, 1))
        self.assertTrue(measured["attempt_started"])
        self.assertFalse(measured["usage_complete"])
        self.assertEqual(measured["unknown_usage_requests"], 1)
        self.assertEqual(refs["summary"]["path"], recovery.RECOVERED)
        self.assertEqual(refs["original_summary"]["sha256"], summary.digest(original))
        observed = summary._knowledge(self.public, run, value, entry, self.wanted, None)
        self.assertIsNone(observed["knowledge_observed_read"])
        self.assertEqual(list(self.public.parent.glob(".export-stage-*")), [])

    def test_conflicting_result_or_actual_request_count_is_rejected_without_public_changes(self):
        original = (self.public / "summary.json").read_bytes()
        for key, value in (("seconds", 5), ("model_patch", "invented patch"), ("instance_id", "different-task")):
            with self.subTest(key=key):
                changed = {**self.result, key: value}
                self.write(self.trial / "result.json", changed)
                with self.assertRaises(ValueError):
                    self.recover()
        self.write(self.trial / "result.json", self.result)
        audit = self.trial / "agent/request-views.jsonl"
        audit.write_text(audit.read_text() + json.dumps({"event": "request", "call": 2}) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual((self.public / "summary.json").read_bytes(), original)
        self.assertEqual({path.name for path in self.public.iterdir()}, {"summary.json", "knowledge-input.json"})

    def test_nonfrozen_task_and_changed_private_patch_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a frozen target"):
            recovery.recover(self.root, "split-invented-r2", self.trial)
        changed = copy.deepcopy(self.spec)
        changed["task"]["problem_statement"] += " altered task"
        self.write(self.trial / "spec.json", changed)
        with self.assertRaisesRegex(ValueError, "not the frozen target"):
            self.recover()
        self.write(self.trial / "spec.json", self.spec)
        (self.trial / "model.patch").write_bytes(b"different saved patch")
        with self.assertRaisesRegex(ValueError, "patch bytes differ"):
            self.recover()

    def test_failed_staging_export_leaves_original_directory_untouched(self):
        before = {path.name: path.read_bytes() for path in self.public.iterdir()}
        with mock.patch.object(recovery.exporter, "export_trial", side_effect=OSError("controlled export failure")):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual({path.name: path.read_bytes() for path in self.public.iterdir()}, before)
        self.assertFalse((self.public / recovery.RECEIPT).exists())

    def test_already_exported_or_recovered_trial_is_never_overwritten(self):
        (self.public / "model-runs").mkdir()
        with self.assertRaisesRegex(ValueError, "no existing generated artifacts"):
            self.recover()
        (self.public / "model-runs").rmdir()
        self.recover()
        receipt = (self.public / recovery.RECEIPT).read_bytes()
        with self.assertRaisesRegex(ValueError, "no existing generated artifacts"):
            self.recover()
        self.assertEqual((self.public / recovery.RECEIPT).read_bytes(), receipt)

    def test_rehashed_hidden_action_and_changed_model_measurements_still_fail(self):
        self.recover()
        audit = self.public / "model-runs/trial/action-audit.jsonl"
        audit.write_bytes(b'{"event":"action"}\n')
        self.refresh_generated_hashes()
        with self.assertRaisesRegex(ValueError, "cannot hide executed actions"):
            recovery.validate_recovered_summary(self.public, self.original, (self.public / "summary.json").read_bytes())
        audit.write_bytes(b"")
        path = self.public / recovery.RECOVERED
        value = json.loads(path.read_bytes())
        value["seconds"] = 0
        self.write(path, value)
        self.refresh_generated_hashes()
        with self.assertRaisesRegex(ValueError, "rewrites original model measurements"):
            recovery.validate_recovered_summary(self.public, self.original, (self.public / "summary.json").read_bytes())

    def test_private_change_during_export_prevents_publication(self):
        export = recovery.exporter.export_trial

        def changed(*args, **kwargs):
            export(*args, **kwargs)
            path = self.trial / "agent/request-views.jsonl"
            path.write_bytes(path.read_bytes() + b"\n")

        original = (self.public / "summary.json").read_bytes()
        with mock.patch.object(recovery.exporter, "export_trial", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "private trial changed"):
                self.recover()
        self.assertEqual((self.public / "summary.json").read_bytes(), original)
        self.assertFalse((self.public / recovery.RECEIPT).exists())

    def test_rehashed_recovery_cannot_claim_a_different_plan(self):
        self.recover()
        receipt_path = self.public / recovery.RECEIPT
        receipt = json.loads(receipt_path.read_bytes())
        receipt["recovery"]["plan_sha256"] = "f" * 64
        for path in (self.public / recovery.RECOVERED, self.public / "model-runs/trial/evidence.json"):
            value = json.loads(path.read_bytes())
            value["export_recovery"] = receipt["recovery"]
            self.write(path, value)
        self.write(receipt_path, receipt)
        self.refresh_generated_hashes()
        with self.assertRaisesRegex(ValueError, "plan hash differs"):
            recovery.validate_recovered_summary(self.public, self.original, (self.public / "summary.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
