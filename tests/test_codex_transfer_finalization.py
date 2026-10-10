"""Offline integrity contracts using the preserved, independently graded cohort.

No model or grader is invoked. Each mutation affects only a temporary copy.
"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from scripts import finalize_codex_transfer as finalizer


ROOT = Path(__file__).resolve().parents[1]


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-finalization-")
        self.root = finalizer.consumer.extended_path(Path(self.temporary.name)) / "cohort"
        source = finalizer.consumer.extended_path(ROOT / "experiments/codex-transfer-v1")
        shutil.copytree(source, self.root)

    def tearDown(self):
        # Windows ordinary-path cleanup silently misses deep preserved exports.
        shutil.rmtree(finalizer.consumer.extended_path(Path(self.temporary.name)))
        self.temporary.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(self):
        # A read-only check must never repair or overwrite a stale result.
        with mock.patch.object(finalizer, "atomic_json", side_effect=AssertionError("unexpected write")):
            return finalizer.finalize(self.root, check=True)

    def test_check_preserves_bytes_and_keeps_original_failures_and_supplement_separate(self):
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        result = self.check()
        self.assertEqual(result["original"]["planned_trials"], 10)
        self.assertEqual([result["original"][key] for key in ("successes", "failures", "unknown")], [4, 6, 0])
        self.assertEqual(result["supplement"]["planned_trials"], 2)
        self.assertEqual(result["supplement"]["successes"], 2)
        self.assertEqual(result["original_cost"]["unknown_usage_requests"], 4)
        self.assertIsNone(result["original_cost"]["true_total_tokens"])
        self.assertEqual(result["preserved_zero_call_launcher_failures"], 14)
        self.assertTrue(all(pair["differences"]["input_tokens"]["verified_minus_empty"] is None
                            for pair in result["knowledge_pairs"]))
        after = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ValueError, "outputs must be fresh"):
            finalizer.finalize(self.root)

    def test_rewritten_success_or_supplement_denominator_is_rejected_without_repair(self):
        for name, key, value in (("summary.json", "planned_target_trials", 12),
                                  ("supplement-summary.json", "included_in_original_comparison", True)):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                payload = json.loads(original)
                payload[key] = value
                self.write(path, payload)
                tampered = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "saved summary differs"):
                    self.check()
                self.assertEqual(path.read_bytes(), tampered)
                path.write_bytes(original)

    def test_extra_supplement_report_cannot_enter_original_merged_inventory(self):
        extra = self.root / "verification-merged/split-baseline-empty-r3/verification.json"
        extra.parent.mkdir()
        extra.write_bytes((self.root / "verification-split/split-baseline-empty-r3/verification.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "exactly ten reports"):
            self.check()

    def test_even_semantically_identical_reformatted_report_is_not_a_byte_preserving_merge(self):
        path = self.root / "verification-merged/last-baseline-empty-r1/verification.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "original artifact bytes"):
            self.check()

    def test_merge_provenance_and_historical_request_are_recomputed(self):
        path = self.root / "verification-merged/merge-provenance.json"
        original = path.read_bytes()
        payload = json.loads(original)
        payload["additional_trials_merged"] = 2
        self.write(path, payload)
        with self.assertRaisesRegex(ValueError, "merge provenance differs"):
            self.check()
        path.write_bytes(original)
        request_path = self.root / "requests/split-and-supplement.json"
        request = json.loads(request_path.read_bytes())
        request["manifests"].pop(0)
        self.write(request_path, request)
        with self.assertRaisesRegex(ValueError, "complete planned inventory"):
            self.check()

    def test_unlisted_downloaded_artifact_is_rejected(self):
        (self.root / "verification-last/unlisted-result.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "missing or unexpected files"):
            self.check()


if __name__ == "__main__":
    unittest.main()
