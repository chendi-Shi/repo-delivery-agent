"""Controlled target artifact fixtures, never model-performance measurements.

The immutable checked-in source evidence is copied to exercise its real importer
gate; target reports below are deliberate verifier-contract fixtures.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from unittest import mock
from dataclasses import asdict
from pathlib import Path

from scripts import summarize_codex_transfer as summary
from scripts import retry_codex_transfer as retry


class CodexTransferSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-transfer-summary-")
        self.root = summary.extended_path(self.temporary.name)
        self.verification = self.root / "independent"
        original = summary.extended_path(summary.ROOT / "experiments/codex-transfer-v1")
        for folder in ("source", "source-verification"):
            shutil.copytree(original / folder, self.root / folder)
        for filename in ("plan.json", "source-package.json"):
            shutil.copyfile(original / filename, self.root / filename)
        self.plan, _, self.planned = summary._plan(self.root)
        self.source, self.source_cost = summary._source(self.root, self.plan)
        self.template = json.loads(next((self.root / "source/model-runs").glob("*/evidence.json")).read_bytes())

    def tearDown(self):
        # TemporaryDirectory otherwise traverses its original unprefixed name,
        # which can leave >260-character descendants behind on Windows.
        self.temporary.name = str(summary.extended_path(self.temporary.name))
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    def create(self, label, *, submitted=True, passed=True, read=True, input_tokens=100, calls=2, seconds=2):
        planned = next(row for row in self.planned if row["label"] == label)
        task = planned["task"]
        directory = self.root / "targets" / label
        run_id = task.instance_id + "-" + planned["profile"] + "-" + label
        model = directory / "model-runs" / run_id
        model.mkdir(parents=True)
        task_value = asdict(task)
        task_value["public_checks"] = list(task.public_checks)
        self.write(model / "task.json", task_value)
        patch = b"controlled target patch fixture\n"
        (model / "model.patch").write_bytes(patch)
        seeded = planned["knowledge"] == "verified_seed"
        records = [{"record": self.source["record"], "origin": self.source["origin"], "expires_at": None}] if seeded else []
        package = {"schema": 1, "kind": "frozen_codex_trial_knowledge_input", "tools_enabled": True,
            "mode": planned["knowledge"], "namespace_sha256": self.source["origin"]["namespace_sha256"],
            "source_database_sha256": "d" * 64 if seeded else None, "records": records, "target_records_reused": False}
        self.write(directory / "knowledge-input.json", package)
        receipt = {"tools_enabled": True, "mode": package["mode"], "record_count": len(records),
            "namespace_sha256": package["namespace_sha256"], "package": "knowledge-input.json",
            "package_sha256": summary.digest((directory / "knowledge-input.json").read_bytes()),
            "source_database_sha256": package["source_database_sha256"], "origins": [row["origin"] for row in records]}
        audit_rows = []
        if seeded:
            origin = self.source["origin"]
            operation = "read" if read else "search"
            command = "delivery-knowledge " + ("read " + origin["source_id"] + " 0 50" if read else "search default_factory")
            audit_rows.append({"event": "action", "outcome": "completed", "returncode": 0,
                "action_kind": "delivery-knowledge/" + operation, "knowledge_operation": operation,
                "knowledge_namespace_sha256": origin["namespace_sha256"], "knowledge_records": [origin],
                "command_preview": command, "command_chars": len(command), "command_sha256": summary.digest(command),
                "call": 1, "ordinal": 0})
        audit_blob = "".join(json.dumps(row) + "\n" for row in audit_rows).encode()
        (model / "action-audit.jsonl").write_bytes(audit_blob)
        audit = {"available": True, "complete": True, "actions": len(audit_rows),
                 "public_audit_sha256": summary.digest(audit_blob)}
        # Expected receipt is assembled from the protocol contract, not the
        # summary consumer, so a search cannot become a read by shared logic.
        matches = []
        if seeded and read:
            command = audit_rows[0]["command_preview"]
            body = self.source["record"]["body"][:50]
            matches = [{"audit_line": 1, "call": 1, "ordinal": 0, "source_id": origin["source_id"],
                "content_sha256": origin["content_sha256"], "verification_sha256": origin["verification_sha256"],
                "command_sha256": summary.digest(command), "offset": 0, "limit": 50,
                "returned_chars": len(body), "slice_sha256": summary.digest(body)}]
        relative = "model-runs/" + run_id
        observation = {"knowledge_observed_read": bool(matches), "nonempty_read_count": len(matches),
            "observed_nonempty_read_count_lower_bound": len(matches), "reads": matches, "audit_complete": True,
            "public_audit_sha256": summary.digest(audit_blob), "input_package_sha256": receipt["package_sha256"],
            "input_package": "knowledge-input.json", "action_audit": relative + "/action-audit.jsonl"}
        self.write(directory / "knowledge-observation.json", observation)
        evidence = copy.deepcopy(self.template)
        evidence.update(profile=planned["profile"], task_hash=task.fingerprint(),
            status="submitted" if submitted else "stopped", exit_status="Submitted" if submitted else "LimitsExceeded",
            patch_sha256=summary.digest(patch), patch_valid=True, seconds=seconds, model_calls=calls,
            context_requests=calls, usage={"complete": True, "input_tokens": input_tokens, "output_tokens": 10,
                "calls_with_usage": calls, "unknown_requests": 0}, knowledge_input=receipt,
            knowledge_observation=observation, action_audit=audit)
        self.write(model / "evidence.json", evidence)
        value = {key: evidence[key] for key in ("provider", "profile", "model_name", "model_identity", "exit_status",
            "patch_sha256", "patch_valid", "usage", "model_calls", "context_requests", "seconds", "action_audit",
            "budgets", "model_call_unit", "knowledge_input", "knowledge_observation")}
        value.update(fixture=False, label=label, model_status=evidence["status"], source_task_sha256=planned["frozen_task_sha256"],
            source_task_fingerprint=task.fingerprint(), submitted=submitted, attempt_started=calls > 0, attempted_count=int(calls > 0))
        self.write(directory / "summary.json", value)
        manifest = {"schema": 1, "kind": "real_model_repository_ablation", "runs": [{"id": run_id,
            "task": relative + "/task.json", "patch": relative + "/model.patch", "evidence": relative + "/evidence.json"}]}
        self.write(directory / "repository-ablation.json", manifest)
        verified = {"id": run_id, "instance_id": task.instance_id, "profile": evidence["profile"],
            "provider": evidence["provider"], "model_name": evidence["model_name"], "model_digest": None,
            "model_identity": evidence["model_identity"], "model_status": evidence["status"],
            "task_sha256": summary.digest((model / "task.json").read_bytes()),
            "evidence_sha256": summary.digest((model / "evidence.json").read_bytes()),
            "patch_sha256": evidence["patch_sha256"], "submitted": submitted, "public_check_eligible": True,
            "patch_valid": True, "resolved_image": "sha256:" + "b" * 64,
            "verification_status": "passed" if passed else "failed", "delivery_success": submitted and passed,
            "public_delivery_status": "passed" if submitted and passed else "failed",
            "public_checks": [{"command": command, "returncode": 0 if passed else 1, "passed": passed}
                              for command in task.public_checks]}
        report = {"schema": 1, "kind": "independent_public_repository_verification", "attempts": 1,
            "manifest_sha256": summary.digest((directory / "repository-ablation.json").read_bytes()),
            "runs": [verified], "infrastructure_failures": []}
        self.write(self.verification / label / "verification.json", report)
        return directory, model

    def summarize(self):
        return summary.summarize(self.root, self.verification)

    def copy_retry_history(self):
        original = summary.extended_path(summary.ROOT / "experiments/codex-transfer-v1")
        shutil.copytree(original / "setup-failures-attempt1", self.root / "setup-failures-attempt1")
        shutil.copyfile(original / "retry-receipt.json", self.root / "retry-receipt.json")
        shutil.copyfile(original / "retry-receipt-correction.json", self.root / "retry-receipt-correction.json")
        return json.loads((self.root / "retry-receipt.json").read_bytes())

    def write_retry_receipt(self, receipt):
        self.write(self.root / "retry-receipt.json", receipt)
        correction = json.loads((self.root / "retry-receipt-correction.json").read_bytes())
        correction["original_receipt_sha256"] = summary.digest((self.root / "retry-receipt.json").read_bytes())
        self.write(self.root / "retry-receipt-correction.json", correction)

    def rebind_retry_file(self, receipt, relative):
        """Simulate a consistently rehashed but semantically dishonest bundle."""
        path = self.root / relative
        item = next(row for row in receipt["preserved_files"] if row["path"] == relative)
        item.update(sha256=summary.digest(path.read_bytes()), bytes=path.stat().st_size)

    def copy_subset_history(self):
        self.copy_retry_history()
        original = summary.extended_path(summary.ROOT / "experiments/codex-transfer-v1")
        shutil.copytree(original / retry.ARCHIVE, self.root / retry.ARCHIVE)
        shutil.copyfile(original / "execution.json", self.root / "execution.json")
        project = self.root / "fixture-project"
        (project / "scripts").mkdir(parents=True)
        shutil.copyfile(summary.ROOT / "scripts/run_codex_trial.py", project / "scripts/run_codex_trial.py")
        return project

    def test_second_retry_retains_four_zero_call_failures_without_adding_target_samples(self):
        project = self.copy_subset_history()
        with mock.patch.object(retry, "ROOT", project):
            receipt = retry.prepare_receipt(self.root)
            retry.validate_receipt(self.root, launching=True, output="runs/ct3")
            result = self.summarize()
        history = result["additional_setup_retry_history"]
        self.assertEqual((history["launcher_failures"], history["model_calls"], history["model_attempts"]), (4, 0, 0))
        self.assertEqual(len(history["preserved_artifacts"]), 17)
        self.assertEqual(result["preserved_zero_call_launcher_failures"], 14)
        self.assertEqual(result["targets"]["planned_trials"], 10)
        self.assertEqual(result["targets"]["unknown"], 10)
        self.assertEqual([row["source_tracked_tree_clean"] for row in history["setup_rows"]], [True, True, False, False])
        self.assertEqual(receipt["original_setup_rows"], history["setup_rows"])
        self.assertFalse((project / "runs/ct3").exists())
        self.assertFalse((self.root / retry.EXECUTION).exists())

    def test_second_retry_refuses_real_calls_changed_budget_and_unplanned_artifacts(self):
        self.copy_subset_history()
        path = self.root / retry.ARCHIVE / "targets/split-baseline-empty-r1/summary.json"
        original = json.loads(path.read_bytes())
        variants = [{**original, "model_calls": 1}, {**original, "attempt_started": True},
                    {**original, "usage": {**original["usage"], "input_tokens": 1}},
                    {**original, "budgets": {**original["budgets"], "max_steps": 25}},
                    {**original, "source_tracked_tree_clean": None}]
        for index, variant in enumerate(variants):
            with self.subTest(index=index):
                self.write(path, variant)
                with self.assertRaises(ValueError):
                    retry._snapshot(self.root)
        self.write(path, original)
        self.write(self.root / retry.ARCHIVE / "hidden-model-result.json", {"model_calls": 1})
        with self.assertRaisesRegex(ValueError, "unplanned"):
            retry._snapshot(self.root)

    def test_second_retry_receipt_binds_all_original_bytes_and_launch_code(self):
        project = self.copy_subset_history()
        with mock.patch.object(retry, "ROOT", project):
            retry.prepare_receipt(self.root)
            runner = project / "scripts/run_codex_trial.py"
            original_runner = runner.read_bytes()
            runner.write_bytes(original_runner + b"\n")
            with self.assertRaisesRegex(ValueError, "executable changed"):
                retry.validate_receipt(self.root, launching=True, output="runs/ct3")
            runner.write_bytes(original_runner)
            summary_path = self.root / retry.ARCHIVE / "targets/split-baseline-empty-r1/summary.json"
            summary_path.write_bytes(summary_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "preserved original evidence"):
                retry.validate_receipt(self.root)

    def test_second_retry_launch_uses_only_four_frozen_empty_split_rows_and_short_slots(self):
        project = self.copy_subset_history()
        seen = []

        class FixtureProcess:
            returncode = 0

            def __init__(self, arguments, **kwargs):
                seen.append(arguments)

            def wait(self, timeout):
                return self.returncode

        with mock.patch.object(retry, "ROOT", project), mock.patch.object(retry.subprocess, "Popen", FixtureProcess):
            receipt = retry.prepare_receipt(self.root)
            self.assertEqual(retry.launch(self.root, "runs/ct3", "fixture-unused-codex"), 0)
            history = retry.summary_history(self.root)
            with self.assertRaisesRegex(ValueError, "cannot overwrite"):
                retry.launch(self.root, "runs/ct3", "fixture-unused-codex")
        self.assertEqual(len(seen), 4)
        labels = {arguments[arguments.index("--label") + 1] for arguments in seen}
        self.assertEqual(labels, {row["label"] for row in receipt["retry"]["planned_order"]})
        self.assertTrue(all(label.startswith("split-") for label in labels))
        for arguments in seen:
            self.assertIn("--knowledge-empty", arguments)
            self.assertNotIn("--knowledge-db", arguments)
            for flag, expected in {"--model": "gpt-6-astra", "--max-steps": "24", "--context-tokens": "64000",
                                   "--wall-seconds": "1800", "--request-timeout": "120",
                                   "--knowledge-namespace": "codex-transfer-v1"}.items():
                self.assertEqual(arguments[arguments.index(flag) + 1], expected)
            self.assertIn(Path(arguments[arguments.index("--output") + 1]).name, ("t1", "t2", "t3", "t4"))
        self.assertTrue(history["retry_execution"]["launcher_batch_complete"])
        self.assertEqual(history["retry_execution"]["launcher_outcomes_recorded"], 4)
        self.assertEqual(history["model_calls"], 0)

    def test_second_retry_requires_receipt_and_rejects_repeated_launcher_rows(self):
        project = self.copy_subset_history()
        with self.assertRaisesRegex(ValueError, "lacks its predeclared receipt"):
            retry.summary_history(self.root)
        with mock.patch.object(retry, "ROOT", project):
            retry.prepare_receipt(self.root)
            receipt, snapshot, receipt_sha = retry.validate_receipt(self.root)
            row = {"label": snapshot["rows"][0]["label"], "launcher_returncode": 1,
                   "launcher_timed_out": False, "launcher_elapsed_seconds": 1, "summary_present": False}
            execution = {"schema": 1, "kind": "zero_call_subset_retry_execution", "fixture": False,
                "plan_sha256": snapshot["plan_sha256"], "retry_receipt_sha256": receipt_sha,
                "original_execution_sha256": snapshot["original_execution"]["sha256"], "max_parallel_trials": 2,
                "planned_order": snapshot["rows"], "outcomes": [row, row]}
            self.write(self.root / retry.EXECUTION, execution)
            with self.assertRaisesRegex(ValueError, "repeats or adds"):
                retry.summary_history(self.root)

    def test_missing_trials_remain_ten_unknown_with_separate_real_source_cost(self):
        result = self.summarize()
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual((result["targets"]["successes"], result["targets"]["failures"], result["targets"]["unknown"]), (0, 0, 10))
        self.assertIsNone(result["targets"]["all_attempt_measurements"]["input_tokens"]["known_subtotal"])
        self.assertTrue(result["source"]["delivery_success"])
        self.assertGreater(result["source"]["input_tokens"], 0)
        self.assertTrue(result["source"]["excluded_from_target_denominator"])

    def test_success_pair_reports_negative_benefit_and_all_attempt_costs(self):
        self.create("last-retrieval-empty-r1", input_tokens=100, calls=2, seconds=2)
        self.create("last-retrieval-verified-r1", input_tokens=150, calls=3, seconds=4)
        self.create("last-baseline-empty-r1", submitted=False, input_tokens=80, calls=2)
        self.create("split-baseline-empty-r1", passed=False, input_tokens=60, calls=1)
        result = self.summarize()
        self.assertEqual((result["targets"]["successes"], result["targets"]["failures"], result["targets"]["unknown"]), (2, 2, 6))
        self.assertEqual(result["targets"]["all_attempt_measurements"]["input_tokens"]["known_subtotal"], 390)
        self.assertEqual(result["targets"]["all_attempt_measurements"]["model_calls"]["known_subtotal"], 8)
        pair = result["knowledge_only_pairs"][0]
        self.assertTrue(pair["both_independently_delivered_and_comparable"])
        self.assertTrue(pair["knowledge_observed_read"])
        self.assertEqual(pair["differences"]["input_tokens"]["verified_minus_empty"], 50)
        self.assertEqual(pair["differences"]["seconds"]["verified_minus_empty"], 2)
        self.assertEqual(pair["differences"]["model_calls"]["verified_minus_empty"], 1)
        self.assertIsNone(result["knowledge_only_pairs"][1]["differences"]["input_tokens"]["verified_minus_empty"])

    def test_successful_search_is_not_a_source_read(self):
        self.create("last-retrieval-verified-r1", read=False)
        result = self.summarize()
        row = next(row for row in result["rows"] if row["label"] == "last-retrieval-verified-r1")
        self.assertTrue(row["delivery_success"])
        self.assertFalse(row["knowledge_observed_read"])
        self.assertEqual(row["knowledge"]["nonempty_read_count"], 0)

    def test_forged_read_receipt_is_recomputed_and_cost_measurements_survive_invalid_gate(self):
        directory, model = self.create("last-retrieval-verified-r1", read=False)
        for path, nested in ((directory / "knowledge-observation.json", None),
                             (directory / "summary.json", "knowledge_observation"),
                             (model / "evidence.json", "knowledge_observation")):
            value = json.loads(path.read_bytes())
            target = value if nested is None else value[nested]
            target["knowledge_observed_read"] = True
            self.write(path, value)
        result = self.summarize()
        row = next(row for row in result["rows"] if row["label"] == "last-retrieval-verified-r1")
        self.assertEqual(row["evidence_status"], "invalid")
        self.assertIsNone(row["delivery_success"])
        self.assertIsNone(row["knowledge_observed_read"])
        self.assertEqual(row["model_calls"], 2)

    def test_patch_summary_and_independent_check_tampering_cannot_pass(self):
        directory, model = self.create("last-baseline-empty-r1")
        (model / "model.patch").write_bytes(b"tampered")
        other, _ = self.create("last-retrieval-empty-r1")
        value = json.loads((other / "summary.json").read_bytes())
        value["usage"]["input_tokens"] += 1
        self.write(other / "summary.json", value)
        self.create("split-baseline-empty-r1")
        path = self.verification / "split-baseline-empty-r1/verification.json"
        report = json.loads(path.read_bytes())
        report["runs"][0]["public_checks"][0]["command"] = "echo invented pass"
        self.write(path, report)
        result = self.summarize()
        self.assertEqual(result["targets"]["successes"], 0)
        self.assertEqual(result["targets"]["failures"], 0)
        self.assertEqual(result["targets"]["unknown"], 10)
        row = next(row for row in result["rows"] if row["label"] == "split-baseline-empty-r1")
        self.assertEqual(row["verification_status"], "invalid")
        self.assertEqual(row["model_calls"], 2)

    def test_missing_verifier_preserves_actual_cost_and_failed_pair_has_no_efficiency_delta(self):
        self.create("last-retrieval-empty-r1", passed=False)
        self.create("last-retrieval-verified-r1")
        self.create("split-baseline-empty-r1")
        (self.verification / "split-baseline-empty-r1/verification.json").unlink()
        result = self.summarize()
        self.assertEqual((result["targets"]["successes"], result["targets"]["failures"], result["targets"]["unknown"]), (1, 1, 8))
        self.assertEqual(result["targets"]["all_attempt_measurements"]["model_calls"]["known_subtotal"], 6)
        self.assertFalse(result["knowledge_only_pairs"][0]["both_independently_delivered_and_comparable"])
        self.assertIsNone(result["knowledge_only_pairs"][0]["differences"]["seconds"]["verified_minus_empty"])

    def test_archived_setup_retry_is_hash_checked_and_never_hides_model_attempts(self):
        receipt = self.copy_retry_history()
        result = self.summarize()
        history = result["setup_retry_history"]
        self.assertEqual((history["launcher_failures"], history["model_calls"], history["model_attempts"]), (10, 0, 0))
        self.assertEqual(len(history["preserved_artifacts"]), 31)
        self.assertEqual(history["inventory_correction"]["additional_task_exports"], 10)
        self.assertEqual(result["targets"]["planned_trials"], 10)
        self.assertGreater(history["launcher_seconds_sum"], history["trial_seconds_sum"])
        self.assertGreater(history["trial_seconds_sum"], 0)
        relative = "setup-failures-attempt1/targets/last-baseline-empty-r1/summary.json"
        path = self.root / relative
        original_bytes = path.read_bytes()
        path.write_bytes(original_bytes + b"\n")
        with self.assertRaises(ValueError):
            self.summarize()
        # Even recomputing the outer hash cannot relabel a real invocation as a
        # setup-only retry; the measured fields are also checked.
        value = json.loads(original_bytes)
        value["model_calls"] = 1
        self.write(path, value)
        self.rebind_retry_file(receipt, relative)
        self.write_retry_receipt(receipt)
        with self.assertRaises(ValueError):
            self.summarize()

    def test_retry_rehashed_changed_budget_timing_or_execution_identity_is_rejected(self):
        receipt = self.copy_retry_history()
        relative = "setup-failures-attempt1/targets/last-baseline-empty-r1/summary.json"
        path = self.root / relative
        original = json.loads(path.read_bytes())
        variants = [{**original, "seconds": None}, {**original, "source_tracked_tree_clean": False},
                    {**original, "patch_valid": True},
                    {**original, "budgets": {**original["budgets"], "selected_context_byte_budget": 32000}},
                    {**original, "usage": {**original["usage"], "input_tokens": 1}}]
        for index, variant in enumerate(variants):
            with self.subTest(index=index):
                self.write(path, variant)
                self.rebind_retry_file(receipt, relative)
                self.write_retry_receipt(receipt)
                with self.assertRaises(ValueError):
                    self.summarize()
        self.write(path, original)
        self.rebind_retry_file(receipt, relative)
        execution_relative = "setup-failures-attempt1/execution.json"
        execution_path = self.root / execution_relative
        execution = json.loads(execution_path.read_bytes())
        execution["planned_order"][0]["task"] = "examples/different-task.json"
        self.write(execution_path, execution)
        self.rebind_retry_file(receipt, execution_relative)
        self.write_retry_receipt(receipt)
        with self.assertRaises(ValueError):
            self.summarize()

    def test_retry_rehashed_knowledge_body_must_match_independent_source(self):
        receipt = self.copy_retry_history()
        prefix = "setup-failures-attempt1/targets/last-retrieval-verified-r1/"
        package_path, summary_path = self.root / (prefix + "knowledge-input.json"), self.root / (prefix + "summary.json")
        package, value = json.loads(package_path.read_bytes()), json.loads(summary_path.read_bytes())
        package["records"][0]["record"]["body"] = "invented knowledge that was never independently verified"
        package["records"][0]["origin"]["content_sha256"] = summary.digest(package["records"][0]["record"]["body"])
        self.write(package_path, package)
        value["knowledge_input"]["package_sha256"] = summary.digest(package_path.read_bytes())
        value["knowledge_input"]["origins"] = [package["records"][0]["origin"]]
        self.write(summary_path, value)
        self.rebind_retry_file(receipt, prefix + "knowledge-input.json")
        self.rebind_retry_file(receipt, prefix + "summary.json")
        self.write_retry_receipt(receipt)
        with self.assertRaisesRegex(ValueError, "knowledge differs from frozen source"):
            self.summarize()

    def test_retry_archive_cannot_hide_an_unlisted_artifact_or_omit_receipt(self):
        self.copy_retry_history()
        extra = self.root / "setup-failures-attempt1/unlisted-model-evidence.json"
        self.write(extra, {"fixture": True, "model_calls": 1})
        with self.assertRaisesRegex(ValueError, "unlisted artifacts"):
            self.summarize()
        # Missing receipt is itself fatal even before inspecting the inventory.
        (self.root / "retry-receipt.json").unlink()
        with self.assertRaisesRegex(ValueError, "lack their retry receipt"):
            self.summarize()

    def test_correction_binds_original_receipt_and_exactly_ten_additional_tasks(self):
        self.copy_retry_history()
        receipt_path = self.root / "retry-receipt.json"
        original = receipt_path.read_bytes()
        receipt_path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "correction is unbound"):
            self.summarize()
        receipt_path.write_bytes(original)
        correction_path = self.root / "retry-receipt-correction.json"
        correction = json.loads(correction_path.read_bytes())
        correction["additional_preserved_files"].pop()
        self.write(correction_path, correction)
        with self.assertRaisesRegex(ValueError, "exactly the ten planned task exports"):
            self.summarize()

    def test_rehashed_archived_task_cannot_change_the_frozen_requirement(self):
        self.copy_retry_history()
        correction_path = self.root / "retry-receipt-correction.json"
        correction = json.loads(correction_path.read_bytes())
        item = correction["additional_preserved_files"][0]
        path = self.root / item["path"]
        value = json.loads(path.read_bytes())
        value["problem_statement"] += "\nForged replacement requirement."
        self.write(path, value)
        item.update(sha256=summary.digest(path.read_bytes()), bytes=path.stat().st_size)
        self.write(correction_path, correction)
        with self.assertRaisesRegex(ValueError, "task export differs from the frozen task"):
            self.summarize()

    def test_long_path_json_is_read_and_enumerated_instead_of_silently_missing(self):
        deep = self.root / ("nested-a-" * 16) / ("nested-b-" * 16)
        path = deep / "task.json"
        self.write(path, {"fixture": True})
        unprefixed = str(path).removeprefix("\\\\?\\")
        self.assertGreater(len(unprefixed), 260)
        value, raw = summary._json(Path(unprefixed))
        self.assertEqual(value, {"fixture": True})
        self.assertEqual(raw, path.read_bytes())
        self.assertIn(path, list(summary.extended_path(self.root).rglob("task.json")))

    def test_copied_repetition_cannot_be_counted_as_a_second_trial(self):
        original, _ = self.create("last-retrieval-empty-r1")
        shutil.copytree(original, self.root / "targets/last-retrieval-empty-r2")
        shutil.copytree(self.verification / "last-retrieval-empty-r1", self.verification / "last-retrieval-empty-r2")
        result = self.summarize()
        copied = next(row for row in result["rows"] if row["label"] == "last-retrieval-empty-r2")
        self.assertEqual(copied["evidence_status"], "invalid")
        self.assertIsNone(copied["delivery_success"])
        self.assertEqual(result["targets"]["successes"], 1)
        # Renaming only the summary label cannot bless the original run ID.
        path = self.root / "targets/last-retrieval-empty-r2/summary.json"
        value = json.loads(path.read_bytes())
        value["label"] = "last-retrieval-empty-r2"
        self.write(path, value)
        copied = next(row for row in self.summarize()["rows"] if row["label"] == "last-retrieval-empty-r2")
        self.assertEqual(copied["evidence_status"], "invalid")

    def test_zero_invocation_submission_cannot_be_a_real_model_success(self):
        self.create("last-baseline-empty-r1", submitted=True, calls=0)
        result = self.summarize()
        row = next(row for row in result["rows"] if row["label"] == "last-baseline-empty-r1")
        self.assertEqual(row["evidence_status"], "invalid")
        self.assertIsNone(row["delivery_success"])
        self.assertEqual(result["targets"]["successes"], 0)


if __name__ == "__main__":
    unittest.main()
