"""Durable acquisition receipts recover a lost reply without a second attempt."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from delivery_agent.scheduler import JobStore, SchedulerError


def payload():
    return {"task": {"instance_id": "claim-fixture", "repo": "example/project", "base_commit": "a" * 40,
        "problem_statement": "Trusted scheduler fixture", "image": "python:3.12-slim", "public_checks": []},
        "config": {"model": "fixture"}}


class ClaimReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "queue.sqlite"
        self.now = 100.0
        self.store = JobStore(self.path, lease_seconds=30, clock=lambda: self.now)
        self.store.register("worker")
        self.store.register("other")
        self.store.submit("first", payload())
        self.store.submit("second", payload())

    def test_same_acquisition_survives_restart_and_renews_without_reexecution(self):
        first = self.store.claim("worker", request_id="lost-response")
        self.now += 10
        restarted = JobStore(self.path, lease_seconds=30, clock=lambda: self.now)
        recovered = restarted.claim("worker", request_id="lost-response")
        for key in ("job_id", "payload", "generation", "lease_token", "attempt"):
            self.assertEqual(first[key], recovered[key])
        self.assertEqual(recovered["lease_until"], self.now + 30)
        row = restarted.get("first")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(len(row["attempt_history"]), 1)
        self.assertEqual(row["attempt_history"][0]["started"], 100)

    def test_new_request_and_legacy_claim_do_not_replay_live_execution(self):
        first = self.store.claim("worker", request_id="first-acquisition")
        self.assertIsNone(self.store.claim("worker", request_id="new-acquisition"))
        self.assertIsNone(self.store.claim("worker"))
        self.assertEqual(self.store.get(first["job_id"])["attempts"], 1)
        other = self.store.claim("other", request_id="first-acquisition")
        self.assertEqual(other["job_id"], "second")
        self.assertNotEqual(first["lease_token"], other["lease_token"])

    def test_expired_receipt_never_steals_reassigned_generation(self):
        first = self.store.claim("worker", request_id="lost-response")
        self.now = first["lease_until"] + 1
        self.assertIsNone(self.store.claim("worker", request_id="lost-response"))
        replacement = self.store.claim("other", request_id="replacement")
        self.assertEqual(replacement["job_id"], "first")
        self.assertEqual(replacement["generation"], 2)
        self.assertIsNone(self.store.claim("worker", request_id="lost-response"))
        self.assertEqual(self.store.get("first")["worker_id"], "other")

    def test_terminal_and_failed_completion_cannot_reuse_old_acquisition(self):
        for outcome in ("succeeded", "failed", "cancelled"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                store = JobStore(Path(temporary) / "queue.sqlite")
                store.register("worker")
                store.submit("first", payload())
                store.submit("second", payload())
                first = store.claim("worker", request_id="request")
                if outcome == "cancelled":
                    store.cancel("first")
                else:
                    store.complete("first", "worker", first["lease_token"], first["generation"],
                        {"outcome": outcome, "result": {}, "patch": "", "evidence": {}})
                self.assertIsNone(store.claim("worker", request_id="request"))
                self.assertEqual(store.get("first")["attempts"], 1)
                self.assertEqual(store.get("second")["attempts"], 0)
                self.assertIsNotNone(store.claim("worker", request_id="new-request"))

    def test_concurrent_retries_create_only_one_attempt(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.store.claim("worker", request_id="same-request"), range(4)))
        self.assertEqual({reply["generation"] for reply in replies}, {1})
        self.assertEqual(len({reply["lease_token"] for reply in replies}), 1)
        self.assertEqual(self.store.get("first")["attempts"], 1)

    def test_existing_database_upgrades_without_changing_jobs_or_attempts(self):
        claim = self.store.claim("worker")
        before = self.store.get(claim["job_id"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE claim_requests")
            db.commit()
        upgraded = JobStore(self.path, clock=lambda: self.now)
        self.assertEqual(upgraded.get(claim["job_id"]), before)
        with self.assertRaises(SchedulerError):
            upgraded.claim("other", request_id="bad/id")
        self.assertEqual(upgraded.get("second")["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
