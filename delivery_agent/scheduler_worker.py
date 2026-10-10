"""Independent HTTP workers and coordinator client. Secrets remain worker-local."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import signal
import threading
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .scheduler import (CONFIG_DEFAULTS, MAX_PATCH_BYTES, SchedulerError, canonical,
                        identifier, validate_payload)

CLAIM_RECOVERY_ATTEMPTS = 3


class Client:
    def __init__(self, url, token, *, timeout=10, ca_file=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("Coordinator URL must be an HTTP(S) origin without credentials")
        if not isinstance(token, str) or not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
            raise ValueError("A bounded external bearer token is required")
        if not 0.05 <= timeout <= 120:
            raise ValueError("Invalid HTTP timeout")
        self.url, self._token, self.timeout = url.rstrip("/"), token, float(timeout)
        self.context = ssl.create_default_context(cafile=ca_file) if parsed.scheme == "https" else None
        # Coordinator origins are explicitly configured. Never leak its bearer
        # token through inherited HTTP_PROXY or Windows registry proxy settings.
        self._opener = build_opener(ProxyHandler({}), HTTPSHandler(context=self.context))
        self._pending_claims = {}

    def request(self, path, value=None):
        body = canonical(value).encode("utf-8") if value is not None else None
        request = Request(self.url + path, data=body,
                          headers={"Authorization": "Bearer " + self._token,
                                   "Content-Type": "application/json"},
                          method="POST" if body is not None else "GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                # Job replies carry one bounded patch; traces are never transmitted.
                raw = response.read(64 * 1024 * 1024 + 1)
                if len(raw) > 64 * 1024 * 1024:
                    raise SchedulerError("Coordinator response exceeds limit", 413)
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            # Discard untrusted remote text, including proxies echoing credentials.
            status = exc.code
            exc.close()
            raise SchedulerError(f"Coordinator rejected request ({status})", status) from exc

    def register(self, worker_id):
        return self.request("/v1/workers/register", {"worker_id": worker_id})

    def submit(self, job_id, payload, max_attempts=3):
        return self.request("/v1/jobs", {"job_id": job_id, "payload": payload, "max_attempts": max_attempts})

    def claim(self, worker_id):
        worker_id = identifier(worker_id, "worker_id")
        # A lost response may hide a committed lease. Reuse only this logical
        # request, never an arbitrary active lease belonging to the worker.
        request_id = self._pending_claims.setdefault(worker_id, uuid.uuid4().hex)
        response = self.request("/v1/claim", {"worker_id": worker_id, "request_id": request_id})
        if (not isinstance(response, dict) or "job" not in response
                or response["job"] is not None and not isinstance(response["job"], dict)):
            raise SchedulerError("Coordinator claim response is malformed", 502)
        self._pending_claims.pop(worker_id)
        return response["job"]

    def heartbeat(self, claim, worker_id):
        return self.request(f"/v1/jobs/{identifier(claim['job_id'])}/heartbeat", self._lease(claim, worker_id))

    def complete(self, claim, worker_id, completion):
        return self.request(f"/v1/jobs/{identifier(claim['job_id'])}/complete",
                            {**self._lease(claim, worker_id), "completion": completion})

    @staticmethod
    def _lease(claim, worker_id):
        return {"worker_id": worker_id, "lease_token": claim["lease_token"], "generation": claim["generation"]}


def evidence_manifest(directory):
    """Hash known artifacts locally; neither provider traces nor host paths leave worker."""
    allowed = {"spec.json": "spec.json", "result.json": "result.json", "model.patch": "model.patch",
               "history.sqlite": "agent/history.sqlite", "trajectory.json": "agent/trajectory.json",
               "request-views.jsonl": "agent/request-views.jsonl"}
    manifest = {}
    for name, relative in allowed.items():
        path = directory / relative
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest[name] = digest.hexdigest()
    return manifest


def result_summary(result):
    keys = {"instance_id", "model_name_or_path", "provider", "fixture", "status", "exit_status",
            "patch_valid", "official_resolved", "changed_files", "seconds", "error_type",
            "patch_sha256", "patch_bytes", "partial_patch_unavailable"}
    summary = {k: result[k] for k in keys if k in result}
    summary["public_checks"] = [{"passed": check.get("passed"), "returncode": check.get("returncode"),
        "output_sha256": hashlib.sha256(str(check.get("output", "")).encode()).hexdigest()}
        for check in result.get("public_checks", [])]
    provenance = result.get("provenance", {})
    summary["provenance"] = {k: provenance[k] for k in ("task_hash", "implementation_sha256",
        "upstream_version", "upstream_commit", "resolved_image", "model_digest") if k in provenance}
    return summary


def execute_repository(claim, directory, cancelled, local_options=None):
    """Run actual delivery CLI with cancellation-scoped Docker sessions.

    Cancel/lease loss closes all containers owned by this task and prevents new
    commands. Already-sent provider requests cannot be recalled; their timeout
    remains bounded and a cancelled worker cannot commit the resulting patch.
    """
    from .cli import run_repository
    from .repository import RepoTask
    from .runtime import DockerSession
    from .file_environment import FileEnvironment

    options = dict(local_options or {})
    payload = validate_payload(claim["payload"])
    config = {**CONFIG_DEFAULTS, **payload["config"], **options}
    config.setdefault("api_base", None)
    args = SimpleNamespace(**config)
    sessions, lock, watcher_done = set(), threading.Lock(), threading.Event()
    cleanup_errors = []

    SessionBase = FileEnvironment if config["backend"] == "files" else DockerSession

    class CancelAwareSession(SessionBase):
        def __init__(self, *a, **kw):
            self._close_lock = threading.RLock()
            if cancelled.is_set():
                raise RuntimeError("Job execution was cancelled")
            super().__init__(*a, **kw)
            with lock:
                sessions.add(self)
            # Constructor may have raced lease loss. It still owns its cleanup.
            if cancelled.is_set():
                self.close()
                raise RuntimeError("Job execution was cancelled during container start")

        def execute(self, *a, **kw):
            if cancelled.is_set():
                self.close()
                raise RuntimeError("Job execution was cancelled")
            return super().execute(*a, **kw)

        def close(self):
            with self._close_lock:
                super().close()
            with lock:
                sessions.discard(self)

        cleanup = close

    def stop_containers():
        while not watcher_done.is_set():
            if cancelled.wait(0.05):
                with lock:
                    active = list(sessions)
                for session in active:
                    try:
                        session.close()
                    except Exception as exc:
                        cleanup_errors.append(type(exc).__name__)
                return

    watcher = threading.Thread(target=stop_containers, daemon=True)
    watcher.start()
    try:
        if cancelled.is_set():
            raise RuntimeError("Job execution was cancelled")
        result = run_repository(RepoTask.parse(payload["task"]), args, directory,
                                session_factory=CancelAwareSession)
    finally:
        watcher_done.set()
        watcher.join(timeout=65)
        with lock:
            active = list(sessions)
        # Retry visible failed context-manager cleanup; never forget an owned session.
        for session in active:
            try:
                session.close()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
    if cleanup_errors:
        raise RuntimeError("Cancelled container cleanup could not be confirmed")
    patch = result.get("model_patch", "")
    if len(patch.encode()) > MAX_PATCH_BYTES:
        raise SchedulerError("Delivery patch exceeds limit", 413)
    summary = result_summary(result)
    # Digest local options without exposing endpoints, host paths, or credentials.
    # Local config must contain paths/IDs only; API credentials remain in environment.
    summary["worker"] = {"hostname": socket.gethostname(),
        "configuration_sha256": hashlib.sha256(canonical(config).encode()).hexdigest()}
    return {"outcome": "succeeded" if result.get("status") == "submitted" else "failed",
            "result": summary, "patch": patch, "evidence": evidence_manifest(directory)}


class Worker:
    """One independently deployed process; execute jobs sequentially within it."""
    def __init__(self, client, worker_id, work_root, *, executor=execute_repository,
                 local_options=None, poll_seconds=0.5):
        self.client, self.worker_id = client, identifier(worker_id, "worker_id")
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        if not 0.01 <= poll_seconds <= 60:
            raise ValueError("Invalid poll interval")
        self.executor, self.local_options, self.poll_seconds = executor, local_options, poll_seconds
        self.stop = threading.Event()

    def _execute(self, claim, claim_started):
        cancelled = threading.Event()
        done = threading.Event()
        box = {}
        directory = self.work_root / (claim["job_id"] + f"-g{claim['generation']}-" + uuid.uuid4().hex[:8])
        # Default executor creates this fresh directory; worker never reuses a prior attempt.
        def invoke():
            try:
                box["completion"] = self.executor(claim, directory, cancelled, self.local_options)
            except Exception as exc:
                box["completion"] = {"outcome": "failed", "result": {"error_type": type(exc).__name__},
                                     "patch": "", "evidence": {}}
            finally:
                done.set()

        runner = threading.Thread(target=invoke, daemon=True)
        runner.start()
        lease_seconds = claim["lease_seconds"]
        interval = max(0.05, lease_seconds / 3)
        deadline = claim_started + lease_seconds
        next_heartbeat = time.monotonic()  # renew immediately after beginning execution
        lease_lost = False
        original_timeout = self.client.timeout
        self.client.timeout = min(original_timeout, max(0.05, lease_seconds / 4))
        try:
            while not done.wait(min(0.05, interval)):
                if self.stop.is_set():
                    lease_lost = True
                    cancelled.set()
                now = time.monotonic()
                if now >= deadline:
                    lease_lost = True
                    cancelled.set()
                if lease_lost:
                    continue  # wait for bounded executor and its container cleanup
                if now >= next_heartbeat:
                    started = time.monotonic()
                    try:
                        self.client.heartbeat(claim, self.worker_id)
                        deadline = started + lease_seconds
                        next_heartbeat = time.monotonic() + interval
                    except SchedulerError:
                        lease_lost = True
                        cancelled.set()
                    except (OSError, URLError, TimeoutError):
                        next_heartbeat = time.monotonic() + min(interval, 0.25)
            if lease_lost or self.stop.is_set() or time.monotonic() >= deadline:
                cancelled.set()
                return {"job_id": claim["job_id"], "state": "lease_lost"}
            completion = box["completion"]
            # Lost completion responses may be retried with the same exact digest.
            for attempt in range(3):
                try:
                    return self.client.complete(claim, self.worker_id, completion)
                except SchedulerError as exc:
                    if exc.status != 500:
                        return {"job_id": claim["job_id"], "state": "commit_rejected"}
                except (OSError, URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    break
                self.stop.wait(min(0.1 * (attempt + 1), max(0, deadline - time.monotonic())))
            return {"job_id": claim["job_id"], "state": "commit_unknown"}
        finally:
            self.client.timeout = original_timeout
            cancelled.set()

    def run(self, *, max_jobs=None, idle_seconds=None):
        if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 1):
            raise ValueError("max_jobs must be positive")
        if idle_seconds is not None and idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self.client.register(self.worker_id)
        count, idle_since, commits, claim_failures = 0, time.monotonic(), [], 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                claim = self.client.claim(self.worker_id)
            except (SchedulerError, OSError, URLError, TimeoutError, HTTPException,
                    json.JSONDecodeError, UnicodeDecodeError) as exc:
                if isinstance(exc, SchedulerError) and exc.status not in {500, 502, 503, 504}:
                    raise
                claim_failures += 1
                if claim_failures >= CLAIM_RECOVERY_ATTEMPTS:
                    # Do not report an idle success while a committed claim may
                    # still be unknown. The pending request ID remains reusable.
                    raise SchedulerError("Claim response remains unknown after bounded retries", 503) from exc
                self.stop.wait(min(self.poll_seconds, 0.1 * (2 ** (claim_failures - 1))))
                continue
            if claim_failures:
                # Only a recovered response resets idle time. In particular, an
                # expired request may return None; allow the next fresh request.
                idle_since = time.monotonic()
                claim_failures = 0
            if claim is None:
                if idle_seconds is not None and time.monotonic() - idle_since >= idle_seconds:
                    break
                self.stop.wait(self.poll_seconds)
                continue
            idle_since = time.monotonic()
            commits.append(self._execute(claim, started))
            count += 1
            if max_jobs is not None and count >= max_jobs:
                break
        return commits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-env", default="DELIVERY_SCHEDULER_TOKEN")
    parser.add_argument("--ca-file")
    parser.add_argument("--work-root", default="runs/distributed")
    parser.add_argument("--worker-id", default="worker-" + uuid.uuid4().hex[:12])
    parser.add_argument("--api-base", help="Worker-local model endpoint; never supplied by coordinator")
    parser.add_argument("--codex-binary", help="Worker-local Codex CLI executable; never supplied by coordinator")
    parser.add_argument("--skills", nargs="*", default=[], help="Worker-local skill roots")
    parser.add_argument("--knowledge-db", help="Worker-local persistent knowledge database")
    parser.add_argument("--knowledge-namespace", default="repository-delivery")
    parser.add_argument("--embedding-model", help="Optional worker-local Ollama embedding model")
    parser.add_argument("--embedding-api-base", default="http://127.0.0.1:11434",
                        help="Worker-local embedding endpoint, independent of the coding model endpoint")
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--idle-seconds", type=float)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--submit", help="JSON file containing task and config")
    actions.add_argument("--status", help="Get complete job and delivered patch")
    actions.add_argument("--cancel", help="Cancel a queued or running job")
    actions.add_argument("--list", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.api_base:
        endpoint = urlsplit(args.api_base)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            parser.error("Model endpoint must exclude credentials and queries")
    embedding_endpoint = urlsplit(args.embedding_api_base)
    if (embedding_endpoint.scheme not in {"http", "https"} or not embedding_endpoint.hostname
            or embedding_endpoint.username or embedding_endpoint.password or embedding_endpoint.query
            or embedding_endpoint.fragment):
        parser.error("Embedding endpoint must exclude credentials and queries")
    try:
        client = Client(args.url, os.environ.get(args.token_env, ""), ca_file=args.ca_file)
        if args.submit:
            if not args.job_id:
                parser.error("--job-id required with --submit")
            value = client.submit(args.job_id, json.loads(Path(args.submit).read_text(encoding="utf-8")), args.max_attempts)
        elif args.status:
            value = client.request("/v1/jobs/" + identifier(args.status))
        elif args.cancel:
            value = client.request("/v1/jobs/" + identifier(args.cancel) + "/cancel", {})
        elif args.list:
            value = client.request("/v1/jobs")
        else:
            worker = Worker(client, args.worker_id, args.work_root, local_options={"api_base": args.api_base,
                "codex_binary": args.codex_binary,
                "skills": args.skills, "knowledge_db": args.knowledge_db,
                "knowledge_namespace": args.knowledge_namespace, "embedding_model": args.embedding_model,
                "embedding_api_base": args.embedding_api_base})
            # Cooperative termination allows the executor to close its owned containers.
            # A second force-kill or remote machine crash is handled by lease recovery.
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: worker.stop.set())
            try:
                value = worker.run(max_jobs=args.max_jobs, idle_seconds=args.idle_seconds)
            except KeyboardInterrupt:
                worker.stop.set()
                return 130
        print(canonical(value), flush=True)
    except (SchedulerError, ValueError, OSError, URLError) as exc:
        print(canonical({"error_type": type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
