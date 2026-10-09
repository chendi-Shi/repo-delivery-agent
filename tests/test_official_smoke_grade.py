"""Pure grading pipeline contracts. Fixtures are never official benchmark scores."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.official_smoke_grade import (PARQUET_SHA, download_verified_parquet, export_public_reports,
                                         grade, read_inputs, validate_grading_rows)

SOURCE = Path(__file__).resolve().parents[1] / "examples" / "official-smoke" / "source.json"
MODEL = "trusted-official-contract-model"


def predictions(root, *, patch_text="diff --git a/a b/a\n", failures=2):
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    path = root / "official-predictions.jsonl"
    rows = [{"instance_id": source["instance_ids"][0], "model_name_or_path": MODEL, "model_patch": patch_text}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    attempts = [{"instance_id": source["instance_ids"][0], "model_name_or_path": MODEL, "submitted": True, "status": "submitted"}]
    attempts += [{"instance_id": item, "model_name_or_path": MODEL, "submitted": False, "status": "failed"}
                 for item in source["instance_ids"][1:1 + failures]]
    path.with_name(path.name + ".attempts.json").write_text(json.dumps({"schema_version": 1,
        "predictions_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "attempts": attempts}), encoding="utf-8")
    return source, path, rows, attempts


class OfficialPipelineTests(unittest.TestCase):
    def test_missing_predictions_remain_waiting_and_never_claim_official_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = grade(SOURCE, root / "missing.jsonl", root / "out", "fixture")
            self.assertEqual(summary["grading_status"], "waiting_for_predictions_and_attempts")
            self.assertIsNone(summary["official_resolved_rate"])
            self.assertFalse(summary["complete"])

    def test_real_failed_attempt_denominator_is_preserved_and_sidecar_hash_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, path, _, _ = predictions(root)
            _, actual, attempts, status = read_inputs(SOURCE, path)
            self.assertEqual((len(actual), len(attempts), status), (1, 3, "ready"))
            path.write_text(path.read_text() + "\n")
            with self.assertRaises(ValueError):
                read_inputs(SOURCE, path)

    def test_actual_zero_submissions_are_separate_from_absent_input_and_no_grade_is_invented(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, path, _, attempts = predictions(root)
            path.write_text("")
            for attempt in attempts:
                attempt["submitted"], attempt["status"] = False, "failed"
            path.with_name(path.name + ".attempts.json").write_text(json.dumps({"schema_version": 1,
                "predictions_sha256": hashlib.sha256(b"").hexdigest(), "attempts": attempts}))
            summary = grade(SOURCE, path, root / "out", "fixture")
            self.assertEqual(summary["grading_status"], "no_submissions")
            self.assertEqual(summary["attempted_denominator"], 3)
            self.assertEqual(summary["agent_failure_count"], 3)
            self.assertIsNone(summary["official_resolved_rate"])

    def test_unpinned_source_foreign_model_and_out_of_selection_predictions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, path, rows, attempts = predictions(root)
            bad = root / "bad-source.json"
            bad.write_text(json.dumps({**source, "revision": "a" * 40}))
            with self.assertRaises(ValueError):
                read_inputs(bad, path)
            rows[0]["model_name_or_path"] = "other-model"
            attempts[0]["model_name_or_path"] = "other-model"
            path.write_text(json.dumps(rows[0]) + "\n")
            path.with_name(path.name + ".attempts.json").write_text(json.dumps({"schema_version": 1,
                "predictions_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "attempts": attempts}))
            with self.assertRaises(ValueError):
                read_inputs(SOURCE, path)

    def test_download_checks_exact_sha_and_removes_unverified_or_oversized_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "fixture.parquet"
            with patch("scripts.official_smoke_grade.urllib.request.urlopen", return_value=io.BytesIO(b"not the official dataset")):
                with self.assertRaises(ValueError):
                    download_verified_parquet({"parquet_url": "https://fixture.invalid/data"}, destination)
            self.assertFalse(destination.exists())
            with patch("scripts.official_smoke_grade.urllib.request.urlopen", return_value=io.BytesIO(b"large")), \
                    patch("scripts.official_smoke_grade.MAX_PARQUET_BYTES", 2):
                with self.assertRaises(ValueError):
                    download_verified_parquet({"parquet_url": "https://fixture.invalid/data"}, destination)
            self.assertFalse(destination.exists())

    def test_row_validation_checks_v5_fields_and_eval_script_image_pins(self):
        script = "private fixture eval script"
        selected = {"instance_id": "fixture-1", "repo": "fixture/repo", "version": "1", "image": "swebench/sweb.eval.x86_64.fixture:latest",
                    "eval_script": script, "log_parser": "pytest", "eval_type": "fail_to_pass", "FAIL_TO_PASS": [], "PASS_TO_PASS": []}
        rows = [{"instance_id": f"other-{number}"} for number in range(499)] + [selected]
        source = {"record_count": 500, "grading_metadata": [{"instance_id": "fixture-1", "image": selected["image"],
                  "eval_script_sha256": hashlib.sha256(script.encode()).hexdigest()}]}
        self.assertEqual(validate_grading_rows(source, rows, ["fixture-1"]), [selected])
        selected["eval_script"] = "modified"
        with self.assertRaises(ValueError):
            validate_grading_rows(source, rows, ["fixture-1"])

    def test_public_artifacts_exclude_gold_scripts_test_status_and_raw_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, rows, _ = predictions(root)
            instance = rows[0]["instance_id"]
            private = root / "private" / MODEL / instance
            private.mkdir(parents=True)
            (private / "report.json").write_text(json.dumps({instance: {"resolved": False, "patch_successfully_applied": True,
                "tests_status": {"gold_test_secret": "gold_body_secret"}, "test_patch": "gold_patch_secret"}}))
            (private / "patch.diff").write_text(rows[0]["model_patch"])
            (private / "eval.sh").write_text("gold_script_secret")
            (private / "test_output.txt").write_text("gold_output_secret")
            public = root / "public"
            export_public_reports(root / "private", public, rows)
            content = "".join(path.read_text() for path in public.rglob("*") if path.is_file())
            for secret in ("gold_test_secret", "gold_body_secret", "gold_patch_secret", "gold_script_secret", "gold_output_secret"):
                self.assertNotIn(secret, content)
            self.assertFalse((public / MODEL / instance / "eval.sh").exists())
            report = json.loads((public / MODEL / instance / "report.json").read_text())
            self.assertEqual(report[instance], {"resolved": False, "patch_successfully_applied": True})


if __name__ == "__main__":
    unittest.main()
