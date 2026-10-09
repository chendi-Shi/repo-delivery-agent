"""Functional pipeline contracts; these fixtures are never model scores."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import official_harness_smoke as smoke
from scripts.official_smoke_grade import grade, read_inputs

SOURCE = Path(__file__).resolve().parents[1] / "examples" / "official-smoke" / "source.json"


def report_fixture(root, **fields):
    directory = root / smoke.FIXTURE_MODEL / smoke.INSTANCE_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "patch.diff").write_bytes(smoke.FIXTURE_PATCH.encode())
    (directory / "report.json").write_text(json.dumps({smoke.INSTANCE_ID: {
        "patch_successfully_applied": True, "resolved": False, "infra_failure": False, **fields}}))
    return directory


class OfficialHarnessFixtureTests(unittest.TestCase):
    def test_control_patch_is_applicable_and_only_adds_explicit_non_solution_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True, timeout=30)
            original = root / "requests.py"
            original.write_text("# existing application is untouched\n")
            patch_path = root / "fixture.patch"
            patch_path.write_bytes(smoke.FIXTURE_PATCH.encode())
            parsed = subprocess.run(["git", "apply", "--numstat", str(patch_path)], cwd=root, check=True,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(parsed.stdout.strip(), "1\t0\tdelivery_harness_smoke.txt")
            subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=root, check=True, capture_output=True, timeout=30)
            subprocess.run(["git", "apply", str(patch_path)], cwd=root, check=True, capture_output=True, timeout=30)
            self.assertEqual((root / "delivery_harness_smoke.txt").read_text(), smoke.FIXTURE_TEXT)
            self.assertIn("not an issue solution", smoke.FIXTURE_TEXT)
            self.assertIn("no LLM calls", smoke.FIXTURE_TEXT)
            self.assertEqual(original.read_text(), "# existing application is untouched\n")

    def test_contract_requires_matching_report_patch_and_all_three_strict_booleans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                smoke.verify_contract_report(root)
            directory = report_fixture(root)
            self.assertEqual(smoke.verify_contract_report(root), {"patch_applied": True, "resolved": False, "infra_failure": False})
            for fields in ({"patch_successfully_applied": False}, {"resolved": True}, {"infra_failure": True},
                           {"patch_successfully_applied": 1}, {"infra_failure": None}):
                with self.subTest(fields=fields):
                    report_fixture(root, **fields)
                    with self.assertRaises(ValueError):
                        smoke.verify_contract_report(root)
            report_fixture(root)
            item = json.loads((directory / "report.json").read_text())
            del item[smoke.INSTANCE_ID]["infra_failure"]
            (directory / "report.json").write_text(json.dumps(item))
            with self.assertRaises(ValueError):
                smoke.verify_contract_report(root)
            report_fixture(root)
            (directory / "patch.diff").write_text("different patch\n")
            with self.assertRaises(ValueError):
                smoke.verify_contract_report(root)

    def test_fixture_orchestration_is_single_task_private_and_real_submission_guard_stays_closed(self):
        """Mock the container boundary, not official grading results in CI."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = "swebench/sweb.eval.x86_64.contract:latest"
            row = {"instance_id": smoke.INSTANCE_ID, "image": image, "test_patch": "private_gold_secret"}
            calls = []

            def run_boundary(argv, output_log, **kwargs):
                calls.append(argv)
                self.assertEqual(argv[1:3], ["-m", "swebench.harness.run_evaluation"])
                self.assertEqual(argv[argv.index("--max_workers") + 1], "1")
                self.assertEqual(argv[argv.index("--timeout") + 1], "900")
                self.assertEqual(argv[argv.index("--instance_ids") + 1:], [smoke.INSTANCE_ID])
                path = Path(argv[argv.index("--predictions_path") + 1])
                self.assertEqual(json.loads(path.read_text()), smoke.fixture_prediction())
                self.assertFalse(path.with_name(path.name + ".attempts.json").exists())
                private_run = Path.cwd() / "logs" / "evaluation" / "contract-fixture"
                report_fixture(private_run)
                directory = private_run / smoke.FIXTURE_MODEL / smoke.INSTANCE_ID
                payload = json.loads((directory / "report.json").read_text())
                payload[smoke.INSTANCE_ID]["tests_status"] = {"secret": "private_gold_secret"}
                (directory / "report.json").write_text(json.dumps(payload))
                (directory / "test_output.txt").write_text("private_gold_secret")
                return {"exit_code": 0, "stop_reason": None, "elapsed_seconds": 1.0}

            def docker_boundary(argv, **kwargs):
                if "--format" in argv:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"Id": "sha256:" + "a" * 64, "RepoDigests": []}).encode())
                return SimpleNamespace(returncode=0, stdout=b"")  # Image preexists; never remove it.

            with patch.object(smoke, "os", SimpleNamespace(name="posix")), \
                    patch.object(smoke, "verify_installed_harness", return_value={"commit": smoke.HARNESS_COMMIT, "run_log_root": "logs/evaluation"}), \
                    patch.object(smoke, "download_verified_parquet", return_value=123), \
                    patch.object(smoke, "select_grading_rows", return_value=[row]), \
                    patch.object(smoke.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024 ** 3)), \
                    patch.object(smoke, "run_bounded", side_effect=run_boundary), \
                    patch.object(smoke, "cleanup_task_container") as cleanup, \
                    patch.object(smoke.subprocess, "run", side_effect=docker_boundary) as docker:
                summary = smoke.run_smoke(SOURCE, root / "public", "contract-fixture")
            self.assertEqual(len(calls), 1)
            cleanup.assert_called_once_with(smoke.INSTANCE_ID, "contract-fixture")
            self.assertFalse(any(call.args[0][:3] == ["docker", "image", "rm"] for call in docker.call_args_list))
            self.assertTrue(summary["contract_passed"])
            self.assertTrue(summary["fixture"])
            self.assertEqual((summary["llm_calls"], summary["scope"]), (0, "functional_contract"))
            self.assertFalse(summary["model_score_eligible"])
            self.assertIsNone(summary["official_resolved_rate"])
            self.assertNotIn("private_gold_secret", "".join(path.read_text() for path in (root / "public").rglob("*") if path.is_file()))
            # Even after fixture success, the real grade input remains waiting.
            _, actual, attempts, status = read_inputs(SOURCE, root / "real-predictions.jsonl")
            self.assertEqual((actual, attempts, status), ([], [], "waiting_for_predictions_and_attempts"))
            real = grade(SOURCE, root / "real-predictions.jsonl", root / "real-grade", "real-run")
            self.assertFalse(real["complete"])
            self.assertIsNone(real["official_resolved_rate"])


if __name__ == "__main__":
    unittest.main()
