"""Durable leased jobs. SQLite belongs to one coordinator, never a shared drive.

Claims and commits are transactional. A lease is an execution permission, not a
promise of exactly-once execution: a partition can cause another worker to retry.
Only the current generation can commit; an identical committed result is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path

from .repository import RepoTask

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024
CONFIG_DEFAULTS = {
    "profile": "retrieval", "max_steps": 20, "wall_seconds": 900,
    "context_tokens": 16384, "max_output_tokens": 2048,
    "request_timeout": 180, "command_timeout": 60, "seed": 42, "cost_limit": 3.0,
    "provider": "litellm", "backend": "docker", "ollama_mode": "json",
}


class SchedulerError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchedulerError("Value is not finite JSON") from exc


def identifier(value, field="identifier"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise SchedulerError(f"Invalid {field}")
    return value


def validate_payload(value):
    """Remote inputs describe containerized repository tasks, never host commands."""
    if not isinstance(value, dict) or set(value) != {"task", "config"}:
        raise SchedulerError("Payload requires only task and config")
    try:
        task = asdict(RepoTask.parse(value["task"]))
        task["public_checks"] = list(task["public_checks"])
    except (TypeError, ValueError) as exc:
        raise SchedulerError(str(exc)) from exc
    supplied = value["config"]
    if not isinstance(supplied, dict) or set(supplied) - (set(CONFIG_DEFAULTS) | {"model"}):
        raise SchedulerError("Unknown execution configuration; endpoints and host paths are worker-local")
    config = {**CONFIG_DEFAULTS, **supplied}
    model = config.get("model")
    if (not isinstance(model, str) or not model or len(model) > 200
            or any(ord(c) < 33 or ord(c) > 126 for c in model)):
        raise SchedulerError("A bounded provider-prefixed model identifier is required")
    if not isinstance(config["profile"], str) or config["profile"] not in {"baseline", "recent", "retrieval"}:
        raise SchedulerError("Invalid context profile")
    for key, choices in (("provider", {"litellm", "ollama_native"}), ("backend", {"docker", "files"}),
                         ("ollama_mode", {"json", "tools"})):
        if not isinstance(config[key], str) or config[key] not in choices:
            raise SchedulerError(f"Invalid {key}")
    limits = {"max_steps": (1, 1000), "wall_seconds": (1, 86400),
              "context_tokens": (1024, 1000000), "max_output_tokens": (1, 32000),
              "request_timeout": (1, 1800), "command_timeout": (1, 1800),
              "seed": (0, 2**31 - 1)}
    for key, (low, high) in limits.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise SchedulerError(f"Invalid {key} budget")
    cost = config["cost_limit"]
    if (type(cost) not in {int, float} or not math.isfinite(cost) or not 0 <= cost <= 100
            or config["context_tokens"] <= config["max_output_tokens"] + 1000):
        raise SchedulerError("Invalid context/cost budgets")
    return {"task": task, "config": config}


def validate_completion(value):
    if not isinstance(value, dict) or set(value) != {"outcome", "result", "patch", "evidence"}:
        raise SchedulerError("Completion requires outcome, result, patch, and evidence")
    if not isinstance(value["outcome"], str) or value["outcome"] not in {"succeeded", "failed"}:
        raise SchedulerError("Invalid completion outcome")
    if not isinstance(value["result"], dict):
        raise SchedulerError("Result must be an object")
    if len(canonical(value["result"]).encode()) > MAX_RESULT_BYTES:
        raise SchedulerError("Result exceeds limit", 413)
    patch = value["patch"]
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise SchedulerError("Patch exceeds limit", 413)
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or len(evidence) > 32:
        raise SchedulerError("Evidence must contain up to 32 named SHA-256 digests")
    for name, digest in evidence.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                or not isinstance(digest, str) or not DIGEST.fullmatch(digest)):
            raise SchedulerError("Invalid evidence digest")
    result = json.loads(canonical(value))
    result["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
    return result


class JobStore:
    """One connection per transaction permits HTTP threads and coordinator restarts."""
    def __init__(self, path, *, lease_seconds=30, clock=time.time):
        if not 0.2 <= lease_seconds <= 3600:
            raise SchedulerError("Lease duration must be 0.2..3600 seconds")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        self.clock = clock
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY, registered REAL NOT NULL, last_seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL, max_attempts INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0, worker_id TEXT, lease_token TEXT, lease_until REAL,
                    created REAL NOT NULL, updated REAL NOT NULL, result TEXT, result_digest TEXT,
                    last_error TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT NOT NULL, generation INTEGER NOT NULL, worker_id TEXT NOT NULL,
                    started REAL NOT NULL, ended REAL, outcome TEXT, result TEXT, result_digest TEXT,
                    PRIMARY KEY(job_id, generation));
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created, job_id);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _recover(self, db, now):
        expired = db.execute("SELECT * FROM jobs WHERE state='leased' AND lease_until<=?", (now,)).fetchall()
        for row in expired:
            state = "queued" if row["attempts"] < row["max_attempts"] else "failed"
            db.execute("UPDATE attempts SET ended=?, outcome='lease_expired' WHERE job_id=? AND generation=?",
                       (now, row["job_id"], row["generation"]))
            db.execute("UPDATE jobs SET state=?, updated=?, last_error='lease_expired',"
                       "worker_id=NULL, lease_token=NULL, lease_until=NULL WHERE job_id=?",
                       (state, now, row["job_id"]))

    def register(self, worker_id):
        worker_id = identifier(worker_id, "worker_id")
        now = self.clock()
        with self._transaction() as db:
            db.execute("INSERT INTO workers VALUES(?,?,?) ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen",
                       (worker_id, now, now))
        return {"worker_id": worker_id, "lease_seconds": self.lease_seconds}

    def submit(self, job_id, payload, *, max_attempts=3):
        job_id = identifier(job_id, "job_id")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise SchedulerError("max_attempts must be 1..10")
        payload = canonical(validate_payload(payload))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = self.clock()
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if existing:
                if existing["payload_digest"] != digest or existing["max_attempts"] != max_attempts:
                    raise SchedulerError("job_id already names a different immutable task", 409)
                return {"job_id": job_id, "state": existing["state"], "idempotent": True}
            db.execute("INSERT INTO jobs(job_id,payload,payload_digest,state,max_attempts,created,updated)"
                       " VALUES(?,?,?,'queued',?,?,?)", (job_id, payload, digest, max_attempts, now, now))
        return {"job_id": job_id, "state": "queued", "idempotent": False}

    def claim(self, worker_id):
        worker_id = identifier(worker_id, "worker_id")
        now = self.clock()
        with self._transaction() as db:
            self._recover(db, now)
            if not db.execute("SELECT 1 FROM workers WHERE worker_id=?", (worker_id,)).fetchone():
                raise SchedulerError("Register worker before claiming", 409)
            # One live execution per process identity; horizontal scale uses unique IDs.
            if db.execute("SELECT 1 FROM jobs WHERE state='leased' AND worker_id=?", (worker_id,)).fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,job_id LIMIT 1").fetchone()
            db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (now, worker_id))
            if row is None:
                return None
            generation = row["generation"] + 1
            token = secrets.token_urlsafe(32)
            until = now + self.lease_seconds
            db.execute("UPDATE jobs SET state='leased',attempts=attempts+1,generation=?,worker_id=?,"
                       "lease_token=?,lease_until=?,updated=? WHERE job_id=?",
                       (generation, worker_id, token, until, now, row["job_id"]))
            db.execute("INSERT INTO attempts(job_id,generation,worker_id,started) VALUES(?,?,?,?)",
                       (row["job_id"], generation, worker_id, now))
            return {"job_id": row["job_id"], "payload": json.loads(row["payload"]),
                    "payload_digest": row["payload_digest"], "generation": generation,
                    "lease_token": token, "lease_until": until, "lease_seconds": self.lease_seconds,
                    "attempt": row["attempts"] + 1}

    def _lease(self, db, job_id, worker_id, token, generation, now):
        identifier(job_id, "job_id")
        identifier(worker_id, "worker_id")
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise SchedulerError("Job not found", 404)
        if (not isinstance(token, str) or not token or type(generation) is not int
                or row["worker_id"] != worker_id or row["lease_token"] != token
                or row["generation"] != generation):
            raise SchedulerError("Stale or invalid lease", 409)
        return row

    def heartbeat(self, job_id, worker_id, token, generation):
        now = self.clock()
        with self._transaction() as db:
            row = self._lease(db, job_id, worker_id, token, generation, now)
            if row["state"] != "leased" or row["lease_until"] <= now:
                raise SchedulerError("Lease expired or job was cancelled", 409)
            until = now + self.lease_seconds
            db.execute("UPDATE jobs SET lease_until=?,updated=? WHERE job_id=?", (until, now, job_id))
            db.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (now, worker_id))
        return {"lease_until": until}

    def complete(self, job_id, worker_id, token, generation, completion):
        completion = validate_completion(completion)
        encoded = canonical(completion)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now = self.clock()
        with self._transaction() as db:
            row = self._lease(db, job_id, worker_id, token, generation, now)
            if row["result_digest"] == digest and row["state"] in {"succeeded", "failed", "queued"}:
                return {"job_id": job_id, "state": row["state"], "idempotent": True,
                        "result_digest": digest}
            if row["state"] != "leased" or row["lease_until"] <= now:
                raise SchedulerError("Lease expired, cancelled, or already committed", 409)
            state = completion["outcome"]
            if state == "failed" and row["attempts"] < row["max_attempts"]:
                state = "queued"
            db.execute("UPDATE attempts SET ended=?,outcome=?,result=?,result_digest=?"
                       " WHERE job_id=? AND generation=?",
                       (now, completion["outcome"], encoded, digest, job_id, generation))
            # Keep final/last generation for idempotent completion replies until a new claim fences it.
            db.execute("UPDATE jobs SET state=?,updated=?,lease_until=NULL,result=?,result_digest=?,"
                       "last_error=? WHERE job_id=?", (state, now, encoded, digest,
                       "executor_failed" if completion["outcome"] == "failed" else None, job_id))
        return {"job_id": job_id, "state": state, "idempotent": False, "result_digest": digest}

    def cancel(self, job_id):
        identifier(job_id, "job_id")
        now = self.clock()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise SchedulerError("Job not found", 404)
            if row["state"] in {"succeeded", "failed"}:
                raise SchedulerError("Terminal job cannot be cancelled", 409)
            if row["state"] == "leased":
                db.execute("UPDATE attempts SET ended=?,outcome='cancelled' WHERE job_id=? AND generation=?",
                           (now, job_id, row["generation"]))
            db.execute("UPDATE jobs SET state='cancelled',lease_until=NULL,updated=? WHERE job_id=?", (now, job_id))
        return {"job_id": job_id, "state": "cancelled"}

    def get(self, job_id):
        identifier(job_id, "job_id")
        with self._transaction() as db:
            self._recover(db, self.clock())
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise SchedulerError("Job not found", 404)
            value = dict(row)
            value.pop("lease_token")
            value["payload"] = json.loads(value["payload"])
            value["result"] = json.loads(value["result"]) if value["result"] else None
            attempts = db.execute("SELECT generation,worker_id,started,ended,outcome,result_digest FROM attempts"
                                  " WHERE job_id=? ORDER BY generation", (job_id,)).fetchall()
            value["attempt_history"] = [dict(a) for a in attempts]
            return value

    def list_jobs(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise SchedulerError("List limit must be 1..1000")
        with self._transaction() as db:
            self._recover(db, self.clock())
            rows = db.execute("SELECT job_id,state,attempts,max_attempts,generation,worker_id,lease_until,"
                              "created,updated,last_error FROM jobs ORDER BY created DESC,job_id LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
