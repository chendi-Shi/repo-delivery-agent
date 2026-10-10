"""Real HTTP response loss after SQLite commit, using the production Worker."""
from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.scheduler import JobStore
from delivery_agent.scheduler_server import Coordinator, CoordinatorHandler
from delivery_agent.scheduler_worker import Client, Worker


OBSERVATIONS = []


def exercise_loss(mode):
    """Faulty transport; production queue/client/worker with a trusted fixture executor."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = JobStore(root / "jobs.sqlite", lease_seconds=30)
        token = "claim-recovery-fixture-" + "x" * 40
        server = Coordinator(("127.0.0.1", 0), store, token, request_timeout=2)
        client = Client(f"http://127.0.0.1:{server.server_port}", token, timeout=1)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        requests, claims, executed, dropped = [], [], [], []
        original_body, original_reply = CoordinatorHandler._body, CoordinatorHandler._reply

        def disconnect(handler, kind):
            dropped.append(kind)
            handler.connection.shutdown(socket.SHUT_RDWR)
            raise BrokenPipeError("controlled HTTP transport fault")

        def body(handler):
            value = original_body(handler)
            if handler.path == "/v1/claim":
                requests.append(value.get("request_id"))
            if mode == "uncommitted_completion" and handler.path == "/v1/jobs/one/complete":
                # Body is parsed, but dispatch has not called store.complete.
                disconnect(handler, "completion_before_commit")
            return value

        def reply(handler, status, value):
            if status == 200 and handler.path == "/v1/claim" and value.get("job"):
                claim = value["job"]
                claims.append((claim["job_id"], claim["generation"], claim["lease_token"]))
                if mode == "claim" and not dropped:
                    # _dispatch returned only after the real transaction committed.
                    disconnect(handler, "claim_after_commit")
            if (status == 200 and mode == "committed_completion"
                    and handler.path == "/v1/jobs/one/complete"):
                disconnect(handler, "completion_after_commit")
            return original_reply(handler, status, value)

        def executor(claim, directory, cancelled, options):
            executed.append((claim["job_id"], claim["generation"]))
            return {"outcome": "succeeded", "result": {"fixture": True, "llm_calls": 0},
                    "patch": "", "evidence": {}}

        with patch.object(CoordinatorHandler, "_body", body), patch.object(CoordinatorHandler, "_reply", reply):
            thread.start()
            try:
                names = ["one", "two"] if mode == "committed_completion" else ["one"]
                for name in names:
                    client.submit(name, {"task": {"instance_id": name, "repo": "example/project",
                        "base_commit": "a" * 40, "problem_statement": "Trusted HTTP transport fixture",
                        "image": "python:3.12-slim", "public_checks": []},
                        "config": {"model": "fixture", "profile": "baseline"}}, max_attempts=2)
                commits = Worker(client, "worker", root / "attempts", executor=executor,
                                 poll_seconds=.02).run(max_jobs=len(names), idle_seconds=.3)
                # A fresh logical request after commit_unknown must not reclaim
                # the same still-active lease and replay its executor.
                retry = client.claim("worker") if mode == "uncommitted_completion" else None
                rows = [client.request("/v1/jobs/" + name) for name in names]
                observation = {"mode": mode, "fixture": True, "llm_calls": 0,
                    "dropped_replies_or_requests": dropped,
                    "claim_request_count": len(requests),
                    "claim_request_ids_present": all(isinstance(item, str) and item for item in requests),
                    "first_two_claim_requests_same_id": len(requests) >= 2 and requests[0] == requests[1],
                    "same_claim_token_and_generation_recovered": len(claims) >= 2 and claims[0] == claims[1],
                    "executor_invocations": [{"job_id": job, "generation": generation} for job, generation in executed],
                    "worker_result_states": [item["state"] for item in commits],
                    "fresh_request_after_unknown_returned_none": retry is None,
                    "rows": [{key: row[key] for key in ("job_id", "state", "attempts", "generation", "attempt_history")}
                             for row in rows]}
                OBSERVATIONS.append(observation)
                return observation
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)


class ClaimRecoveryHTTPTests(unittest.TestCase):
    def test_lost_claim_reply_recovers_committed_lease_without_second_attempt(self):
        result = exercise_loss("claim")
        self.assertEqual(result["dropped_replies_or_requests"], ["claim_after_commit"])
        self.assertEqual(result["claim_request_count"], 2)
        self.assertTrue(result["claim_request_ids_present"])
        self.assertTrue(result["first_two_claim_requests_same_id"])
        self.assertTrue(result["same_claim_token_and_generation_recovered"])
        self.assertEqual(result["executor_invocations"], [{"job_id": "one", "generation": 1}])
        self.assertEqual(result["worker_result_states"], ["succeeded"])
        self.assertEqual(result["rows"][0]["attempts"], 1)
        self.assertEqual([item["outcome"] for item in result["rows"][0]["attempt_history"]], ["succeeded"])

    def test_lost_committed_completion_replies_do_not_replay_on_next_claim(self):
        result = exercise_loss("committed_completion")
        self.assertEqual(result["dropped_replies_or_requests"], ["completion_after_commit"] * 3)
        self.assertTrue(result["claim_request_ids_present"])
        self.assertFalse(result["first_two_claim_requests_same_id"])
        self.assertEqual(result["executor_invocations"],
                         [{"job_id": "one", "generation": 1}, {"job_id": "two", "generation": 1}])
        self.assertEqual(result["worker_result_states"], ["commit_unknown", "succeeded"])
        self.assertEqual([(row["state"], row["attempts"]) for row in result["rows"]],
                         [("succeeded", 1), ("succeeded", 1)])

    def test_uncommitted_unknown_does_not_reclaim_active_lease_with_fresh_id(self):
        result = exercise_loss("uncommitted_completion")
        self.assertEqual(result["dropped_replies_or_requests"], ["completion_before_commit"] * 3)
        self.assertTrue(result["claim_request_ids_present"])
        self.assertFalse(result["first_two_claim_requests_same_id"])
        self.assertTrue(result["fresh_request_after_unknown_returned_none"])
        self.assertEqual(result["executor_invocations"], [{"job_id": "one", "generation": 1}])
        self.assertEqual(result["worker_result_states"], ["commit_unknown"])
        self.assertEqual((result["rows"][0]["state"], result["rows"][0]["attempts"]), ("leased", 1))


if __name__ == "__main__":
    unittest.main()
