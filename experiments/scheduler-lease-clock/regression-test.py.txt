"""Lease decisions use time after the real SQLite write lock is acquired."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.scheduler import JobStore, SchedulerError


class TransactionClockTests(unittest.TestCase):
    def blocked_operation(self, operation):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            store = JobStore(Path(directory) / "jobs.sqlite", lease_seconds=2, clock=lambda: now[0])
            store.register("worker")
            store.submit("job", {"task": {"instance_id": "clock-fixture", "repo": "example/project",
                "base_commit": "a" * 40, "problem_statement": "Trusted scheduling fixture",
                "image": "python:3.12-slim", "public_checks": []},
                "config": {"model": "fixture", "profile": "baseline"}})
            claim = store.claim("worker") if operation != "claim" else None
            blocker = store._connect()
            blocker.execute("BEGIN IMMEDIATE")
            attempted = threading.Event()
            original_connect = store._connect

            def traced_connect():
                connection = original_connect()
                # This callback runs before BEGIN attempts the lock still held
                # by blocker. It does not depend on when clock() is sampled.
                connection.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
                return connection

            outcome = {}

            def invoke():
                try:
                    if operation == "claim":
                        outcome["result"] = store.claim("worker")
                    elif operation == "heartbeat":
                        outcome["result"] = store.heartbeat("job", "worker", claim["lease_token"], claim["generation"])
                    else:
                        outcome["result"] = store.complete("job", "worker", claim["lease_token"], claim["generation"],
                            {"outcome": "succeeded", "result": {}, "patch": "", "evidence": {}})
                except BaseException as exc:
                    outcome["error"] = exc

            with patch.object(store, "_connect", traced_connect):
                thread = threading.Thread(target=invoke, daemon=True)
                thread.start()
                try:
                    self.assertTrue(attempted.wait(5), "Operation did not reach BEGIN IMMEDIATE")
                    now[0] = 1003.0
                finally:
                    blocker.commit()
                    blocker.close()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "Operation remained blocked after write lock release")
            return outcome

    def test_claim_starts_lease_after_waiting_for_write_lock(self):
        outcome = self.blocked_operation("claim")
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["lease_until"], 1005.0)

    def test_heartbeat_rejects_lease_expired_during_write_lock_wait(self):
        outcome = self.blocked_operation("heartbeat")
        self.assertNotIn("result", outcome)
        self.assertIsInstance(outcome.get("error"), SchedulerError)
        self.assertEqual(outcome["error"].status, 409)

    def test_completion_rejects_lease_expired_during_write_lock_wait(self):
        outcome = self.blocked_operation("complete")
        self.assertNotIn("result", outcome)
        self.assertIsInstance(outcome.get("error"), SchedulerError)
        self.assertEqual(outcome["error"].status, 409)


if __name__ == "__main__":
    unittest.main()
