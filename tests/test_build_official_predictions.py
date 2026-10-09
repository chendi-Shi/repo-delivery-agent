"""Artifact export contracts with synthetic inputs, never LLM performance evidence.

These fixtures intentionally emulate accepted artifact metadata only. No model,
Docker engine, official dataset, hidden grader or actual benchmark runs are used.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.benchmark import freeze_dataset
from scripts import build_official_predictions as builder
from scripts.grade_experiments import load_plan


class OfficialPredictionBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-builder-contract-")
        self.root = Path(self.temporary.name)
        self.revision = "a" * 40
        self.model = "synthetic-official-contract-model"
        self.tasks = [{"instance_id": f"fixture__project-{number}", "repo": "fixture/project",
                       "base_commit": str(number) * 40,
                       "problem_statement": f"Synthetic public requirement {number}"}
                      for number in (1, 2, 3)]
        raw = self.root / "synthetic-source.json"
        raw.write_text(json.dumps(self.tasks), encoding="utf-8")
        self.frozen = self.root / "frozen"
        freeze_dataset(raw, self.frozen, "synthetic/fixture", self.revision)
        self.source = self.frozen / "source.json"
        self.source.write_text(json.dumps({"dataset": "synthetic/fixture", "revision": self.revision,
            "instance_ids": [task["instance_id"] for task in self.tasks]}), encoding="utf-8")
        self.manifest = self.root / "experiments" / "synthetic-plan.json"
        self.manifest.parent.mkdir()
        self.output = self.root / "export" / "official-predictions.jsonl"
        # Non-ASCII bytes catch accidental str(bytes) export rather than UTF-8 decode.
        self.patch_text = "diff --git a/note.txt b/note.txt\n--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-old\n+新内容\n"
        self.runs = [self.create_run("submitted", self.tasks[0], status="submitted"),
                     self.create_run("failed", self.tasks[1], status="failed", blob=b""),
                     self.create_run("stopped", self.tasks[2], status="stopped", blob=b"partial patch\n")]
        self.write_plan(self.runs)

    def tearDown(self):
        self.temporary.cleanup()

    def create_run(self, name, public_task, *, status, blob=None, profile="retrieval"):
        directory = self.manifest.parent / "model-runs" / name
        directory.mkdir(parents=True)
        blob = self.patch_text.encode("utf-8") if blob is None else blob
        task = {**public_task, "image": "python:3.12-slim", "public_checks": []}
        (directory / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (directory / "model.patch").write_bytes(blob)
        evidence = {"fixture": False, "provider": "ollama_native", "status": status,
                    "exit_status": "Submitted" if status == "submitted" else "StepLimitExceeded",
                    "profile": profile, "model_name": "ollama_chat/synthetic-model",
                    "model_digest": "synthetic-model-digest" if status == "submitted" else None,
                    "patch_sha256": hashlib.sha256(blob).hexdigest(), "usage": {},
                    "seconds": 1.0, "context_requests": 1, "verification_status": "deferred"}
        (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        return {"id": name, "task": f"model-runs/{name}/task.json",
                "patch": f"model-runs/{name}/model.patch",
                "evidence": f"model-runs/{name}/evidence.json"}

    def write_plan(self, runs):
        self.manifest.write_text(json.dumps({"schema": 1, "kind": "real_model_repository_ablation",
            "limitations": ["Synthetic contract fixture; no model or official grading performed."],
            "runs": runs}), encoding="utf-8")

    def read_sidecar(self):
        return json.loads(self.output.with_name(self.output.name + ".attempts.json").read_text(encoding="utf-8"))

    def test_real_load_plan_bytes_are_decoded_into_exact_unicode_prediction(self):
        # Spy rather than replace the parser: real bounded loading and hash checks run.
        self.assertIsInstance(load_plan(self.manifest)[0]["patch"], bytes)
        with patch.object(builder, "load_plan", wraps=load_plan) as parsed:
            summary = builder.build(self.manifest, self.source, self.output, self.model)
        parsed.assert_called_once_with(self.manifest)
        rows = [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows, [{"instance_id": self.tasks[0]["instance_id"],
            "model_name_or_path": self.model, "model_patch": self.patch_text}])
        self.assertFalse(summary["official_grade_performed"])
        self.assertEqual(summary["source_manifest_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(summary["dataset_revision"], self.revision)
        self.assertNotIn("official_resolved", summary)

    def test_all_three_attempts_keep_failed_and_stopped_in_denominator(self):
        summary = builder.build(self.manifest, self.source, self.output, self.model)
        self.assertEqual((summary["attempted_count"], summary["submitted_count"], summary["agent_failure_count"]), (3, 1, 2))
        sidecar = self.read_sidecar()
        self.assertEqual(sidecar["predictions_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual([row["instance_id"] for row in sidecar["attempts"]],
                         [task["instance_id"] for task in self.tasks])
        self.assertEqual([row["status"] for row in sidecar["attempts"]], ["submitted", "failed", "stopped"])
        self.assertEqual([row["submitted"] for row in sidecar["attempts"]], [True, False, False])

    def test_inference_repo_commit_and_problem_must_match_frozen_task(self):
        path = self.manifest.parent / self.runs[0]["task"]
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, altered in (("repo", "other/project"), ("base_commit", "f" * 40),
                               ("problem_statement", "A different public problem")):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: altered}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Inference input differs"):
                    builder.build(self.manifest, self.source, self.output, self.model)
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertFalse(self.output.exists())

    def test_modified_public_manifest_is_rejected_by_frozen_snapshot_hash(self):
        path = self.frozen / "manifest.json"
        frozen = json.loads(path.read_text(encoding="utf-8"))
        frozen["tasks"][0]["problem_statement"] = "Modified after freezing"
        path.write_text(json.dumps(frozen), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_fixture_flag_blocks_entire_export_instead_of_silently_dropping_denominator(self):
        path = self.manifest.parent / self.runs[1]["evidence"]
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["fixture"] = True
        path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid or mismatched"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_tampered_patch_is_rejected_before_predictions_are_written(self):
        path = self.manifest.parent / self.runs[0]["patch"]
        path.write_bytes(path.read_bytes() + b"tampered\n")
        with self.assertRaisesRegex(ValueError, "Invalid or mismatched"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_same_instance_with_distinct_profile_and_run_id_is_rejected(self):
        extra = self.create_run("other-configuration", self.tasks[0], status="submitted", profile="baseline")
        self.write_plan([*self.runs, extra])
        self.assertEqual(len(load_plan(self.manifest)), 4)
        with self.assertRaisesRegex(ValueError, "exactly one real attempt"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_missing_official_attempt_cannot_shrink_selection_denominator(self):
        self.write_plan(self.runs[:2])
        with self.assertRaisesRegex(ValueError, "Every fixed official task"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_source_selection_must_agree_with_all_frozen_task_ids(self):
        source = json.loads(self.source.read_text(encoding="utf-8"))
        source["instance_ids"] = source["instance_ids"][:2]
        self.source.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "selection differs"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_source_revision_and_dataset_must_match_frozen_snapshot(self):
        original = json.loads(self.source.read_text(encoding="utf-8"))
        for field, altered in (("revision", "b" * 40), ("dataset", "different/fixture")):
            with self.subTest(field=field):
                self.source.write_text(json.dumps({**original, field: altered}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "selection differs"):
                    builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())

    def test_duplicate_source_ids_are_rejected_even_when_set_agrees(self):
        source = json.loads(self.source.read_text(encoding="utf-8"))
        source["instance_ids"].append(source["instance_ids"][0])
        self.source.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "selection differs"):
            builder.build(self.manifest, self.source, self.output, self.model)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
