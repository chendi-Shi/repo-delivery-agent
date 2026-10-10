"""Claim transport contracts with deterministic clocks; no executor or network."""
from __future__ import annotations

from http.client import IncompleteRead
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from delivery_agent.scheduler import SchedulerError
from delivery_agent.scheduler_worker import CLAIM_RECOVERY_ATTEMPTS, Client, Worker


TOKEN = "test-claim-token-" + "x" * 40
CLAIM = {"job_id": "one", "generation": 1, "lease_token": "l" * 64,
         "lease_seconds": 30, "payload": {}}


class ClaimClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("http://127.0.0.1:1", TOKEN, timeout=3)

    def test_unknown_response_reuses_id_until_job_then_next_claim_gets_new_id(self):
        requests = []
        replies = iter([TimeoutError("response lost"), IncompleteRead(b""), {"job": CLAIM}, {"job": None}])

        def transport(path, body):
            self.assertEqual(path, "/v1/claim")
            requests.append(body.copy())
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(self.client, "request", side_effect=transport):
            for error in (TimeoutError, IncompleteRead):
                with self.assertRaises(error):
                    self.client.claim("worker")
            self.assertEqual(self.client.claim("worker"), CLAIM)
            self.assertIsNone(self.client.claim("worker"))
        ids = [row["request_id"] for row in requests]
        self.assertEqual(len(set(ids[:3])), 1)
        self.assertNotEqual(ids[2], ids[3])
        self.assertRegex(ids[0], r"^[0-9a-f]{32}$")
        self.assertEqual(self.client._pending_claims, {})

    def test_pending_identity_is_per_worker_and_malformed_response_does_not_clear_it(self):
        requests = []

        def transport(path, body):
            requests.append(body.copy())
            return {} if body["worker_id"] == "first" and len(requests) == 1 else {"job": None}

        with patch.object(self.client, "request", side_effect=transport):
            with self.assertRaises(SchedulerError) as error:
                self.client.claim("first")
            self.assertEqual(error.exception.status, 502)
            self.assertIsNone(self.client.claim("second"))
            self.assertIsNone(self.client.claim("first"))
        self.assertEqual(requests[0]["request_id"], requests[2]["request_id"])
        self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])


class ClaimWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claim-client-")
        self.addCleanup(temporary.cleanup)
        self.client = Client("http://127.0.0.1:1", TOKEN, timeout=3)
        self.worker = Worker(self.client, "worker", Path(temporary.name), poll_seconds=.01,
                             executor=Mock(side_effect=AssertionError("executor must not run in transport test")))
        self.now = 100.0
        self.requests = []

    def run_script(self, replies, *, max_jobs=1):
        events = iter(replies)

        def transport(path, body):
            if path == "/v1/workers/register":
                return {}
            self.assertEqual(path, "/v1/claim")
            self.requests.append((self.now, body.copy()))
            value = next(events)
            if isinstance(value, Exception):
                self.now += 2  # Longer than the load fixture's .6 second idle bound.
                raise value
            return {"job": value}

        def wait(seconds):
            self.now += seconds
            return False

        with (patch.object(self.client, "request", side_effect=transport),
              patch("delivery_agent.scheduler_worker.time.monotonic", side_effect=lambda: self.now),
              patch.object(self.worker.stop, "wait", side_effect=wait),
              patch.object(self.worker, "_execute", return_value={"state": "succeeded"}) as execute):
            self.executed = execute
            return self.worker.run(max_jobs=max_jobs, idle_seconds=.6)

    def test_lost_response_outlasts_idle_but_recovered_claim_executes_once_with_fresh_start(self):
        self.assertEqual(self.run_script([TimeoutError("lost"), CLAIM]), [{"state": "succeeded"}])
        self.executed.assert_called_once_with(CLAIM, self.requests[1][0])
        self.assertGreater(self.requests[1][0] - self.requests[0][0], .6)
        self.assertEqual(self.requests[0][1]["request_id"], self.requests[1][1]["request_id"])

    def test_recovered_none_allows_fresh_request_instead_of_exiting_after_timeout(self):
        self.assertEqual(self.run_script([URLError("lost"), None, CLAIM]), [{"state": "succeeded"}])
        ids = [body["request_id"] for _, body in self.requests]
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[1], ids[2])
        self.executed.assert_called_once_with(CLAIM, self.requests[2][0])

    def test_transport_retry_is_bounded_and_never_runs_executor_on_unknown_claim(self):
        with self.assertRaises(SchedulerError) as error:
            self.run_script([TimeoutError("lost")] * CLAIM_RECOVERY_ATTEMPTS)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(len(self.requests), CLAIM_RECOVERY_ATTEMPTS)
        self.assertEqual(len({body["request_id"] for _, body in self.requests}), 1)
        self.executed.assert_not_called()
        self.worker.executor.assert_not_called()
        # A caller choosing to try later can still resolve exactly that request.
        with patch.object(self.client, "request", return_value={"job": None}) as transport:
            self.assertIsNone(self.client.claim("worker"))
        self.assertEqual(transport.call_args.args[1]["request_id"], self.requests[0][1]["request_id"])

    def test_server_unknown_error_retries_but_definitive_rejection_does_not(self):
        self.run_script([SchedulerError("upstream unavailable", 503), CLAIM])
        self.assertEqual(self.requests[0][1]["request_id"], self.requests[1][1]["request_id"])
        self.requests.clear()
        with self.assertRaises(SchedulerError) as error:
            self.run_script([SchedulerError("unauthorized", 401)])
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(len(self.requests), 1)
        self.executed.assert_not_called()

    def test_definitive_empty_responses_still_reach_the_normal_idle_exit(self):
        self.assertEqual(self.run_script([None] * 100), [])
        self.executed.assert_not_called()
        self.assertLess(len(self.requests), 100)
        self.assertGreaterEqual(self.now - 100, .6)
        self.assertEqual(len({body["request_id"] for _, body in self.requests}), len(self.requests))


if __name__ == "__main__":
    unittest.main()
