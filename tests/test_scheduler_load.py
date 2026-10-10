"""Real subprocess/HTTP fault acceptance plus negative evidence-consumer contracts."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from scripts.scheduler_load import (DeadlineClient, ExecutionDeadlineExceeded, FAULT_STATES,
                                    load_metrics, public_row, run_validation, verify_rows)

ROOT = Path(__file__).resolve().parents[1]


class SchedulerLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="scheduler-load-contract-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name) / "report.json"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/scheduler_load.py"),
            "--jobs", "24", "--workers", "3", "--lease-seconds", "3", "--timeout-seconds", "90",
            "--output", str(cls.output)], cwd=ROOT, capture_output=True, text=True, timeout=110)
        if result.returncode:
            evidence = cls.output.read_text(encoding="utf-8") if cls.output.exists() else result.stdout + result.stderr
            raise AssertionError(evidence)
        cls.report = json.loads(cls.output.read_text(encoding="utf-8"))
        cls.rows_path = cls.output.with_name("report.jobs.json")
        cls.rows = json.loads(cls.rows_path.read_text(encoding="utf-8"))
        cls.load_ids = [f"load-{n:06d}" for n in range(24)]
        cls.expected = {**dict.fromkeys(cls.load_ids, "succeeded"), **FAULT_STATES}

    def test_actual_http_processes_account_for_every_planned_job(self):
        self.assertEqual(self.report["status"], "passed")
        self.assertEqual(self.report["planned_jobs"], 29)
        self.assertEqual(self.report["unverified_planned_jobs"], 0)
        self.assertEqual(len(self.rows), 29)
        self.assertEqual(set(row["job_id"] for row in self.rows), set(self.expected))
        self.assertEqual(Counter(row["state"] for row in self.rows), {"succeeded": 27, "cancelled": 2})
        self.assertEqual(len(set(self.report["load_worker_pids"])), 3)
        self.assertEqual(self.report["load"]["observed_worker_processes"], 3)
        self.assertEqual(self.report["load"]["acknowledged_jobs"], 24)
        self.assertEqual(self.report["load"]["duplicate_job_acknowledgements"], 0)
        self.assertEqual(self.report["verification"]["accepted_successful_attempts"], 27)
        self.assertEqual(self.report["verification"]["duplicate_accepted_jobs"], 0)
        self.assertEqual(self.report["database_audit"]["stored_jobs"], 29)
        self.assertEqual(self.report["database_audit"]["accepted_successful_attempts"], 27)
        self.assertEqual(self.report["database_audit"]["unexpected_job_ids"], [])
        self.assertTrue(all(sum(a["outcome"] == "succeeded" for a in row["attempt_history"]) <= 1 for row in self.rows))

    def test_forced_kill_reassigns_and_rejects_old_generation_without_double_acceptance(self):
        fault = self.report["faults"]["killed_worker"]
        self.assertNotEqual(fault["force_kill"]["returncode"], 0)
        self.assertEqual(fault["force_kill"]["method"], "Popen.kill")
        self.assertEqual((fault["old_generation"], fault["new_generation"]), (1, 2))
        self.assertEqual(fault["stale_completion_http_status"], 409)
        self.assertEqual(fault["stale_probe_new_generation_state"], "leased")
        self.assertTrue(fault["stale_probe_preserved_live_replacement"])
        self.assertEqual(fault["conflicting_completion_http_status"], 409)
        self.assertTrue(fault["exact_repeat_idempotent"])
        self.assertTrue(fault["replacement_acknowledged"])
        row = next(row for row in self.rows if row["job_id"] == "fault-killed")
        self.assertEqual([a["outcome"] for a in row["attempt_history"]], ["lease_expired", "succeeded"])
        self.assertNotEqual(row["attempt_history"][0]["worker_id"], row["attempt_history"][1]["worker_id"])

    def test_real_coordinator_restart_and_running_cancellation(self):
        restart = self.report["faults"]["coordinator_restart"]
        self.assertNotEqual(restart["force_kill"]["returncode"], 0)
        self.assertNotEqual(restart["force_kill"]["pid"], restart["replacement_pid"])
        self.assertEqual(restart["preserved_states"], ["succeeded", "leased", "queued"])
        self.assertTrue(restart["same_database"] and restart["same_http_origin"] and restart["live_worker_continued"])
        cancelled = self.report["faults"]["cancellation"]
        self.assertTrue(cancelled["executor_observed_cancellation"])
        self.assertEqual(cancelled["late_completion_http_status"], 409)
        for row in self.rows:
            if row["state"] == "cancelled":
                self.assertIsNone(row["result"])

    def test_measured_latency_and_evidence_have_explicit_fixture_scope(self):
        load = self.report["load"]
        self.assertTrue(load["complete"])
        self.assertGreater(load["throughput_jobs_per_second"], 0)
        self.assertAlmostEqual(load["throughput_jobs_per_second"], 24 / load["active_lease_window_seconds"])
        for name in ("submit_to_commit", "claim_to_commit"):
            self.assertEqual(load[name]["samples"], 24)
            self.assertGreaterEqual(load[name]["p95_seconds"], load[name]["p50_seconds"])
        self.assertTrue(self.report["fixture"])
        self.assertEqual((self.report["llm_calls"], self.report["physical_hosts"]), (0, 1))
        self.assertEqual(self.report["readback_artifact"]["sha256"], hashlib.sha256(self.rows_path.read_bytes()).hexdigest())
        serialized = self.output.read_text(encoding="utf-8") + self.rows_path.read_text(encoding="utf-8")
        self.assertNotIn("lease_token", serialized)
        self.assertNotIn("Bearer ", serialized)
        self.assertNotIn(self.temporary.name, serialized)
        raw = copy.deepcopy(self.rows[0])
        raw.update(lease_token="private-lease", payload={"host_path": "private-path"}, extra="private-extra")
        raw["result"]["result"]["provider_trace"] = "private-trace"
        cleaned = json.dumps(public_row(raw))
        self.assertNotIn("private-", cleaned)

    def test_missing_or_duplicated_readback_cannot_be_a_complete_success(self):
        missing = verify_rows(self.expected, self.rows[1:])
        self.assertFalse(missing["passed"])
        self.assertEqual(missing["missing_jobs"], 1)
        self.assertEqual(missing["planned_jobs"], 29)
        duplicate = verify_rows(self.expected, self.rows + [self.rows[0]])
        self.assertFalse(duplicate["passed"])
        self.assertIn("duplicate_readback:" + self.rows[0]["job_id"], duplicate["errors"])

    def test_double_accepted_history_and_nonfixture_result_are_rejected(self):
        duplicated = copy.deepcopy(self.rows)
        duplicated[0]["attempt_history"].append(copy.deepcopy(duplicated[0]["attempt_history"][0]))
        duplicated[0]["attempt_history"][-1]["generation"] = 2
        duplicated[0]["attempts"] = duplicated[0]["generation"] = 2
        verdict = verify_rows(self.expected, duplicated)
        self.assertFalse(verdict["passed"])
        self.assertIn("duplicate_accepted_attempt:" + duplicated[0]["job_id"], verdict["errors"])
        foreign = copy.deepcopy(self.rows)
        foreign[0]["result"]["result"]["llm_calls"] = 1
        self.assertIn("result_binding:" + foreign[0]["job_id"], verify_rows(self.expected, foreign)["errors"])

    def test_incomplete_load_never_reports_full_workload_throughput(self):
        partial = load_metrics(self.rows[1:], self.load_ids, [], 3)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["planned_jobs"], 24)
        self.assertEqual(partial["successful_jobs"], 23)
        self.assertEqual(partial["submit_to_commit"]["samples"], 23)
        self.assertIsNone(partial["throughput_jobs_per_second"])
        self.assertFalse(partial["acknowledgements_match_plan"])

    def test_existing_evidence_is_not_overwritten_and_configuration_is_bounded(self):
        original = self.output.read_bytes()
        repeated = subprocess.run([sys.executable, str(ROOT / "scripts/scheduler_load.py"),
            "--output", str(self.output)], cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(repeated.returncode, 2)
        self.assertEqual(self.output.read_bytes(), original)
        for options in ({"workers": 0}, {"jobs": 1, "workers": 2}, {"jobs": 10001},
                        {"lease_seconds": 1}, {"timeout_seconds": 901}, {"fixture_delay": float("nan")}):
            with self.assertRaises(ValueError):
                run_validation(**options)

    def test_parent_http_deadline_prevents_slow_bulk_operations_exceeding_budget(self):
        client = DeadlineClient("http://127.0.0.1:1", "test-token-" + "x" * 40, deadline=101, timeout=3)
        observed = []
        def transport(*args, **kwargs):
            observed.append(client.timeout)
            return {"status": "ok"}
        with patch("scripts.scheduler_load.time.monotonic", side_effect=[100, 101.001]), \
                patch("delivery_agent.scheduler_worker.Client.request", side_effect=transport):
            with self.assertRaises(ExecutionDeadlineExceeded):
                client.request("/health")
        self.assertEqual(observed, [1])
        self.assertEqual(client.timeout, 3)
        with patch("scripts.scheduler_load.time.monotonic", return_value=102), \
                patch("delivery_agent.scheduler_worker.Client.request") as transport:
            with self.assertRaises(ExecutionDeadlineExceeded):
                client.submit("expired", {})
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
