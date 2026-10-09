"""Transactional contracts plus two independent worker processes over real HTTP."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from delivery_agent.scheduler import JobStore, SchedulerError, validate_completion, validate_payload
from delivery_agent.scheduler_server import Coordinator, MAX_REQUEST_BYTES
from delivery_agent.scheduler_worker import Client, Worker, evidence_manifest, execute_repository, result_summary

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "scheduler-test-bearer-" + "x" * 40


def payload(instance="example-1"):
    return {"task": {"instance_id": instance, "repo": "example/project", "base_commit": "a" * 40,
        "problem_statement": "Fix a public repository task", "image": "python:3.12-slim", "public_checks": []},
        "config": {"model": "ollama_chat/model", "profile": "retrieval"}}


def completion(outcome="succeeded", value=1):
    return {"outcome": outcome, "result": {"value": value}, "patch": "patch\n", "evidence": {"trace.json": "b" * 64}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = JobStore(Path(self.tmp.name) / "jobs.sqlite", lease_seconds=2, clock=lambda: self.now)
        self.store.register("first")
        self.store.register("second")

    def tearDown(self):
        self.tmp.cleanup()

    def call_complete(self, claim, worker="first", body=None):
        return self.store.complete(claim["job_id"], worker, claim["lease_token"], claim["generation"], body or completion())

    def test_submission_is_immutable_and_idempotent(self):
        self.assertFalse(self.store.submit("one", payload())["idempotent"])
        self.assertTrue(self.store.submit("one", payload())["idempotent"])
        with self.assertRaises(SchedulerError):
            self.store.submit("one", payload("different"))
        with self.assertRaises(SchedulerError):
            self.store.submit("one", payload(), max_attempts=4)

    def test_atomic_claim_has_unique_generation_and_one_lease_per_worker(self):
        for name in ("one", "two"):
            self.store.submit(name, payload(name))
        claim = self.store.claim("first")
        self.assertEqual(claim["generation"], 1)
        self.assertIsNone(self.store.claim("first"))
        other = self.store.claim("second")
        self.assertNotEqual(claim["job_id"], other["job_id"])

    def test_unregistered_worker_cannot_claim(self):
        with self.assertRaises(SchedulerError):
            self.store.claim("unregistered")

    def test_heartbeat_extends_live_lease(self):
        self.store.submit("one", payload())
        claim = self.store.claim("first")
        self.now += 1
        renewed = self.store.heartbeat("one", "first", claim["lease_token"], claim["generation"])
        self.assertEqual(renewed["lease_until"], 1003.0)
        self.now = 1002.1
        self.assertIsNone(self.store.claim("second"))

    def test_expiry_retry_fences_old_result_and_recovers_after_restart(self):
        self.store.submit("one", payload(), max_attempts=2)
        old = self.store.claim("first")
        self.now += 2.1
        restarted = JobStore(self.store.path, lease_seconds=2, clock=lambda: self.now)
        newer = restarted.claim("second")
        self.assertEqual(newer["generation"], 2)
        self.assertNotEqual(old["lease_token"], newer["lease_token"])
        with self.assertRaises(SchedulerError):
            self.call_complete(old)
        restarted.complete("one", "second", newer["lease_token"], 2, completion())
        self.assertEqual(restarted.get("one")["attempt_history"][0]["outcome"], "lease_expired")

    def test_expired_worker_cannot_commit_without_reclaimer(self):
        self.store.submit("one", payload())
        claim = self.store.claim("first")
        self.now += 2.1
        with self.assertRaises(SchedulerError):
            self.call_complete(claim)
        with self.assertRaises(SchedulerError):
            self.store.heartbeat("one", "first", claim["lease_token"], 1)

    def test_retries_have_hard_bound(self):
        self.store.submit("one", payload(), max_attempts=2)
        for attempt in range(2):
            claim = self.store.claim("first")
            self.assertEqual(claim["attempt"], attempt + 1)
            self.call_complete(claim, body=completion("failed", attempt))
        self.assertIsNone(self.store.claim("second"))
        row = self.store.get("one")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(len(row["attempt_history"]), 2)

    def test_lease_expirations_exhaust_attempts(self):
        self.store.submit("one", payload(), max_attempts=1)
        self.store.claim("first")
        self.now += 3
        self.assertIsNone(self.store.claim("second"))
        self.assertEqual(self.store.get("one")["state"], "failed")

    def test_identical_commit_idempotent_different_commit_rejected(self):
        self.store.submit("one", payload())
        claim = self.store.claim("first")
        self.assertFalse(self.call_complete(claim)["idempotent"])
        self.assertTrue(self.call_complete(claim)["idempotent"])
        with self.assertRaises(SchedulerError):
            self.call_complete(claim, body=completion(value=2))
        self.assertEqual(self.store.get("one")["state"], "succeeded")

    def test_failed_commit_idempotent_until_next_generation(self):
        self.store.submit("one", payload())
        first = self.store.claim("first")
        self.call_complete(first, body=completion("failed"))
        self.assertTrue(self.call_complete(first, body=completion("failed"))["idempotent"])
        self.store.claim("second")
        with self.assertRaises(SchedulerError):
            self.call_complete(first, body=completion("failed"))

    def test_cancellation_revokes_lease_and_never_requeues(self):
        self.store.submit("one", payload())
        claim = self.store.claim("first")
        self.store.cancel("one")
        self.store.cancel("one")
        self.now += 10
        self.assertIsNone(self.store.claim("second"))
        with self.assertRaises(SchedulerError):
            self.call_complete(claim)
        self.assertEqual(self.store.get("one")["attempt_history"][0]["outcome"], "cancelled")

    def test_queued_cancellation_and_terminal_cancellation(self):
        self.store.submit("one", payload())
        self.store.cancel("one")
        self.assertIsNone(self.store.claim("first"))
        self.store.submit("two", payload())
        self.call_complete(self.store.claim("first"))
        with self.assertRaises(SchedulerError):
            self.store.cancel("two")

    def test_get_and_list_do_not_reveal_lease_token(self):
        self.store.submit("one", payload())
        claim = self.store.claim("first")
        self.assertNotIn(claim["lease_token"], json.dumps(self.store.get("one")))
        self.assertNotIn(claim["lease_token"], json.dumps(self.store.list_jobs()))

    def test_unknown_payload_fields_endpoints_paths_commands_rejected(self):
        for field in ("api_base", "output", "host_command", "skills"):
            value = payload()
            value["config"][field] = "arbitrary"
            with self.assertRaises(SchedulerError):
                validate_payload(value)
        value = payload()
        value["task"]["patch"] = "gold answer"
        with self.assertRaises(SchedulerError):
            validate_payload(value)

    def test_nonfinite_and_boolean_budgets_rejected(self):
        for field, value in (("cost_limit", float("nan")), ("max_steps", True), ("context_tokens", 1024)):
            candidate = payload()
            candidate["config"][field] = value
            with self.assertRaises(SchedulerError):
                validate_payload(candidate)

    def test_normalized_payload_can_be_validated_again_by_real_executor(self):
        candidate = payload()
        candidate["task"]["public_checks"] = ["python -m unittest"]
        normalized = validate_payload(candidate)
        self.assertEqual(validate_payload(normalized), normalized)

    def test_provider_and_backend_are_allowed_but_local_resources_are_not(self):
        candidate = payload()
        candidate["config"].update(provider="ollama_native", backend="files", ollama_mode="tools")
        self.assertEqual(validate_payload(candidate)["config"]["backend"], "files")
        for field in ("knowledge_db", "embedding_model", "skills"):
            invalid = copy.deepcopy(candidate)
            invalid["config"][field] = "remote-selected-local-resource"
            with self.assertRaises(SchedulerError):
                validate_payload(invalid)

    def test_evidence_and_patch_digest_are_validated(self):
        validated = validate_completion(completion())
        self.assertEqual(validated["patch_sha256"], hashlib.sha256(b"patch\n").hexdigest())
        bad = completion()
        bad["evidence"] = {"../secret": "b" * 64}
        with self.assertRaises(SchedulerError):
            validate_completion(bad)
        bad = completion()
        bad["evidence"] = {"trace.json": "not-a-digest"}
        with self.assertRaises(SchedulerError):
            validate_completion(bad)


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "jobs.sqlite", lease_seconds=5)
        self.server = Coordinator(("127.0.0.1", 0), self.store, TOKEN, request_timeout=3)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = Client(self.url, TOKEN, timeout=3)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def child(self, worker_id, *, mode="run", jobs=1, delay=0.1):
        env = {**os.environ, "DELIVERY_SCHEDULER_TOKEN": TOKEN, "PYTHONPATH": str(ROOT)}
        destination = self.root / worker_id
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/support_scheduler_worker.py"),
            "--url", self.url, "--root", str(destination), "--id", worker_id, "--mode", mode,
            "--jobs", str(jobs), "--delay", str(delay)], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(process)
        return process, destination

    def finish(self, process, timeout=60):
        stdout, stderr = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, (stdout + stderr).decode("utf-8", "replace"))

    def await_file(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"Timed out waiting for fixture artifact {path.name}")

    def test_two_independent_processes_claim_unique_jobs_and_heartbeat(self):
        for n in range(6):
            self.client.submit(f"job-{n}", payload(f"instance-{n}"))
        first, root_a = self.child("worker-a", jobs=3, delay=6)
        second, root_b = self.child("worker-b", jobs=3, delay=6)
        self.finish(first)
        self.finish(second)
        rows = self.client.request("/v1/jobs")["jobs"]
        self.assertEqual({r["state"] for r in rows}, {"succeeded"})
        self.assertEqual({r["attempts"] for r in rows}, {1})
        pids = set()
        for row in rows:
            full = self.client.request("/v1/jobs/" + row["job_id"])
            pids.add(full["result"]["result"]["pid"])
            self.assertEqual(full["result"]["evidence"]["trace.json"], "a" * 64)
            self.assertEqual(full["result"]["patch_sha256"], hashlib.sha256(full["result"]["patch"].encode()).hexdigest())
        self.assertEqual(len(pids), 2)
        self.assertEqual(len(json.loads((root_a / "commits.json").read_text())), 3)
        self.assertEqual(len(json.loads((root_b / "commits.json").read_text())), 3)

    def test_crashed_process_is_retried_and_stale_completion_rejected(self):
        self.store.lease_seconds = 2
        self.client.submit("crash-job", payload(), max_attempts=2)
        crashed, crashed_root = self.child("crashed-worker", mode="crash")
        self.finish(crashed)
        old = json.loads((crashed_root / "crashed-claim.json").read_text())
        time.sleep(2.2)
        retry, _ = self.child("retry-worker")
        self.finish(retry)
        row = self.client.request("/v1/jobs/crash-job")
        self.assertEqual((row["state"], row["attempts"]), ("succeeded", 2))
        self.assertEqual(row["attempt_history"][0]["outcome"], "lease_expired")
        with self.assertRaises(SchedulerError) as caught:
            self.client.complete(old, "crashed-worker", completion())
        self.assertEqual(caught.exception.status, 409)

    def test_live_process_observes_cancellation_and_cannot_commit(self):
        self.client.submit("cancel-job", payload())
        process, root = self.child("cancel-worker", mode="cancel", delay=10)
        self.await_file(root / "cancel-job.started")
        self.client.request("/v1/jobs/cancel-job/cancel", {})
        self.finish(process)
        self.assertTrue((root / "cancel-job.cancelled").exists())
        commits = json.loads((root / "commits.json").read_text())
        self.assertEqual(commits[0]["state"], "lease_lost")
        self.assertIsNone(self.client.request("/v1/jobs/cancel-job")["result"])

    def test_http_idempotency_and_bearer_not_persisted(self):
        self.client.register("manual")
        self.client.submit("one", payload())
        claim = self.client.claim("manual")
        self.assertFalse(self.client.complete(claim, "manual", completion())["idempotent"])
        self.assertTrue(self.client.complete(claim, "manual", completion())["idempotent"])
        for path in self.root.glob("jobs.sqlite*"):
            self.assertNotIn(TOKEN.encode(), path.read_bytes())

    def test_authentication_required_for_reads_and_writes(self):
        for path in ("/health", "/v1/jobs"):
            request = Request(self.url + path)
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
        wrong = Client(self.url, "z" * 40)
        with self.assertRaises(SchedulerError) as caught:
            wrong.submit("one", payload())
        self.assertEqual(caught.exception.status, 401)

    def test_oversize_request_rejected_before_body_read(self):
        request = Request(self.url + "/v1/jobs", data=b"{}", headers={"Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BYTES + 1)})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 413)
        caught.exception.close()

    def test_malformed_fields_and_nonfinite_json_rejected(self):
        with self.assertRaises(SchedulerError) as caught:
            self.client.request("/v1/claim", {"worker_id": "manual", "execute": "evil"})
        self.assertEqual(caught.exception.status, 400)
        request = Request(self.url + "/v1/jobs", data=b'{"max_attempts":NaN}',
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_coordinator_restart_preserves_completion_and_attempts(self):
        self.client.register("manual")
        self.client.submit("one", payload())
        claim = self.client.claim("manual")
        self.client.complete(claim, "manual", completion())
        restarted = JobStore(self.root / "jobs.sqlite")
        self.assertEqual(restarted.get("one")["state"], "succeeded")
        self.assertEqual(len(restarted.get("one")["attempt_history"]), 1)


class WorkerArtifactTests(unittest.TestCase):
    def test_files_backend_closes_source_session_and_refuses_post_cancel_writes(self):
        started, cancelled = threading.Event(), threading.Event()
        observed = {}

        def run(task, args, directory, *, session_factory):
            directory.mkdir()
            session = session_factory(directory, "deferred")
            observed["session"] = session
            session.execute({"command": "delivery-file write original.py <<'DELIVERY'\nvalue = 1\nDELIVERY"})
            started.set()
            deadline = time.monotonic() + 5
            while not session.closed and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(RuntimeError):
                session.execute({"command": "delivery-file write original.py <<'DELIVERY'\nvalue = 2\nDELIVERY"})
            self.assertEqual((directory / "original.py").read_text(), "value = 1\n")
            return {"status": "failed", "model_patch": "", "provenance": {"image_resolution": "deferred"}}

        job = {"payload": payload()}
        job["payload"]["config"].update(backend="files", provider="ollama_native")
        box = {}
        with tempfile.TemporaryDirectory() as directory, patch("delivery_agent.cli.run_repository", run):
            runner = threading.Thread(target=lambda: box.update(result=execute_repository(
                job, Path(directory) / "trial", cancelled)), daemon=True)
            runner.start()
            self.assertTrue(started.wait(5))
            cancelled.set()
            runner.join(timeout=5)
            self.assertFalse(runner.is_alive())
            self.assertTrue(observed["session"].closed)
            self.assertEqual(box["result"]["outcome"], "failed")

    def test_default_executor_cancellation_closes_owned_session_and_blocks_commands(self):
        started, cancelled = threading.Event(), threading.Event()
        observed = {}

        class ControlledSession:
            def __init__(self, *args, **kwargs):
                self.closed = False
                observed["session"] = self

            def execute(self, *args, **kwargs):
                started.set()
                deadline = time.monotonic() + 5
                while not self.closed and time.monotonic() < deadline:
                    time.sleep(0.01)
                return {"output": "", "returncode": 1}

            def close(self):
                self.closed = True

        def run(task, args, directory, *, session_factory):
            directory.mkdir()
            session = session_factory(directory, "image")
            session.execute({"command": "container-command"})
            with self.assertRaises(RuntimeError):
                session.execute({"command": "second-command"})
            return {"status": "failed", "model_patch": "", "provenance": {"resolved_image": "sha256:fixed"}}

        box = {}
        with tempfile.TemporaryDirectory() as directory:
            with patch("delivery_agent.runtime.DockerSession", ControlledSession), patch("delivery_agent.cli.run_repository", run):
                runner = threading.Thread(target=lambda: box.update(result=execute_repository(
                    {"payload": payload()}, Path(directory) / "trial", cancelled)), daemon=True)
                runner.start()
                self.assertTrue(started.wait(5))
                cancelled.set()
                runner.join(timeout=5)
                self.assertFalse(runner.is_alive())
                self.assertTrue(observed["session"].closed)
                self.assertEqual(box["result"]["outcome"], "failed")

    def test_default_executor_constructor_race_closes_new_container(self):
        cancelled = threading.Event()
        observed = {}

        class ControlledSession:
            def __init__(self, *args, **kwargs):
                observed["session"] = self
                self.closed = False
                cancelled.set()  # lease lost while constructor was in progress

            def close(self):
                self.closed = True

        def run(task, args, directory, *, session_factory):
            return session_factory(directory, "image")

        with tempfile.TemporaryDirectory() as directory:
            with patch("delivery_agent.runtime.DockerSession", ControlledSession), patch("delivery_agent.cli.run_repository", run):
                with self.assertRaises(RuntimeError):
                    execute_repository({"payload": payload()}, Path(directory) / "trial", cancelled)
        self.assertTrue(observed["session"].closed)

    def test_summary_omits_provider_output_and_local_endpoints(self):
        summary = result_summary({"status": "submitted", "model_patch": "long", "provider_trace": "secret",
            "provenance": {"resolved_image": "sha256:a", "api_base": "http://secret", "model_digest": "digest"},
            "public_checks": [{"passed": True, "returncode": 0, "output": "raw private text"}]})
        encoded = json.dumps(summary)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private text", encoded)
        self.assertEqual(summary["provenance"]["model_digest"], "digest")

    def test_manifest_only_hashes_known_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "spec.json").write_text("{}", encoding="utf-8")
            (root / "unknown-secrets.txt").write_text("secret", encoding="utf-8")
            self.assertEqual(evidence_manifest(root), {"spec.json": hashlib.sha256(b"{}").hexdigest()})

    def test_client_url_and_configuration_boundaries(self):
        for url in ("http://user:pass@localhost", "file:///tmp/x", "http://host/path", "http://host/?q=1"):
            with self.assertRaises(ValueError):
                Client(url, TOKEN)
        with self.assertRaises(ValueError):
            Coordinator(("127.0.0.1", 0), object(), "short")


if __name__ == "__main__":
    unittest.main()
