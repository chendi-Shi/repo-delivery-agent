"""Bounded real HTTP/SQLite scheduler validation; fixture work, zero LLM calls.

Example: python scripts/scheduler_load.py --jobs 1000 --workers 8 --output runs/scheduler-load/report.json
The coordinator and workers are separate local processes. Only the trusted
executor is a fixture; Client, Worker, Coordinator and JobStore are production
implementations. This is neither a multi-machine nor a model performance test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.scheduler import SchedulerError
from delivery_agent.scheduler_worker import Client, Worker

TOKEN_ENV = "DELIVERY_SCHEDULER_TOKEN"
FAULT_STATES = {
    "fault-killed": "succeeded",
    "fault-cancel-queued": "cancelled",
    "fault-cancel-running": "cancelled",
    "fault-restart-leased": "succeeded",
    "fault-restart-queued": "succeeded",
}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                          encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def payload(job_id):
    return {"task": {"instance_id": job_id, "repo": "fixture/scheduler-load",
        "base_commit": "a" * 40, "problem_statement": "Trusted scheduler fixture; no repository or model execution.",
        "image": "python:3.12-slim", "public_checks": []},
        "config": {"model": "fixture/no-model", "profile": "baseline"}}


def fixture_completion(claim, worker_id):
    result = {"fixture": True, "llm_calls": 0, "job_id": claim["job_id"],
              "generation": claim["generation"], "worker_id": worker_id, "pid": os.getpid()}
    return {"outcome": "succeeded", "result": result, "patch": "", "evidence": {}}


def percentile(values, fraction):
    """Nearest-rank percentile; no interpolation or invented missing samples."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def statistics(values):
    return {"samples": len(values), "p50_seconds": percentile(values, .5),
            "p95_seconds": percentile(values, .95), "max_seconds": max(values) if values else None}


def public_row(row):
    """Allowlist fixture evidence; HTTP credentials and ephemeral paths cannot escape."""
    result = {key: row[key] for key in ("job_id", "state", "attempts", "generation", "worker_id",
        "created", "updated", "payload_digest", "result_digest")}
    result["attempt_history"] = [{key: attempt[key] for key in
        ("generation", "worker_id", "started", "ended", "outcome", "result_digest")}
        for attempt in row["attempt_history"]]
    result["result"] = None
    if row["result"] is not None:
        completion = row["result"]
        result["result"] = {key: completion[key] for key in ("outcome", "patch", "patch_sha256", "evidence")}
        result["result"]["result"] = {key: completion["result"].get(key) for key in
            ("fixture", "llm_calls", "job_id", "generation", "worker_id", "pid")}
    return result


def database_audit(path, expected):
    """Read-only inventory after owned processes exit; all mutations used HTTP APIs."""
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        jobs = connection.execute("SELECT job_id,state FROM jobs").fetchall()
        duplicates = connection.execute("SELECT job_id FROM attempts WHERE outcome='succeeded' "
                                        "GROUP BY job_id HAVING count(*)>1").fetchall()
        accepted = connection.execute("SELECT count(*) FROM attempts WHERE outcome='succeeded'").fetchone()[0]
        seen = dict(jobs)
        return {"read_only_after_process_shutdown": True, "stored_jobs": len(jobs),
                "missing_job_ids": sorted(set(expected) - set(seen)),
                "unexpected_job_ids": sorted(set(seen) - set(expected)),
                "wrong_state_job_ids": sorted(key for key in expected.keys() & seen.keys() if expected[key] != seen[key]),
                "duplicate_accepted_job_ids": [row[0] for row in duplicates],
                "accepted_successful_attempts": accepted, "states": dict(Counter(seen.values()))}
    finally:
        connection.close()


def verify_rows(expected, rows):
    """Independent readback oracle over production HTTP records, not worker exit codes."""
    by_id = {}
    errors = []
    for row in rows:
        job_id = row["job_id"]
        if job_id in by_id:
            errors.append(f"duplicate_readback:{job_id}")
        by_id[job_id] = row
    for job_id in sorted(set(expected) - set(by_id)):
        errors.append(f"missing:{job_id}")
    for job_id in sorted(set(by_id) - set(expected)):
        errors.append(f"unexpected:{job_id}")
    successful_attempts = 0
    for job_id, row in by_id.items():
        if job_id not in expected:
            continue
        state = expected[job_id]
        history = row["attempt_history"]
        successes = [a for a in history if a["outcome"] == "succeeded"]
        successful_attempts += len(successes)
        if row["state"] != state:
            errors.append(f"wrong_state:{job_id}")
        if len(successes) > 1:
            errors.append(f"duplicate_accepted_attempt:{job_id}")
        if row["attempts"] != len(history) or row["generation"] != len(history):
            errors.append(f"attempt_accounting:{job_id}")
        if [a["generation"] for a in history] != list(range(1, len(history) + 1)):
            errors.append(f"generation_sequence:{job_id}")
        wanted_history = (["lease_expired", "succeeded"] if job_id == "fault-killed" else
                          [] if job_id == "fault-cancel-queued" else
                          ["cancelled"] if state == "cancelled" else ["succeeded"])
        if [a["outcome"] for a in history] != wanted_history:
            errors.append(f"unexpected_attempt_outcomes:{job_id}")
        if state == "succeeded":
            completion = row.get("result") or {}
            result = completion.get("result", {})
            if (len(successes) != 1 or completion.get("outcome") != "succeeded"
                    or result.get("fixture") is not True or result.get("llm_calls") != 0
                    or result.get("job_id") != job_id or result.get("generation") != row["generation"]
                    or result.get("worker_id") != row["worker_id"] or type(result.get("pid")) is not int):
                errors.append(f"result_binding:{job_id}")
            encoded = json.dumps(completion, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if row.get("result_digest") != digest or any(a["result_digest"] != digest for a in successes):
                errors.append(f"result_digest:{job_id}")
            if completion.get("patch") != "" or completion.get("patch_sha256") != hashlib.sha256(b"").hexdigest():
                errors.append(f"fixture_patch:{job_id}")
        elif row.get("result") is not None or successes:
            errors.append(f"cancelled_result:{job_id}")
    return {"passed": not errors, "planned_jobs": len(expected), "observed_jobs": len(by_id),
            "missing_jobs": len(set(expected) - set(by_id)),
            "duplicate_accepted_jobs": sum(sum(a["outcome"] == "succeeded" for a in r["attempt_history"]) > 1
                                           for r in rows),
            "accepted_successful_attempts": successful_attempts,
            "states": dict(Counter(r["state"] for r in rows)), "errors": errors}


def load_metrics(rows, load_ids, commits, worker_count):
    selected = [row for row in rows if row["job_id"] in load_ids]
    full = len(selected) == len(load_ids) and all(row["state"] == "succeeded" for row in selected)
    successes = [r for r in selected if r["state"] == "succeeded"]
    end_to_end, leased = [], []
    starts, ends = [], []
    for row in successes:
        attempt = row["attempt_history"][-1]
        if attempt["ended"] is None or attempt["ended"] < attempt["started"] or attempt["ended"] < row["created"]:
            raise ValueError("non_monotonic_coordinator_wall_clock")
        end_to_end.append(attempt["ended"] - row["created"])
        leased.append(attempt["ended"] - attempt["started"])
        starts.append(attempt["started"])
        ends.append(attempt["ended"])
    window = max(ends) - min(starts) if starts else None
    pids = {r["result"]["result"]["pid"] for r in successes}
    acknowledged = [c["job_id"] for c in commits if c.get("state") == "succeeded"]
    ack_counts = Counter(acknowledged)
    return {"complete": full, "planned_jobs": len(load_ids), "successful_jobs": len(successes),
            "requested_worker_processes": worker_count, "observed_worker_processes": len(pids),
            "active_lease_window_seconds": window,
            "throughput_jobs_per_second": len(successes) / window if full and window and window > 0 else None,
            "throughput_scope": "First load lease start to last load commit; includes first-action barrier, excludes enqueue/startup/readback/idle shutdown.",
            "submit_to_commit": statistics(end_to_end), "claim_to_commit": statistics(leased),
            "latency_scope": "Coordinator wall timestamps; submit latency includes queueing and worker startup. Nearest-rank percentiles.",
            "acknowledged_jobs": len(ack_counts),
            "duplicate_job_acknowledgements": sum(n > 1 for n in ack_counts.values()),
            "acknowledgements_match_plan": set(ack_counts) == set(load_ids) and all(n == 1 for n in ack_counts.values()),
            "idempotent_completion_responses": sum(c.get("idempotent") is True for c in commits)}


def provenance():
    paths = ["scripts/scheduler_load.py", "delivery_agent/scheduler.py", "delivery_agent/scheduler_server.py",
             "delivery_agent/scheduler_worker.py", "delivery_agent/repository.py"]
    result = {"source_commit": os.environ.get("GITHUB_SHA"),
              "source_files_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths},
              "source_files_clean": None, "python": platform.python_version(),
              "platform": platform.system(), "machine": platform.machine(), "logical_cpu_count": os.cpu_count(),
              "github_run_id": os.environ.get("GITHUB_RUN_ID"), "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}
    git = shutil.which("git")
    if git:
        head = subprocess.run([git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        status = subprocess.run([git, "status", "--porcelain", "--", *paths], cwd=ROOT,
                                capture_output=True, text=True, timeout=5)
        if head.returncode == 0:
            result["source_commit"] = head.stdout.strip()
        if status.returncode == 0:
            result["source_files_clean"] = not status.stdout.strip()
    return result


class ExecutionDeadlineExceeded(TimeoutError):
    pass


class DeadlineClient(Client):
    """Bound every parent request, including bulk submit/readback, by one deadline."""
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def request(self, path, value=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutionDeadlineExceeded()
        original_timeout = self.timeout
        self.timeout = min(original_timeout, remaining)
        try:
            result = super().request(path, value)
        finally:
            self.timeout = original_timeout
        if time.monotonic() >= self.deadline:
            raise ExecutionDeadlineExceeded()
        return result


class Harness:
    def __init__(self, root, *, lease_seconds, timeout):
        self.root = root
        self.env = {**os.environ, TOKEN_ENV: secrets.token_urlsafe(48), "PYTHONPATH": str(ROOT)}
        self.lease_seconds = lease_seconds
        self.deadline = time.monotonic() + timeout
        self.children = []
        self.coordinator = None
        self.port = 0
        self.starts = 0
        self.client = None

    def require(self, condition, code):
        if not condition:
            raise RuntimeError(code)

    def wait(self, predicate, code):
        while time.monotonic() < self.deadline:
            if predicate():
                return
            time.sleep(.02)
        raise TimeoutError(code)

    def spawn(self, args, stdout=subprocess.DEVNULL):
        child = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.children.append(child)
        return child

    def start_coordinator(self):
        self.starts += 1
        destination = self.root / f"coordinator-{self.starts}.jsonl"
        with destination.open("wb") as stream:
            self.coordinator = self.spawn(["-m", "delivery_agent.scheduler_server", "--db",
                str(self.root / "jobs.sqlite"), "--port", str(self.port),
                "--lease-seconds", str(self.lease_seconds)], stdout=stream)

        def listening():
            self.require(self.coordinator.poll() is None, "coordinator_exited_before_listening")
            try:
                value = read_json(destination)
            except (OSError, ValueError):
                return False
            self.port = value["port"]
            return True

        self.wait(listening, "coordinator_start_timeout")
        self.client = DeadlineClient(f"http://127.0.0.1:{self.port}", self.env[TOKEN_ENV], timeout=3,
                                     deadline=self.deadline)
        self.require(self.client.request("/health")["status"] == "ok", "unhealthy_coordinator")

    def worker(self, worker_id, *, jobs, delay, gated=False, hold=False, retain=False):
        directory = self.root / worker_id
        directory.mkdir()
        args = [str(Path(__file__).resolve()), "--child-worker", "--url", self.client.url,
                "--directory", str(directory), "--worker-id", worker_id, "--jobs", str(jobs),
                "--fixture-delay", str(delay), "--timeout-seconds", str(max(1, self.deadline - time.monotonic()))]
        for active, flag in ((gated, "--gated"), (hold, "--hold"), (retain, "--retain")):
            if active:
                args.append(flag)
        return self.spawn(args), directory

    def finish(self, child, directory):
        self.wait(lambda: child.poll() is not None, "worker_finish_timeout")
        self.require(child.returncode == 0, "worker_process_failed")
        result = read_json(directory / "worker.json")
        self.require(result["status"] == "completed", "worker_report_failed")
        return result

    def started(self, child, directory):
        def ready():
            self.require(child.poll() is None, "worker_exited_before_action")
            return (directory / "started.json").exists()
        self.wait(ready, "worker_action_start_timeout")
        return read_json(directory / "started.json")

    def kill(self, process):
        self.require(process.poll() is None, "process_not_alive_before_force_kill")
        process.kill()
        process.wait(timeout=10)
        self.require(process.returncode != 0, "force_kill_not_observed")
        return {"pid": process.pid, "returncode": process.returncode, "method": "Popen.kill"}

    def rejected(self, claim, worker_id, completion):
        try:
            self.client.complete(claim, worker_id, completion)
        except SchedulerError as exc:
            self.require(exc.status == 409, "unexpected_completion_rejection")
            return exc.status
        raise RuntimeError("invalid_completion_was_accepted")

    def close(self):
        for process in reversed(self.children):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def child_worker(args):
    directory = Path(args.directory)
    deadline = time.monotonic() + args.timeout_seconds

    class ObservedClient(Client):
        def register(self, worker_id):
            value = super().register(worker_id)
            write_json(directory / "ready.json", {"pid": os.getpid(), "worker_id": worker_id})
            return value

        def claim(self, worker_id):
            while args.gated and not (directory.parent / "load-start").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("worker_start_barrier_timeout")
                time.sleep(.01)
            return super().claim(worker_id)

        def complete(self, claim, worker_id, completion):
            if args.retain:
                # Ephemeral harness control data only; never exported as evidence.
                write_json(directory / "completion-private.json", {"claim": claim, "completion": completion})
            return super().complete(claim, worker_id, completion)

    client = ObservedClient(args.url, os.environ[TOKEN_ENV], timeout=3)
    first = True

    def executor(claim, work_root, cancelled, local_options):
        nonlocal first
        if first:
            if args.retain:
                write_json(directory / "claim-private.json", claim)
            write_json(directory / "started.json", {"pid": os.getpid(), "job_id": claim["job_id"],
                       "generation": claim["generation"]})
            first = False
        while ((args.hold and not (directory / "release").exists()) or
               (args.gated and not (directory.parent / "load-execute").exists())):
            if cancelled.wait(.01):
                write_json(directory / "cancel-observed.json", {"cancelled": True})
                return {"outcome": "failed", "result": {"fixture": True, "llm_calls": 0, "cancelled": True},
                        "patch": "", "evidence": {}}
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture_hold_timeout")
        if cancelled.wait(args.fixture_delay):
            return {"outcome": "failed", "result": {"fixture": True, "llm_calls": 0}, "patch": "", "evidence": {}}
        return fixture_completion(claim, args.worker_id)

    try:
        worker = Worker(client, args.worker_id, directory / "attempts", executor=executor, poll_seconds=.02)
        commits = worker.run(max_jobs=args.jobs, idle_seconds=.6)
        write_json(directory / "worker.json", {"status": "completed", "pid": os.getpid(), "commits": commits})
        return 0
    except Exception as exc:
        write_json(directory / "worker.json", {"status": "failed", "error_type": type(exc).__name__})
        return 1


def run_validation(*, jobs=1000, workers=8, fixture_delay=.005, lease_seconds=5, timeout_seconds=300):
    if (type(jobs) is not int or type(workers) is not int or not 1 <= workers <= 16 or not workers <= jobs <= 10000
            or not 0 <= fixture_delay <= 1 or not 2 <= lease_seconds <= 30 or not 10 <= timeout_seconds <= 900):
        raise ValueError("Require workers 1..16, jobs workers..10000, delay 0..1, lease 2..30, timeout 10..900")
    load_ids = [f"load-{n:06d}" for n in range(jobs)]
    expected = {**dict.fromkeys(load_ids, "succeeded"), **FAULT_STATES}
    report = {"schema": 1, "kind": "real_http_scheduler_load_and_fault_validation", "fixture": True,
              "llm_calls": 0, "physical_hosts": 1, "status": "failed", "provenance": provenance(),
              "configuration": {"load_jobs": jobs, "additional_fault_jobs": len(FAULT_STATES), "workers": workers,
                  "fixture_delay_seconds": fixture_delay, "lease_seconds": lease_seconds,
                  "timeout_seconds": timeout_seconds, "timeout_scope": "Global execution deadline including parent HTTP; excludes bounded process cleanup and read-only final audit.",
                  "transport": "authenticated loopback HTTP", "storage": "SQLite WAL"},
              "planned_jobs": len(expected), "stage": "startup", "faults": {}, "verification": None,
              "limits": ["One local host, separate OS processes, no cross-machine/network-partition proof.",
                  "Fixture executor does not invoke a model, clone repositories, run Docker, or prove code delivery quality.",
                  "Lease expiry permits repeated execution; verification checks accepted commits, not exactly-once execution.",
                  "Finite workload and measured host only; no unbounded production capacity claim."]}
    rows = []
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="delivery-scheduler-load-") as temporary:
        harness = Harness(Path(temporary), lease_seconds=lease_seconds, timeout=timeout_seconds)
        try:
            harness.start_coordinator()
            client = harness.client
            report["stage"] = "load"
            for job_id in load_ids:
                harness.require(client.submit(job_id, payload(job_id), max_attempts=2)["idempotent"] is False,
                                "initial_submission_not_unique")
            processes = [harness.worker(f"load-worker-{n}", jobs=jobs, delay=fixture_delay, gated=True)
                         for n in range(workers)]
            harness.wait(lambda: all((directory / "ready.json").exists() for _, directory in processes),
                         "workers_register_timeout")
            (harness.root / "load-start").touch()
            first_claims = [harness.started(child, directory) for child, directory in processes]
            harness.require(len({c["job_id"] for c in first_claims}) == workers, "duplicate_initial_claim")
            harness.require(len({c["pid"] for c in first_claims}) == workers, "workers_not_independent_processes")
            (harness.root / "load-execute").touch()
            commits = []
            for child, directory in processes:
                commits.extend(harness.finish(child, directory)["commits"])
            report["load_worker_pids"] = [c["pid"] for c in first_claims]

            report["stage"] = "killed_worker"
            client.submit("fault-killed", payload("fault-killed"), max_attempts=2)
            victim, victim_dir = harness.worker("killed-worker", jobs=1, delay=0, hold=True, retain=True)
            harness.started(victim, victim_dir)
            old = read_json(victim_dir / "claim-private.json")
            killed = harness.kill(victim)
            lease_until = client.request("/v1/jobs/fault-killed")["lease_until"]
            harness.wait(lambda: time.time() > lease_until + .1, "lease_expiry_timeout")
            retry, retry_dir = harness.worker("replacement-worker", jobs=1, delay=0, retain=True, hold=True)
            harness.started(retry, retry_dir)
            replacement_claim = read_json(retry_dir / "claim-private.json")
            active_before = client.request("/v1/jobs/fault-killed")
            harness.require(active_before["state"] == "leased" and active_before["generation"] == 2
                            and active_before["worker_id"] == "replacement-worker", "replacement_not_actively_leased")
            stale_status = harness.rejected(old, "killed-worker", fixture_completion(old, "killed-worker"))
            active_after = client.request("/v1/jobs/fault-killed")
            harness.require(active_after["state"] == "leased" and active_after["generation"] == 2
                            and active_after["result"] is None, "stale_result_changed_live_replacement")
            harness.require(replacement_claim["generation"] == 2, "replacement_claim_generation_changed")
            (retry_dir / "release").touch()
            retry_result = harness.finish(retry, retry_dir)
            new = read_json(retry_dir / "completion-private.json")
            harness.require(new["claim"]["job_id"] == "fault-killed" and new["claim"]["generation"] == 2,
                            "killed_job_not_reassigned")
            duplicate = client.complete(new["claim"], "replacement-worker", new["completion"])
            harness.require(duplicate["idempotent"] is True, "exact_repeat_not_idempotent")
            conflict = json.loads(json.dumps(new["completion"]))
            conflict["result"]["conflicting_result"] = True
            conflict_status = harness.rejected(new["claim"], "replacement-worker", conflict)
            report["faults"]["killed_worker"] = {"passed": True, "force_kill": killed,
                "old_generation": old["generation"], "new_generation": new["claim"]["generation"],
                "stale_completion_http_status": stale_status, "exact_repeat_idempotent": True,
                "stale_probe_new_generation_state": "leased", "stale_probe_preserved_live_replacement": True,
                "conflicting_completion_http_status": conflict_status,
                "replacement_acknowledged": retry_result["commits"][0]["state"] == "succeeded"}

            report["stage"] = "cancellation"
            client.submit("fault-cancel-queued", payload("fault-cancel-queued"))
            client.request("/v1/jobs/fault-cancel-queued/cancel", {})
            client.submit("fault-cancel-running", payload("fault-cancel-running"))
            cancelled, cancelled_dir = harness.worker("cancelled-worker", jobs=1, delay=0, hold=True, retain=True)
            harness.started(cancelled, cancelled_dir)
            cancelled_claim = read_json(cancelled_dir / "claim-private.json")
            client.request("/v1/jobs/fault-cancel-running/cancel", {})
            cancelled_result = harness.finish(cancelled, cancelled_dir)
            harness.require((cancelled_dir / "cancel-observed.json").exists(), "executor_did_not_observe_cancel")
            harness.require(cancelled_result["commits"][0]["state"] == "lease_lost", "cancelled_worker_committed")
            cancelled_status = harness.rejected(cancelled_claim, "cancelled-worker",
                                               fixture_completion(cancelled_claim, "cancelled-worker"))
            report["faults"]["cancellation"] = {"passed": True, "queued_and_running": True,
                "executor_observed_cancellation": True, "late_completion_http_status": cancelled_status}

            report["stage"] = "coordinator_restart"
            client.submit("fault-restart-leased", payload("fault-restart-leased"))
            held, held_dir = harness.worker("restart-live-worker", jobs=1, delay=0, hold=True)
            harness.started(held, held_dir)
            client.submit("fault-restart-queued", payload("fault-restart-queued"))
            restart_ids = [load_ids[0], "fault-restart-leased", "fault-restart-queued"]
            before = {job_id: client.request("/v1/jobs/" + job_id) for job_id in restart_ids}
            coordinator_killed = harness.kill(harness.coordinator)
            restart_began = time.monotonic()
            harness.start_coordinator()
            client = harness.client
            restart_seconds = time.monotonic() - restart_began
            after = {job_id: client.request("/v1/jobs/" + job_id) for job_id in restart_ids}
            for job_id in restart_ids:
                for field in ("state", "payload_digest", "attempts", "generation", "worker_id", "result_digest"):
                    harness.require(before[job_id][field] == after[job_id][field], "restart_changed_durable_state")
            harness.require(after["fault-restart-leased"]["state"] == "leased", "live_lease_expired_during_restart")
            (held_dir / "release").touch()
            harness.finish(held, held_dir)
            queued, queued_dir = harness.worker("restart-queued-worker", jobs=1, delay=0)
            harness.finish(queued, queued_dir)
            report["faults"]["coordinator_restart"] = {"passed": True, "force_kill": coordinator_killed,
                "replacement_pid": harness.coordinator.pid, "restart_seconds": restart_seconds,
                "same_database": True, "same_http_origin": True,
                "preserved_states": ["succeeded", "leased", "queued"], "live_worker_continued": True}

            report["stage"] = "full_readback"
            # Use individually known IDs: /jobs list is capped at 1000 and is not pagination.
            for job_id in expected:
                rows.append(public_row(client.request("/v1/jobs/" + job_id)))
            report["verification"] = verify_rows(expected, rows)
            report["load"] = load_metrics(rows, load_ids, commits, workers)
            harness.require(report["verification"]["passed"], "full_readback_verification_failed")
            harness.require(report["load"]["acknowledgements_match_plan"], "worker_acknowledgements_differ_from_plan")
            harness.require(report["load"]["observed_worker_processes"] == workers, "not_all_workers_executed")
            report["status"] = "passed"
            report["stage"] = "completed"
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            # Do not emit exception messages from sockets, subprocesses or credentials.
        finally:
            harness.close()
            if (harness.root / "jobs.sqlite").exists():
                try:
                    report["database_audit"] = database_audit(harness.root / "jobs.sqlite", expected)
                    if any(report["database_audit"][key] for key in ("missing_job_ids", "unexpected_job_ids",
                                                                  "wrong_state_job_ids", "duplicate_accepted_job_ids")):
                        report["status"] = "failed"
                except Exception as exc:
                    report["database_audit_error_type"] = type(exc).__name__
                    report["status"] = "failed"
    report["elapsed_seconds"] = time.monotonic() - began
    report["observed_jobs"] = len(rows)
    report["unverified_planned_jobs"] = len(expected) - len({r["job_id"] for r in rows})
    return report, rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--fixture-delay", type=float, default=.005)
    parser.add_argument("--lease-seconds", type=float, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, default=Path("runs/scheduler-load/report.json"))
    parser.add_argument("--child-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--url", help=argparse.SUPPRESS)
    parser.add_argument("--directory", help=argparse.SUPPRESS)
    parser.add_argument("--worker-id", help=argparse.SUPPRESS)
    parser.add_argument("--gated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retain", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_worker:
        return child_worker(args)
    rows_path = args.output.with_name(args.output.stem + ".jobs.json")
    if args.output.exists() or rows_path.exists():
        parser.error("Use a fresh output path; prior evidence is never overwritten")
    report, rows = run_validation(jobs=args.jobs, workers=args.workers, fixture_delay=args.fixture_delay,
        lease_seconds=args.lease_seconds, timeout_seconds=args.timeout_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(rows_path, rows)
    report["readback_artifact"] = {"path": rows_path.name, "sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest()}
    write_json(args.output, report)
    print(json.dumps({"status": report["status"], "stage": report["stage"], "planned_jobs": report["planned_jobs"],
                      "observed_jobs": report["observed_jobs"], "llm_calls": 0}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
