"""The bridge tests do not use a model, Docker or real benchmark answers."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from delivery_agent.benchmark import (
    ANSWER_FIELDS,
    BenchmarkError,
    HARNESS_COMMIT,
    aggregate_official_reports,
    export_predictions,
    freeze_dataset,
    grade_command,
    load_manifest,
    read_dataset,
)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def task(self, instance="org__project-1"):
        return {"instance_id": instance, "repo": "org/project", "base_commit": "a" * 40,
                "problem_statement": "Repair Unicode output.", "version": "1.0"}

    def result(self, instance="org__project-1", **overrides):
        return {"instance_id": instance, "provider": "litellm", "fixture": False,
                "model_name_or_path": "provider/model", "status": "submitted",
                "exit_status": "Submitted", "model_patch": "diff --git a/x b/x\n",
                "patch_valid": True, "official_resolved": True, **overrides}

    def export(self, *results):
        paths = [self.write(f"results/{i}/result.json", row) for i, row in enumerate(results)]
        output = self.root / "predictions.jsonl"
        return output, export_predictions(paths, output)

    def report(self, prediction, *, resolved=True, applied=True, patch=None):
        directory = self.root / "reports" / prediction["model_name_or_path"].replace("/", "__") / prediction["instance_id"]
        self.write(str(directory.relative_to(self.root) / "report.json"), {
            prediction["instance_id"]: {"resolved": resolved, "patch_successfully_applied": applied},
        })
        (directory / "patch.diff").write_bytes((prediction["model_patch"] if patch is None else patch).encode("utf-8"))
        return directory

    def test_freeze_excludes_every_answer_and_unknown_nested_field(self):
        row = self.task()
        row.update({key: f"SECRET_{key}" for key in ANSWER_FIELDS})
        row["image_assets"] = {"patch": "SECRET_ASSET"}
        source = self.write("private/data.json", [row])
        result = freeze_dataset(source, self.root / "snapshot", "org/dataset", "b" * 40)
        tasks = load_manifest(result["manifest_path"])
        self.assertEqual(tasks, [self.task()])
        self.assertEqual(result["snapshot_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(result["harness_commit"], HARNESS_COMMIT)
        for path in (Path(result["manifest_path"]), Path(result["metadata_path"])):
            self.assertNotIn("SECRET", path.read_text())
        self.assertFalse(result["revision_verified_remotely"])

    def test_keyed_map_and_jsonl_have_same_public_tasks(self):
        task = self.task()
        task.pop("instance_id")
        keyed = self.write("keyed.json", {"org__project-1": task})
        line = self.root / "rows.jsonl"
        line.write_text("\n" + json.dumps(self.task()) + "\n", encoding="utf-8")
        self.assertEqual(read_dataset(keyed), read_dataset(line))

    def test_mismatched_key_is_rejected(self):
        with self.assertRaises(BenchmarkError):
            read_dataset(self.write("wrong.json", {"different-1": self.task()}))

    def test_bad_required_fields_are_rejected(self):
        cases = [("base_commit", "abcdef"), ("base_commit", "g" * 40), ("repo", "../repo"),
                 ("repo", "https://github.com/org/repo"), ("instance_id", "../task"),
                 ("instance_id", "task/path"), ("problem_statement", ""), ("problem_statement", 3),
                 ("version", None)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(BenchmarkError):
                read_dataset(self.write("bad.json", [{**self.task(), key: value}]))

    def test_duplicate_tasks_and_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(BenchmarkError):
            read_dataset(self.write("duplicate.json", [self.task(), self.task()]))
        path = self.root / "keys.json"
        path.write_text('{"x": {}, "x": {}}')
        with self.assertRaises(BenchmarkError):
            read_dataset(path)

    def test_empty_and_scalar_dataset_are_rejected(self):
        for value in ([], {}, None, 4, ["row"]):
            with self.subTest(value=value), self.assertRaises(BenchmarkError):
                read_dataset(self.write("bad.json", value))

    def test_jsonl_malformed_line_has_location(self):
        path = self.root / "rows.jsonl"
        path.write_text(json.dumps(self.task()) + "\n{nope}\n", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "rows.jsonl:2"):
            read_dataset(path)

    def test_selection_is_explicit_and_unique(self):
        source = self.write("dataset.json", [self.task("task-1"), self.task("task-2")])
        frozen = freeze_dataset(source, self.root / "one", "org/dataset", "b" * 40, instance_ids=["task-2"])
        self.assertEqual(frozen["source_record_count"], 2)
        self.assertEqual([task["instance_id"] for task in load_manifest(frozen["manifest_path"])], ["task-2"])
        for ids in ([], ["missing"], ["task-1", "task-1"]):
            with self.subTest(ids=ids), self.assertRaises(BenchmarkError):
                freeze_dataset(source, self.root / "bad", "org/dataset", "b" * 40, instance_ids=ids)

    def test_dataset_pin_and_fresh_output_are_required(self):
        source = self.write("data.json", [self.task()])
        with self.assertRaises(BenchmarkError):
            freeze_dataset(source, self.root / "snapshot", "org/dataset", "main")
        freeze_dataset(source, self.root / "snapshot", "org/dataset", "b" * 40)
        with self.assertRaises(BenchmarkError):
            freeze_dataset(source, self.root / "snapshot", "org/dataset", "b" * 40)

    def test_modified_manifest_is_rejected(self):
        source = self.write("data.json", [self.task()])
        frozen = freeze_dataset(source, self.root / "snapshot", "org/dataset", "b" * 40)
        manifest = Path(frozen["manifest_path"])
        doc = json.loads(manifest.read_text())
        doc["tasks"][0]["problem_statement"] = "Changed"
        manifest.write_text(json.dumps(doc))
        with self.assertRaisesRegex(BenchmarkError, "hash"):
            load_manifest(manifest)

    def test_answer_field_in_manifest_rejected_even_with_matching_hash(self):
        source = self.write("data.json", [self.task()])
        frozen = freeze_dataset(source, self.root / "snapshot", "org/dataset", "b" * 40)
        manifest, metadata = Path(frozen["manifest_path"]), Path(frozen["metadata_path"])
        doc = json.loads(manifest.read_text())
        doc["tasks"][0]["patch"] = "answer"
        manifest.write_text(json.dumps(doc))
        pin = json.loads(metadata.read_text())
        pin["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        metadata.write_text(json.dumps(pin))
        with self.assertRaisesRegex(BenchmarkError, "forbidden"):
            load_manifest(manifest)

    def test_export_retains_empty_and_locally_invalid_patch(self):
        output, summary = self.export(self.result(model_patch="", patch_valid=False))
        self.assertEqual(summary["submitted_count"], 1)
        self.assertEqual(summary["empty_patch_count"], 1)
        self.assertEqual(json.loads(output.read_text())["model_patch"], "")
        self.assertIsNone(summary["submitted"][0]["official_resolved"])
        self.assertFalse(summary["submitted"][0]["patch_valid"])

    def test_export_excludes_fixtures_failures_and_unverified_providers(self):
        cases = [self.result("fixture", fixture=True), self.result("replay", provider="replay"),
                 self.result("missing", fixture=None), self.result("failed", status="failed"),
                 self.result("timeout", exit_status="TimeoutError"), self.result("provider", provider="")]
        output, summary = self.export(*cases)
        self.assertEqual(output.read_text(), "")
        self.assertEqual(len(summary["excluded"]), len(cases))

    def test_codex_cli_keeps_submission_protocol_and_failed_attempt_denominator(self):
        output, summary = self.export(
            self.result("cli-submitted", provider="codex_cli", model_name_or_path="gpt-6-astra"),
            self.result("cli-failed", provider="codex_cli", model_name_or_path="gpt-6-astra",
                        status="failed", exit_status=None),
            self.result("cli-fixture", provider="codex_cli", fixture=True))
        self.assertEqual(summary["submitted_count"], 1)
        self.assertEqual(summary["attempted_count"], 2)
        self.assertEqual(summary["agent_failure_count"], 1)
        self.assertEqual(json.loads(output.read_text())["instance_id"], "cli-submitted")
        self.assertIsNone(summary["official_resolved"])

    def test_export_rejects_malformed_and_duplicate_submissions(self):
        for overrides in ({"model_patch": None}, {"patch_valid": "true"}, {"instance_id": "../x"},
                          {"model_name_or_path": "../model\\name"}):
            with self.subTest(overrides=overrides), self.assertRaises(BenchmarkError):
                self.export(self.result(**overrides))
        with self.assertRaisesRegex(BenchmarkError, "duplicate"):
            self.export(self.result(), self.result())

    def test_export_generator_cannot_overwrite_source(self):
        source = self.write("result.json", self.result())
        with self.assertRaisesRegex(BenchmarkError, "overwrite"):
            export_predictions((item for item in [source]), source)
        self.assertEqual(json.loads(source.read_text())["status"], "submitted")

    def test_grade_command_matches_official_args_without_shell(self):
        command = grade_command("a path/predictions.jsonl", "private dataset.json", "run-1", instance_ids=["task-1"])
        self.assertEqual(command[:3], ["python", "-m", "swebench.harness.run_evaluation"])
        self.assertEqual(command[command.index("--dataset_name") + 1], "private dataset.json")
        self.assertEqual(command[-2:], ["--instance_ids", "task-1"])
        self.assertNotIn("--cache_level", command)
        for kwargs in ({"max_workers": False}, {"timeout": 0}, {"instance_ids": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BenchmarkError):
                grade_command("pred.jsonl", "data.json", "run-1", **kwargs)

    def test_missing_report_is_unknown_and_denominator_is_requested(self):
        first, second = self.result("task-1"), self.result("task-2")
        predictions, _ = self.export(first, second)
        self.report(first)
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["requested_count"], 2)
        self.assertEqual(summary["resolved_count"], 1)
        self.assertEqual(summary["missing_ids"], ["task-2"])
        self.assertEqual(summary["patch_valid_unknown_count"], 1)
        self.assertIsNone(summary["outcomes"][1]["official_resolved"])
        self.assertIsNone(summary["submitted_only_resolved_rate"])
        self.assertIsNone(summary["all_attempt_resolved_rate"])
        self.assertFalse(summary["complete"])

    def test_complete_reports_use_official_boolean_not_agent_claim(self):
        first, second = self.result("task-1"), self.result("task-2")
        predictions, _ = self.export(first, second)
        self.report(first, resolved=True)
        self.report(second, resolved=False, applied=False)
        output = self.root / "summary.json"
        summary = aggregate_official_reports(predictions, self.root / "reports", output)
        self.assertEqual(summary["submitted_only_resolved_rate"], 0.5)
        self.assertEqual(summary["all_attempt_resolved_rate"], 0.5)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["unresolved_ids"], ["task-2"])
        self.assertEqual(summary["patch_invalid_count"], 1)
        self.assertFalse(summary["outcomes"][1]["official_resolved"])
        self.assertEqual(json.loads(output.read_text()), summary)

    def test_invalid_official_booleans_do_not_turn_truthy(self):
        prediction = self.result()
        predictions, _ = self.export(prediction)
        for resolved, applied in (("true", True), (1, True), (None, True), (True, "false"), (True, False)):
            with self.subTest(resolved=resolved, applied=applied):
                self.report(prediction, resolved=resolved, applied=applied)
                summary = aggregate_official_reports(predictions, self.root / "reports")
                self.assertEqual(summary["invalid_count"], 1)
                self.assertIsNone(summary["outcomes"][0]["official_resolved"])

    def test_stale_or_missing_patch_evidence_is_invalid(self):
        prediction = self.result()
        predictions, _ = self.export(prediction)
        directory = self.report(prediction, patch="different patch")
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["invalid_count"], 1)
        (directory / "patch.diff").unlink()
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["invalid_count"], 1)

    def test_empty_prediction_set_has_no_fabricated_rate(self):
        predictions, _ = self.export(self.result(fixture=True))
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["requested_count"], 0)
        self.assertIsNone(summary["submitted_only_resolved_rate"])
        self.assertIsNone(summary["all_attempt_resolved_rate"])
        self.assertFalse(summary["complete"])

    def test_failed_real_attempts_stay_in_full_denominator(self):
        winner = self.result("task-0")
        losers = [self.result(f"task-{index}", status="failed", exit_status="ProviderError")
                  for index in range(1, 10)]
        predictions, exported = self.export(winner, *losers, self.result("fixture", fixture=True))
        self.assertEqual(exported["attempted_count"], 10)
        self.assertEqual(exported["agent_failure_count"], 9)
        self.report(winner)
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["attempted_denominator"], 10)
        self.assertEqual(summary["submitted_only_resolved_rate"], 1.0)
        self.assertEqual(summary["all_attempt_resolved_rate"], 0.1)
        self.assertEqual(len(summary["unresolved_agent_failures"]), 9)

    def test_attempt_denominator_without_sidecar_is_unknown(self):
        winner = self.result()
        predictions, _ = self.export(winner)
        predictions.with_name(predictions.name + ".attempts.json").unlink()
        self.report(winner)
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["submitted_only_resolved_rate"], 1.0)
        self.assertIsNone(summary["all_attempt_resolved_rate"])
        self.assertIsNone(summary["attempted_count"])

    def test_modified_predictions_cannot_reuse_stale_attempt_denominator(self):
        predictions, _ = self.export(self.result())
        predictions.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(BenchmarkError, "sidecar"):
            aggregate_official_reports(predictions, self.root / "reports")

    def test_windows_patch_log_line_endings_preserve_payload_hash(self):
        winner = self.result()
        predictions, _ = self.export(winner)
        self.report(winner, patch=winner["model_patch"].replace("\n", "\r\n"))
        summary = aggregate_official_reports(predictions, self.root / "reports")
        self.assertEqual(summary["resolved_count"], 1)
        outcome = summary["outcomes"][0]
        self.assertNotEqual(outcome["submitted_patch_sha256"], outcome["logged_patch_sha256"])


if __name__ == "__main__":
    unittest.main()
