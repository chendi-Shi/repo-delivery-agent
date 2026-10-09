"""Collector preserves failed attempts and rejects missing/colliding provenance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("merge_live_experiments", ROOT / "scripts/merge_live_experiments.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shards = self.root / "shards"
        self.shards.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def shard(self, name, run_id, *, status="submitted", profile="retrieval", digest="a" * 64):
        root = self.shards / name
        target = root / "model-runs" / run_id
        target.mkdir(parents=True)
        task = {"instance_id": "public-task", "repo": "example/project", "base_commit": "a" * 40,
                "problem_statement": "Public requirement", "image": "python:3.12-slim", "public_checks": []}
        patch = b"actual recorded patch"
        evidence = {"fixture": False, "provider": "ollama_native", "status": status,
            "exit_status": "Submitted" if status == "submitted" else "ModelError", "profile": profile,
            "model_name": "ollama_chat/qwen2.5-coder:1.5b", "model_digest": digest,
            "patch_sha256": hashlib.sha256(patch).hexdigest(), "usage": {}, "seconds": 1,
            "context_requests": 1, "verification_status": "deferred"}
        (target / "task.json").write_text(json.dumps(task))
        (target / "evidence.json").write_text(json.dumps(evidence))
        (target / "model.patch").write_bytes(patch)
        (root / "ci-environment.json").write_text(json.dumps({"model_digest": digest}))
        (root / "raw-private-trace.txt").write_text("must never be copied")
        (root / "repository-ablation.json").write_text(json.dumps({"schema": 1,
            "kind": "real_model_repository_ablation", "limitations": ["Small exploratory sample"],
            "runs": [{"id": run_id, "task": f"model-runs/{run_id}/task.json",
                "patch": f"model-runs/{run_id}/model.patch", "evidence": f"model-runs/{run_id}/evidence.json"}]}))
        return root

    def test_merge_retains_actual_failed_attempt_and_omits_unknown_files(self):
        self.shard("one", "run-baseline", profile="baseline")
        self.shard("two", "run-retrieval", status="failed", digest=None)
        output = self.root / "public"
        result = collector.merge(self.shards, output, expected_runs=2)
        self.assertEqual(result["attempts"], 2)
        plan = json.loads((output / "repository-ablation.json").read_text())
        self.assertEqual(len(plan["runs"]), 2)
        evidence = json.loads((output / "model-runs/run-retrieval/evidence.json").read_text())
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(plan["limitations"], ["Small exploratory sample"])
        self.assertFalse(any(p.name == "raw-private-trace.txt" for p in output.rglob("*")))
        self.assertEqual((output / "model-runs/run-baseline/model.patch").read_bytes(), b"actual recorded patch")

    def test_missing_shards_fail_instead_of_reducing_denominator(self):
        self.shard("one", "run-one")
        with self.assertRaises(ValueError):
            collector.merge(self.shards, self.root / "public", expected_runs=2)

    def test_duplicate_run_identity_rejected(self):
        self.shard("one", "same-run")
        self.shard("two", "same-run")
        with self.assertRaises(ValueError):
            collector.merge(self.shards, self.root / "public", expected_runs=2)

    def test_different_model_digest_rejected(self):
        self.shard("one", "run-one")
        self.shard("two", "run-two", digest="b" * 64)
        with self.assertRaises(ValueError):
            collector.merge(self.shards, self.root / "public", expected_runs=2)

    def test_existing_output_is_immutable(self):
        self.shard("one", "run-one")
        output = self.root / "public"
        output.mkdir()
        with self.assertRaises(ValueError):
            collector.merge(self.shards, output, expected_runs=1)


if __name__ == "__main__":
    unittest.main()
