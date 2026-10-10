"""Typed artifact fixtures test aggregation contracts, never model performance."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import summarize_closure_suite as summary


class CohortArtifacts:
    """Unit-only JSON producer, deliberately separate from the live runner."""
    def __init__(self, root):
        self.root = Path(root)
        common = {"provider": "ollama_native", "backend": "docker", "tools_sha256": "a" * 64,
                  "implementation_sha256": "b" * 64, "budgets": {"max_steps": 24, "context_tokens": 24000,
                  "max_output_tokens": 1500, "wall_seconds": 1800, "request_timeout": 300,
                  "command_timeout": 90, "cost_limit": 3., "temperature": 0}}
        task = {"task_sha256": "c" * 64, "task_fingerprint": "d" * 64, "repo": "owner/repository",
                "base_commit": "a" * 40, "inference_image_id": "sha256:" + "e" * 64,
                "public_base_image_id": "sha256:" + "f" * 64,
                "public_checks_sha256": hashlib.sha256(b'["public-check"]').hexdigest()}
        self.plan = {"schema": 1, "kind": "fixed_closure_cohort", "fixture": False,
            "frozen_before_execution": True, "suite_id": "summary-unit-contract", "common": common,
            "knowledge_namespace": "summary-unit-knowledge",
            "model": {"name": "qwen3:4b-instruct", "digest": "2" * 64}, "reference_arm": "baseline",
            "seeds": [42, 43], "arms": [{"id": "baseline", "profile": "baseline", "knowledge": "none"},
                {"id": "retrieval", "profile": "retrieval", "knowledge": "none"},
                {"id": "seeded", "profile": "retrieval", "knowledge": "verified_source"}],
            "targets": [{**task, "instance_id": "target-last", "role": "near_transfer",
                         "arms": ["baseline", "retrieval", "seeded"]},
                        {**task, "instance_id": "target-split", "role": "holdout", "arms": ["baseline", "retrieval"]}],
            "source_attempts": [{"instance_id": "source-first", "seed": 42}]}
        self.plan_path = self.root / "plan.json"
        self.observations_path = self.root / "observations.json"
        self.receipts = {}
        self.sources = {}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        path.write_bytes(raw)
        return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}

    def add(self, identity, seed, arm, *, status="submitted", internal=True, external=True,
            tokens=(100, 20), calls=3, seconds=10., complete=True, infra=False, started=True,
            source=False, patch_invalid=False):
        if source:
            target = {**self.plan["targets"][0], "instance_id": identity, "role": "source"}
            strategy = {"id": arm, "profile": "recent", "knowledge": "none"}
        else:
            target = next(row for row in self.plan["targets"] if row["instance_id"] == identity)
            strategy = next(row for row in self.plan["arms"] if row["id"] == arm)
        submitted = status == "submitted"
        delivered = None if infra else False if not submitted else summary._checks(internal, external)
        directory = ("source-trial/" if source else "cohort-trials/") + f"{identity}/{seed}/{arm}"
        task = {key: target[key] for key in summary.TASK_FIELDS}
        model = copy.deepcopy(self.plan["model"])
        if not started:
            status, submitted, internal, external, delivered = "infrastructure_failed", False, None, None, None
            infra, calls, complete, tokens, seconds = True, 0, False, (None, None), None
            model["digest"] = None
            task["inference_image_id"] = task["public_base_image_id"] = None
        usage = {"complete": complete, "input_tokens": tokens[0], "output_tokens": tokens[1],
                 "calls_with_usage": calls if complete else max(0, calls - 1), "unknown_requests": 0}
        receipt = {"schema": 1, "kind": "closure_trial_receipt", "fixture": False,
            "instance_id": identity, "seed": seed, "arm": arm, "role": target["role"],
            "model": model, "common": copy.deepcopy(self.plan["common"]), "profile": strategy["profile"],
            "knowledge": strategy["knowledge"], "task": task, "status": status,
            "exit_status": "Submitted" if submitted else "LimitsExceeded" if status == "stopped" else None,
            "submitted": submitted, "internal_checks_passed": internal, "external_checks_passed": external,
            "delivery_success": delivered, "usage": usage, "model_calls": calls, "seconds": seconds,
            "infrastructure_failed": infra, "attempt_started": started, "attempted_count": int(started),
            "knowledge_observed_reuse": False, "raw_trajectory": "private raw bytes must never be copied"}
        artifacts = {"summary": self.write(directory + "/summary.json", {"fixture": False,
            "model_status": status, "exit_status": receipt["exit_status"], "delivery_success": delivered,
            "attempt_started": started, "attempted_count": int(started), "model_digest": model["digest"],
            "internal_checks_passed": internal, "external_checks_passed": external, "seconds": seconds,
            "private_field": "secret-output-body"}), "evidence": None, "verification": None}
        if started:
            evidence = {"fixture": False, "provider": self.plan["common"]["provider"],
                "model_digest": model["digest"], "profile": strategy["profile"], "status": status,
                "exit_status": receipt["exit_status"], "implementation_sha256": self.plan["common"]["implementation_sha256"],
                "budgets": {**self.plan["common"]["budgets"], "seed": seed}, "seconds": seconds,
                "model_calls": calls, "usage": copy.deepcopy(usage), "estimated_input_tokens": 99999999,
                "patch_sha256": "e" * 64,
                "internal_verification_status": "passed" if internal is True else "failed" if internal is False else None,
                "internal_public_checks": [] if internal is None else [{"command": "public-check",
                    "returncode": 0 if internal else 1, "passed": internal}],
                "bindings": {"source_task_sha256": task["task_sha256"], "source_task_fingerprint": task["task_fingerprint"],
                             "inference_image_id": task["inference_image_id"], "public_base_image_id": task["public_base_image_id"]}}
            artifacts["evidence"] = self.write(directory + "/evidence.json", evidence)
            checks = [] if patch_invalid else [{"command": "public-check", "returncode": 0 if external else 1,
                                                "passed": bool(external), "output_preview": "private-output-preview"}]
            outcome = "patch_invalid" if patch_invalid else "passed" if external else "failed"
            record = {"instance_id": identity, "model_digest": model["digest"], "submitted": submitted,
                      "verification_status": outcome, "public_checks": checks}
            if not patch_invalid:
                record["resolved_image"] = task["public_base_image_id"]
            verification = {"schema": 1, "runs": [] if infra else [record],
                            "infrastructure_failures": [{"stage": "independent_verification"}] if infra else []}
            artifacts["verification"] = self.write(directory + "/verification.json", verification)
        receipt["artifacts"] = artifacts
        key = (identity, seed, arm)
        ref = self.write(directory + "/receipt.json", receipt)
        if source:
            self.sources[(identity, seed)] = {"instance_id": identity, "seed": seed, "receipt": ref}
        else:
            self.receipts[key] = {"instance_id": identity, "seed": seed, "arm": arm, "receipt": ref}
        return receipt

    def all(self):
        for target in self.plan["targets"]:
            for seed in self.plan["seeds"]:
                for arm in target["arms"]:
                    self.add(target["instance_id"], seed, arm)
        self.add("source-first", 42, "source", source=True)

    def save(self):
        plan_ref = self.write("plan.json", self.plan)
        observations = {"schema": 1, "kind": "closure_cohort_observations", "plan_sha256": plan_ref["sha256"],
                        "runs": list(self.receipts.values()), "source_attempts": list(self.sources.values())}
        self.write("observations.json", observations)
        return observations

    def summarize(self):
        self.save()
        return summary.summarize(self.plan_path, self.observations_path)

    def replace_receipt(self, key, value):
        self.receipts[key]["receipt"] = self.write(self.receipts[key]["receipt"]["path"], value)

    def observed_read(self, key=("target-last", 42, "seeded")):
        """Build unit-only public artifacts for an independently checked source read."""
        observations = self.save()
        source_ref = self.sources[("source-first", 42)]["receipt"]
        source = json.loads((self.root / source_ref["path"]).read_text())
        record = {"title": "source-first: repository delivery", "source_type": "repository_run",
            "source_ref": "run:" + "4" * 64, "repo": source["task"]["repo"],
            "base_commit": source["task"]["base_commit"], "task_id": "source-first", "verified": True,
            "body": "Unit-only source content, not model-performance evidence.",
            "verification": {"kind": "independent_public_checks", "run_status": "submitted", "patch_valid": True,
                "patch_sha256": "e" * 64, "checks": [{"command": "public-check", "returncode": 0, "passed": True}]}}
        namespace_hash = hashlib.sha256(self.plan["knowledge_namespace"].encode()).hexdigest()
        identity = hashlib.sha256(json.dumps([record["source_type"], record["source_ref"], record["title"]],
                                             separators=(",", ":")).encode()).hexdigest()
        origin = {"source_id": "k" + hashlib.sha256((namespace_hash + identity).encode()).hexdigest()[:32],
            "content_sha256": hashlib.sha256(record["body"].encode()).hexdigest(), "task_id": "source-first",
            "namespace_sha256": namespace_hash, "verified": True, "source_ref": record["source_ref"],
            "verification_sha256": hashlib.sha256(json.dumps(record["verification"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
        guard = {"schema": 1, "kind": "source_seed_leakage_guard", "eligible": True, "violations": [],
            "omitted_violations": 0, "functional_targets": [{"instance_id": row["instance_id"],
                "returncode": 1, "target_feature_still_missing": True} for row in self.plan["targets"]]}
        package = {"schema": 1, "kind": "verified_closure_seed_package", "fixture": False, "eligible": True,
            "plan_sha256": observations["plan_sha256"], "source_receipt": source_ref,
            "source_task_sha256": source["task"]["task_sha256"], "origin": origin, "record": record,
            "guard": self.write("source-leakage-guard.json", guard)}
        self.write("seed-package.json", package)
        receipt = json.loads((self.root / self.receipts[key]["receipt"]["path"]).read_text())
        evidence_ref = receipt["artifacts"]["evidence"]
        evidence = json.loads((self.root / evidence_ref["path"]).read_text())
        audit_path = str(Path(evidence_ref["path"]).with_name("action-audit.jsonl")).replace("\\", "/")
        audit = {"event": "action", "outcome": "completed", "returncode": 0,
            "action_kind": "delivery-knowledge/read", "knowledge_operation": "read",
            "knowledge_namespace_sha256": namespace_hash, "knowledge_records": [origin]}
        command = "delivery-knowledge read " + origin["source_id"]
        audit.update(command_preview=command, command_chars=len(command),
                     command_sha256=hashlib.sha256(command.encode()).hexdigest())
        raw = (json.dumps(audit) + "\n").encode()
        (self.root / audit_path).write_bytes(raw)
        evidence["action_audit"] = {"available": True, "complete": True, "actions": 1,
                                     "public_audit_sha256": hashlib.sha256(raw).hexdigest()}
        receipt["artifacts"]["evidence"] = self.write(evidence_ref["path"], evidence)
        receipt["knowledge_observed_reuse"] = True
        self.replace_receipt(key, receipt)
        return package, audit_path, audit, receipt


class ClosureSummaryTests(unittest.TestCase):
    def test_fixed_denominator_separates_sources_and_counts_stopped_and_failed_costs(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("target-last", 42, "retrieval", tokens=(80, 10), calls=2, seconds=8.)
            for seed in (42, 43):
                data.add("target-last", seed, "seeded", status="failed", internal=False, external=False,
                         tokens=(5, 2), calls=1, seconds=1.)
            data.add("target-split", 42, "baseline", internal=False, external=False)
            data.add("target-split", 42, "retrieval", status="stopped", internal=True, external=True)
            result = data.summarize()
            self.assertEqual(result["expected_target_attempts"], 10)
            self.assertEqual(result["source_attempts"]["delivery_success_count"], 1)
            baseline, retrieval, seeded = (result["by_arm"][key] for key in ("baseline", "retrieval", "seeded"))
            self.assertEqual((baseline["expected_attempts"], retrieval["expected_attempts"], seeded["expected_attempts"]), (4, 4, 2))
            self.assertEqual((retrieval["submitted_count"], retrieval["all_check_success_count"], retrieval["delivery_success_count"]), (3, 4, 3))
            self.assertEqual(seeded["status_counts"], {"failed": 2})
            self.assertEqual(baseline["total_actual_input_tokens"], 400)
            self.assertEqual(baseline["actual_provider_tokens_per_delivery_success"], 480 / 3)
            self.assertEqual(retrieval["actual_provider_tokens_per_delivery_success"], 450 / 3)
            self.assertEqual(result["by_role"]["holdout"]["seeded"]["expected_attempts"], 0)
            pair = next(row for row in result["paired"]["pairs"] if row["instance_id"] == "target-last" and
                        row["seed"] == 42 and row["challenger_arm"] == "retrieval")
            self.assertEqual((pair["delta_actual_input_tokens"], pair["delta_actual_output_tokens"],
                              pair["delta_actual_model_calls"], pair["delta_actual_seconds"]), (-20, -10, -1, -2.))
            self.assertTrue(all(row["delta_actual_input_tokens"] is None for row in result["paired"]["pairs"]
                                if row["challenger_arm"] == "seeded"))
            self.assertEqual(result["paired"]["comparisons"]["seeded"]["success_discordance"]["reference_only"], 2)
            self.assertFalse(result["global_improvement_claim"])
            text = json.dumps(result)
            for secret in ("private raw bytes", "secret-output-body", "private-output-preview", "estimated_input_tokens", temporary):
                self.assertNotIn(secret, text)

    def test_missing_row_or_declared_file_remains_unknown_with_fixed_denominator(self):
        for remove_file in (False, True):
            with self.subTest(remove_file=remove_file), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                if remove_file:
                    (data.root / data.receipts[key]["receipt"]["path"]).unlink()
                else:
                    del data.receipts[key]
                result = data.summarize()
                arm = result["by_arm"]["retrieval"]
                self.assertEqual((arm["expected_attempts"], arm["delivery_unknown_count"], arm["observed_attempts"]), (4, 1, 3))
                self.assertIsNone(arm["delivery_success_rate"])
                self.assertIsNone(arm["total_actual_input_tokens"])
                self.assertIsNone(arm["actual_provider_tokens_per_delivery_success"])
                self.assertFalse(result["complete"])
                self.assertEqual(result["paired"]["comparisons"]["retrieval"]["success_discordance"]["unknown"], 1)

    def test_failed_source_blocks_seeded_rows_before_model_without_shrinking_cohort(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("source-first", 42, "source", source=True, status="stopped", internal=False, external=False)
            for seed in (42, 43):
                data.add("target-last", seed, "seeded", started=False)
            result = data.summarize()
            arm = result["by_arm"]["seeded"]
            self.assertEqual(result["expected_target_attempts"], 10)
            self.assertEqual((arm["expected_attempts"], arm["actual_model_attempt_count"], arm["blocked_before_model_count"]), (2, 0, 2))
            self.assertEqual((arm["infrastructure_failure_count"], arm["delivery_unknown_count"]), (2, 2))
            self.assertIsNone(arm["total_actual_input_tokens"])
            self.assertEqual(arm["total_actual_model_calls"], 0)
            self.assertEqual(result["source_attempts"]["delivery_success_count"], 0)

    def test_verifier_infrastructure_failure_preserves_real_submitted_and_actual_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("target-last", 42, "baseline", status="submitted", internal=True, external=None, infra=True)
            result = data.summarize()
            arm = result["by_arm"]["baseline"]
            self.assertEqual(arm["submitted_count"], 4)
            self.assertEqual(arm["infrastructure_failure_count"], 1)
            self.assertEqual(arm["delivery_unknown_count"], 1)
            self.assertEqual(arm["total_actual_input_tokens"], 400)
            self.assertIsNone(arm["delivery_success_rate"])
            self.assertIsNone(arm["actual_provider_tokens_per_delivery_success"])

    def test_incomplete_actual_usage_never_substitutes_estimates_or_claims_efficiency(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("target-last", 42, "retrieval", tokens=(80, 10), complete=False)
            result = data.summarize()
            arm = result["by_arm"]["retrieval"]
            self.assertEqual(arm["token_usage_covered_attempts"], 3)
            self.assertIsNone(arm["total_actual_input_tokens"])
            self.assertIsNone(arm["actual_provider_tokens_per_delivery_success"])
            self.assertEqual(arm["total_actual_model_calls"], 12)
            self.assertIsNone(result["paired"]["comparisons"]["retrieval"]["mean_delta_actual_input_tokens"])
            self.assertEqual(result["paired"]["comparisons"]["retrieval"]["mean_delta_actual_seconds"], 0)

    def test_mixed_weights_runtime_revision_provider_tools_or_budgets_are_rejected(self):
        changes = [("model", "digest", "3" * 64), ("task", "inference_image_id", "sha256:" + "3" * 64),
                   ("task", "public_base_image_id", "sha256:" + "3" * 64), ("task", "base_commit", "3" * 40),
                   ("common", "provider", "litellm"), ("common", "tools_sha256", "3" * 64),
                   ("common", "implementation_sha256", "3" * 64), ("budgets", "max_steps", 25)]
        for group, field, value in changes:
            with self.subTest(group=group, field=field), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                receipt = json.loads((data.root / data.receipts[key]["receipt"]["path"]).read_text())
                (receipt["common"]["budgets"] if group == "budgets" else receipt[group])[field] = value
                data.replace_receipt(key, receipt)
                with self.assertRaises(summary.SummaryError):
                    data.summarize()

    def test_fixture_contradictory_delivery_and_tampered_or_private_paths_are_rejected(self):
        for mode in ("fixture", "delivery", "hash", "escape", "duplicate", "unexpected"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                receipt = json.loads((data.root / data.receipts[key]["receipt"]["path"]).read_text())
                if mode == "fixture":
                    receipt["fixture"] = True
                    data.replace_receipt(key, receipt)
                elif mode == "delivery":
                    receipt["status"], receipt["exit_status"], receipt["submitted"] = "stopped", "LimitsExceeded", False
                    data.replace_receipt(key, receipt)
                elif mode == "hash":
                    (data.root / receipt["artifacts"]["evidence"]["path"]).write_text("{}")
                elif mode == "escape":
                    data.receipts[key]["receipt"]["path"] = "../private-result.json"
                observations = data.save()
                if mode == "duplicate":
                    observations["runs"].append(observations["runs"][0])
                elif mode == "unexpected":
                    observations["runs"][0]["seed"] = 999
                data.write("observations.json", observations)
                with self.assertRaises(summary.SummaryError):
                    summary.summarize(data.plan_path, data.observations_path)

    def test_actual_evidence_and_independent_check_mismatch_cannot_be_hidden_by_receipt_hashes(self):
        for mode in ("evidence_usage", "evidence_seed", "verification_image", "verification_checks"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                receipt = json.loads((data.root / data.receipts[key]["receipt"]["path"]).read_text())
                kind = "evidence" if mode.startswith("evidence") else "verification"
                ref = receipt["artifacts"][kind]
                artifact = json.loads((data.root / ref["path"]).read_text())
                if mode == "evidence_usage":
                    artifact["usage"]["input_tokens"] += 1
                elif mode == "evidence_seed":
                    artifact["budgets"]["seed"] += 1
                elif mode == "verification_image":
                    artifact["runs"][0]["resolved_image"] = "sha256:" + "3" * 64
                else:
                    artifact["runs"][0]["public_checks"] = []
                receipt["artifacts"][kind] = data.write(ref["path"], artifact)
                data.replace_receipt(key, receipt)
                with self.assertRaises(summary.SummaryError):
                    data.summarize()

    def test_invalid_patch_is_a_retained_model_failure_and_output_cannot_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("target-last", 42, "baseline", internal=False, external=False, patch_invalid=True)
            result = data.summarize()
            self.assertEqual(result["by_arm"]["baseline"]["delivery_failure_count"], 1)
            self.assertTrue(result["complete"])
            protected = data.root / data.receipts[("target-last", 42, "baseline")]["receipt"]["path"]
            original = protected.read_bytes()
            with self.assertRaises(summary.SummaryError):
                summary.summarize(data.plan_path, data.observations_path, protected)
            self.assertEqual(protected.read_bytes(), original)
            output = data.root / "cohort-summary.json"
            summary.summarize(data.plan_path, data.observations_path, output)
            self.assertEqual(json.loads(output.read_text())["expected_target_attempts"], 10)

    def test_internal_evidence_and_summary_claims_cannot_contradict_receipt(self):
        changes = [("evidence", "internal_verification_status", "failed"),
                   ("evidence", "internal_public_checks", []),
                   ("evidence", "internal_public_checks", [{"command": "different-check", "returncode": 0, "passed": True}]),
                   ("evidence", "internal_public_checks", [{"command": "public-check", "returncode": 1, "passed": True}]),
                   ("summary", "attempt_started", False), ("summary", "attempted_count", 0),
                   ("summary", "model_status", "stopped"), ("summary", "exit_status", "LimitsExceeded"),
                   ("summary", "model_digest", "9" * 64), ("summary", "delivery_success", False)]
        for kind, field, value in changes:
            with self.subTest(kind=kind, field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                receipt = json.loads((data.root / data.receipts[key]["receipt"]["path"]).read_text())
                ref = receipt["artifacts"][kind]
                artifact = json.loads((data.root / ref["path"]).read_text())
                artifact[field] = value
                receipt["artifacts"][kind] = data.write(ref["path"], artifact)
                data.replace_receipt(key, receipt)
                with self.assertRaises(summary.SummaryError):
                    data.summarize()

    def test_frozen_same_profile_knowledge_contrast_separates_combined_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            data.add("target-last", 42, "baseline", tokens=(50, 10), seconds=5.)
            data.add("target-last", 42, "retrieval", tokens=(200, 30), seconds=20.)
            data.add("target-last", 42, "seeded", tokens=(150, 20), seconds=15.)
            data.add("target-last", 43, "seeded", status="stopped", internal=True, external=True)
            result = data.summarize()
            comparisons = result["paired"]["comparisons"]
            combined, knowledge = comparisons["seeded"], comparisons["retrieval->seeded"]
            self.assertEqual(combined["intervention"], "context_and_knowledge")
            self.assertEqual(combined["mean_delta_actual_input_tokens"], 100)
            self.assertEqual(knowledge["intervention"], "knowledge_only")
            self.assertEqual((knowledge["reference_arm"], knowledge["challenger_arm"]), ("retrieval", "seeded"))
            self.assertEqual((knowledge["expected_pairs"], knowledge["successful_pairs"]), (2, 1))
            self.assertEqual(knowledge["mean_delta_actual_input_tokens"], -50)
            self.assertEqual(knowledge["success_discordance"]["reference_only"], 1)
            self.assertTrue(all(row["role"] == "near_transfer" for row in result["paired"]["pairs"]
                                if row["comparison_id"] == "retrieval->seeded"))
            del data.receipts[("target-last", 42, "seeded")]
            after = data.summarize()["paired"]["comparisons"]
            self.assertEqual(set(after), set(comparisons))
            self.assertEqual(after["retrieval->seeded"]["expected_pairs"], 2)
            self.assertIsNone(after["retrieval->seeded"]["mean_delta_actual_input_tokens"])

    def test_post_submit_infrastructure_failure_retains_raw_exit_without_success_claim(self):
        for infrastructure_failure in (True, False):
            with self.subTest(infrastructure_failure=infrastructure_failure), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                key = ("target-last", 42, "retrieval")
                receipt = data.add(*key, status="failed", internal=True,
                                   external=None if infrastructure_failure else False,
                                   infra=infrastructure_failure)
                receipt["exit_status"] = "Submitted"
                for kind in ("summary", "evidence"):
                    ref = receipt["artifacts"][kind]
                    artifact = json.loads((data.root / ref["path"]).read_text())
                    artifact["exit_status"] = "Submitted"
                    receipt["artifacts"][kind] = data.write(ref["path"], artifact)
                data.replace_receipt(key, receipt)
                if not infrastructure_failure:
                    with self.assertRaises(summary.SummaryError):
                        data.summarize()
                    continue
                result = data.summarize()
                row = next(row for row in result["target_attempts"]
                           if (row["instance_id"], row["seed"], row["arm"]) == key)
                self.assertEqual(row["status"], "failed")
                self.assertFalse(row["submitted"])
                self.assertTrue(row["infrastructure_failed"])
                self.assertIsNone(row["delivery_success"])
                self.assertIsNone(row["all_checks_passed"])
                self.assertEqual(row["actual_input_tokens"], 100)
                self.assertFalse(result["complete"])
                preserved = json.loads((data.root / row["receipt"]["path"]).read_text())
                self.assertEqual(preserved["exit_status"], "Submitted")

    def test_positive_source_read_binds_audit_seed_and_delivered_source_without_causal_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            _, audit_path, _, _ = data.observed_read()
            result = data.summarize()
            stats = result["by_arm"]["seeded"]
            self.assertEqual(stats["knowledge_observed_read_count"], 1)
            self.assertEqual(stats["knowledge_observed_reuse_count"], 1)
            row = next(row for row in result["target_attempts"] if row["knowledge_observed_read"])
            self.assertEqual(set(row["knowledge_read_evidence"]),
                             {"action_audit", "seed_package", "source_receipt", "source_guard"})
            self.assertIn("no claim", result["knowledge_metric_scope"])
            self.assertIn("deprecated aliases", result["knowledge_metric_scope"])
            self.assertNotIn("Unit-only source content", json.dumps(result))
            original = (data.root / audit_path).read_bytes()
            with self.assertRaises(summary.SummaryError):
                summary.summarize(data.plan_path, data.observations_path, data.root / audit_path)
            self.assertEqual((data.root / audit_path).read_bytes(), original)

    def test_positive_source_read_rejects_false_flag_tampered_origin_audit_or_source_chain(self):
        for mode in ("search_only", "audit_hash", "audit_origin", "empty_read", "truncated_command", "origin_content", "origin_verification",
                     "wrong_plan", "failed_source", "guard_gap", "source_patch"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                package, audit_path, audit, receipt = data.observed_read()
                if mode in {"search_only", "audit_origin", "empty_read", "truncated_command"}:
                    if mode == "search_only":
                        audit["action_kind"], audit["knowledge_operation"] = "delivery-knowledge/search", "search"
                    elif mode == "audit_origin":
                        audit["knowledge_records"][0]["task_id"] = "target-last"
                    elif mode == "empty_read":
                        command = audit["command_preview"] + " " + str(len(package["record"]["body"]))
                        audit.update(command_preview=command, command_chars=len(command),
                                     command_sha256=hashlib.sha256(command.encode()).hexdigest())
                    else:
                        audit["command_chars"] += 1
                    raw = (json.dumps(audit) + "\n").encode()
                    (data.root / audit_path).write_bytes(raw)
                    ref = receipt["artifacts"]["evidence"]
                    evidence = json.loads((data.root / ref["path"]).read_text())
                    evidence["action_audit"]["public_audit_sha256"] = hashlib.sha256(raw).hexdigest()
                    receipt["artifacts"]["evidence"] = data.write(ref["path"], evidence)
                    data.replace_receipt(("target-last", 42, "seeded"), receipt)
                elif mode == "audit_hash":
                    (data.root / audit_path).write_bytes(b"{}\n")
                elif mode == "origin_content":
                    package["origin"]["content_sha256"] = "9" * 64
                elif mode == "origin_verification":
                    package["origin"]["verification_sha256"] = "9" * 64
                elif mode == "wrong_plan":
                    package["plan_sha256"] = "9" * 64
                elif mode == "failed_source":
                    data.add("source-first", 42, "source", source=True, status="stopped", internal=False, external=False)
                    package["source_receipt"] = data.sources[("source-first", 42)]["receipt"]
                elif mode == "guard_gap":
                    ref = package["guard"]
                    guard = json.loads((data.root / ref["path"]).read_text())
                    guard["functional_targets"][0]["returncode"] = 0
                    package["guard"] = data.write(ref["path"], guard)
                else:
                    package["record"]["verification"]["patch_sha256"] = "9" * 64
                    package["origin"]["verification_sha256"] = hashlib.sha256(json.dumps(
                        package["record"]["verification"], ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                data.write("seed-package.json", package)
                with self.assertRaises(summary.SummaryError):
                    data.summarize()

    def test_positive_flag_without_source_artifacts_stays_unknown_and_never_counts_a_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            key = ("target-last", 42, "seeded")
            receipt = json.loads((data.root / data.receipts[key]["receipt"]["path"]).read_text())
            receipt["knowledge_observed_reuse"] = True
            data.replace_receipt(key, receipt)
            result = data.summarize()
            self.assertEqual(result["expected_target_attempts"], 10)
            self.assertEqual(result["by_arm"]["seeded"]["knowledge_observed_read_count"], 0)
            self.assertEqual(result["by_arm"]["seeded"]["delivery_unknown_count"], 1)
            self.assertFalse(result["complete"])

    def test_source_request_timeout_preserves_measured_time_and_unknown_response_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = CohortArtifacts(temporary)
            data.all()
            receipt = data.add("source-first", 42, "source", source=True, status="failed",
                internal=None, external=None, tokens=(None, None), calls=0, seconds=300.949730128,
                complete=False, infra=True, started=True)
            receipt["model_calls"] = None
            receipt["artifacts"]["evidence"] = receipt["artifacts"]["verification"] = None
            ref = receipt["artifacts"]["summary"]
            actual_summary = json.loads((data.root / ref["path"]).read_text())
            actual_summary.update(observed_provider_requests=1, model_calls=None, usage=receipt["usage"])
            receipt["artifacts"]["summary"] = data.write(ref["path"], actual_summary)
            source_ref = data.sources[("source-first", 42)]["receipt"]
            data.sources[("source-first", 42)]["receipt"] = data.write(source_ref["path"], receipt)
            result = data.summarize()
            self.assertEqual(result["expected_target_attempts"], 10)
            sources = result["source_attempts"]
            self.assertEqual(sources["actual_model_attempt_count"], 1)
            source = sources["attempts"][0]
            self.assertEqual(source["observed_provider_requests"], 1)
            self.assertEqual(source["actual_seconds"], 300.949730128)
            self.assertFalse(source["usage_complete"])
            for field in ("delivery_success", "actual_model_calls", "actual_input_tokens", "actual_output_tokens"):
                self.assertIsNone(source[field])

    def test_provider_request_counter_is_bounded_and_cannot_contradict_start(self):
        for count in (-1, True, 0, 10 ** 15 + 1):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                data = CohortArtifacts(temporary)
                data.all()
                source_ref = data.sources[("source-first", 42)]["receipt"]
                receipt = json.loads((data.root / source_ref["path"]).read_text())
                ref = receipt["artifacts"]["summary"]
                actual_summary = json.loads((data.root / ref["path"]).read_text())
                actual_summary["observed_provider_requests"] = count
                receipt["artifacts"]["summary"] = data.write(ref["path"], actual_summary)
                data.sources[("source-first", 42)]["receipt"] = data.write(source_ref["path"], receipt)
                with self.assertRaises(summary.SummaryError):
                    data.summarize()


if __name__ == "__main__":
    unittest.main()
