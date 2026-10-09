"""Namespace-isolated, source-backed cross-task knowledge with FTS5 and RRF.

Every operation uses its own SQLite connection and short transaction, permitting
multiple workers/processes. Model tools expose search/read only. Operators may
ingest trusted harness artifacts; agent-authored prose is never auto-verified.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_CONTENT_CHARS = 32768
MAX_READ_CHARS = 4000
MAX_SEARCH_RESULTS = 8


def _hash(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _vector(vector: Any) -> list[float]:
    if not isinstance(vector, list) or not 1 <= len(vector) <= 4096:
        raise ValueError("embedding must contain 1..4096 numbers")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in vector):
        raise ValueError("embedding values must be finite numbers")
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding must have a nonzero finite norm")
    return [v / norm for v in values]


class OllamaEmbedder:
    """Bounded /api/embed adapter, pinned to the local model's /api/tags digest."""
    def __init__(self, model: str = "bge-m3", base_url: str = "http://127.0.0.1:11434", timeout: float = 60):
        endpoint = urlsplit(base_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
            raise ValueError("Ollama base URL must be an HTTP origin without credentials")
        if not model.strip() or len(model) > 200 or not 0 < timeout <= 300:
            raise ValueError("invalid embedding model or request timeout")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.model_digest: str | None = None
        self.dimension: int | None = None

    def _json(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Ollama response exceeds 4 MiB")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Ollama response must be an object")
        return result

    def _digest(self) -> str:
        wanted = self.model if ":" in self.model else self.model + ":latest"
        models = self._json("/api/tags").get("models", [])
        match = next((m for m in models if m.get("name") in {self.model, wanted}), None)
        digest = match.get("digest") if match else None
        if not isinstance(digest, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
            raise ValueError("embedding model requires a digest in Ollama /api/tags")
        return digest

    @property
    def identity(self) -> str:
        if self.model_digest is None:
            self.model_digest = self._digest()
        return f"ollama:{self.model}@{self.model_digest}"

    def embed(self, text: str) -> list[float]:
        if not text.strip() or len(text) > MAX_CONTENT_CHARS:
            raise ValueError("embedding input must contain 1..32768 characters")
        # Recheck before each request; mutable model tags cannot mix vector spaces.
        digest = self._digest()
        if self.model_digest is not None and digest != self.model_digest:
            raise ValueError("Ollama model digest changed during this embedding session")
        self.model_digest = digest
        response = self._json("/api/embed", {"model": self.model, "input": text,
                                                   "truncate": False, "keep_alive": "5m"})
        vectors = response.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise ValueError("Ollama must return exactly one embedding")
        vector = _vector(vectors[0])
        if self.dimension is not None and len(vector) != self.dimension:
            raise ValueError("embedding dimension changed")
        self.dimension = len(vector)
        return vector

    def metadata(self) -> dict[str, Any]:
        return {"provider": "ollama", "model": self.model, "model_digest": self.model_digest,
                "dimension": self.dimension, "endpoint": self.base_url}


class KnowledgeStore:
    def __init__(self, path: Path | str, namespace: str, embedder: Any = None, semantic_candidate_limit: int = 5000):
        if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 128:
            raise ValueError("namespace must contain 1..128 characters")
        if not 1 <= semantic_candidate_limit <= 20000:
            raise ValueError("semantic candidate limit must be between 1 and 20000")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.namespace_sha256 = _hash(namespace)
        self.embedder = embedder
        self.semantic_candidate_limit = semantic_candidate_limit
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge (
                  source_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, identity TEXT NOT NULL,
                  title TEXT NOT NULL, body TEXT NOT NULL, repo TEXT NOT NULL, base_commit TEXT NOT NULL,
                  task_id TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL, verified INTEGER NOT NULL, status TEXT NOT NULL,
                  verification_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  expires_at REAL, embedding_json TEXT, embedding_model TEXT, embedding_error TEXT,
                  UNIQUE(namespace, identity));
                CREATE INDEX IF NOT EXISTS knowledge_namespace ON knowledge(namespace, status, verified, expires_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                  source_id UNINDEXED, namespace UNINDEXED, title, body, tokenize='unicode61');
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, *, title: str, body: str, source_type: str, source_ref: str,
            repo: str = "", base_commit: str = "", task_id: str = "", verified: bool = False,
            verification: dict[str, Any] | None = None, ttl_days: float | None = 90) -> dict[str, Any]:
        """Operator-side import. A tool call cannot promote its own claims."""
        for name, value, cap in (("title", title, 200), ("body", body, MAX_CONTENT_CHARS),
                                 ("source_type", source_type, 80), ("source_ref", source_ref, 2000)):
            if not isinstance(value, str) or not value.strip() or len(value) > cap:
                raise ValueError(f"{name} must contain 1..{cap} characters")
        if not isinstance(verified, bool) or (verified and not verification):
            raise ValueError("verified knowledge requires explicit operator-supplied verification evidence")
        if len(repo) > 300 or len(task_id) > 200 or (base_commit and not re.fullmatch(r"[0-9a-f]{40}", base_commit)):
            raise ValueError("invalid knowledge provenance")
        if ttl_days is not None and (not isinstance(ttl_days, (int, float)) or
                                    not math.isfinite(ttl_days) or not 0 <= ttl_days <= 3650):
            raise ValueError("TTL must be 0..3650 days or None")
        verification_json = json.dumps(verification or {}, ensure_ascii=False, sort_keys=True)
        if len(verification_json) > 12000:
            raise ValueError("verification evidence exceeds the metadata budget")
        identity = _hash(json.dumps([source_type, source_ref, title], separators=(",", ":")))
        source_id = "k" + _hash(self.namespace_sha256 + identity)[:32]
        vector, model, error = None, None, None
        if self.embedder is not None:
            try:
                vector = json.dumps(_vector(self.embedder.embed((title + "\n" + body)[:MAX_CONTENT_CHARS])))
                model = self.embedder.identity
            except Exception as exc:
                error = type(exc).__name__  # provider messages may include sensitive payloads
        now = time.time()
        expires = None if ttl_days is None else now + ttl_days * 86400
        # UPSERT and its FTS replacement occur atomically under the write lock.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(namespace,identity) DO UPDATE SET
                title=excluded.title,body=excluded.body,repo=excluded.repo,base_commit=excluded.base_commit,
                task_id=excluded.task_id,content_sha256=excluded.content_sha256,verified=excluded.verified,
                status=excluded.status,verification_json=excluded.verification_json,updated_at=excluded.updated_at,
                expires_at=excluded.expires_at,embedding_json=excluded.embedding_json,
                embedding_model=excluded.embedding_model,embedding_error=excluded.embedding_error""",
                (source_id, self.namespace, identity, title, body, repo, base_commit, task_id,
                 source_type, source_ref, _hash(body), int(verified), "active", verification_json,
                 now, now, expires, vector, model, error))
            db.execute("DELETE FROM knowledge_fts WHERE source_id=? AND namespace=?", (source_id, self.namespace))
            db.execute("INSERT INTO knowledge_fts(source_id,namespace,title,body) VALUES(?,?,?,?)",
                       (source_id, self.namespace, title, body))
        return self.read(source_id, limit=1)

    @staticmethod
    def _metadata(row: sqlite3.Row, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        expired = row["expires_at"] is not None and row["expires_at"] <= now
        return {key: row[key] for key in ("source_id", "title", "repo", "base_commit", "task_id",
                "source_type", "source_ref", "content_sha256", "created_at", "updated_at", "expires_at",
                "embedding_model", "embedding_error")} | {
                "namespace_sha256": _hash(row["namespace"]),
                "verified": bool(row["verified"]) and row["status"] == "active" and not expired,
                "status": "expired" if expired else row["status"],
                "verification": json.loads(row["verification_json"]),
                "dimension": len(json.loads(row["embedding_json"])) if row["embedding_json"] else None}

    def read(self, source_id: str, offset: int = 0, limit: int = 2000,
             exclude_task_ids: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        if not re.fullmatch(r"k[0-9a-f]{32}", source_id) or offset < 0 or not 1 <= limit <= MAX_READ_CHARS:
            raise ValueError("knowledge read needs a valid source ID, nonnegative offset and limit 1..4000")
        with self._connect() as db:
            row = db.execute("SELECT * FROM knowledge WHERE namespace=? AND source_id=?",
                             (self.namespace, source_id)).fetchone()
        if row is None:
            raise ValueError("source ID was not found in this namespace")
        if (not isinstance(exclude_task_ids, (list, tuple)) or len(exclude_task_ids) > 1000 or
                any(not isinstance(task, str) or not task or len(task) > 200 for task in exclude_task_ids)):
            raise ValueError("excluded task IDs must be a bounded list or tuple")
        if row["task_id"] in exclude_task_ids:
            raise ValueError("knowledge from an excluded current task cannot be read")
        body = row["body"]
        if _hash(body) != row["content_sha256"]:
            raise ValueError("knowledge content hash mismatch")
        end = min(len(body), offset + limit)
        return self._metadata(row) | {"offset": offset, "total_chars": len(body),
            "text": body[offset:end], "next_offset": end if end < len(body) else None}

    def search(self, query: str, limit: int = 5, verified_only: bool = True, repo: str | None = None,
               exclude_task_ids: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError("knowledge search needs query 1..200 characters and limit 1..8")
        terms = re.findall(r"\w+", query, re.UNICODE)[:32]
        if not terms:
            raise ValueError("search query needs at least one word or code identifier")
        condition = "k.namespace=? AND k.status='active' AND (k.expires_at IS NULL OR k.expires_at>?)"
        params: list[Any] = [self.namespace, time.time()]
        if verified_only:
            condition += " AND k.verified=1"
        if repo is not None:
            condition += " AND k.repo=?"
            params.append(repo)
        if (not isinstance(exclude_task_ids, (list, tuple)) or len(exclude_task_ids) > 1000 or
                any(not isinstance(task, str) or not task or len(task) > 200 for task in exclude_task_ids)):
            raise ValueError("excluded task IDs must be a list or tuple of at most 1000 valid IDs")
        if exclude_task_ids:
            condition += " AND k.task_id NOT IN (" + ",".join("?" for _ in exclude_task_ids) + ")"
            params.extend(exclude_task_ids)
        lexical: list[sqlite3.Row] = []
        semantic_rows: list[sqlite3.Row] = []
        with self._connect() as db:
            # Operators and quotation marks never enter the FTS query grammar.
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            lexical = db.execute("SELECT k.*,bm25(knowledge_fts,0,0,2,1) AS bm25_score FROM knowledge_fts "
                "JOIN knowledge k ON k.source_id=knowledge_fts.source_id WHERE knowledge_fts MATCH ? AND " +
                condition + " ORDER BY bm25_score,k.source_id LIMIT 64", [match] + params).fetchall()
            if self.embedder is not None:
                semantic_rows = db.execute("SELECT k.* FROM knowledge k WHERE " + condition +
                    " AND k.embedding_json IS NOT NULL ORDER BY k.updated_at DESC,k.source_id LIMIT ?",
                    params + [self.semantic_candidate_limit + 1]).fetchall()
        ranks: dict[str, float] = {}
        records = {r["source_id"]: r for r in lexical}
        lexical_rank = {}
        semantic_rank = {}
        for rank, row in enumerate(lexical, 1):
            ranks[row["source_id"]] = 1 / (60 + rank)
            lexical_rank[row["source_id"]] = rank
        error = None
        semantic_used = False
        truncated = len(semantic_rows) > self.semantic_candidate_limit
        if self.embedder is not None:
            try:
                query_vector = _vector(self.embedder.embed(query))
                identity = self.embedder.identity
                scored = []
                for row in semantic_rows[:self.semantic_candidate_limit]:
                    if row["embedding_model"] != identity:
                        continue
                    vector = json.loads(row["embedding_json"])
                    if len(vector) != len(query_vector):
                        continue
                    similarity = sum(a * b for a, b in zip(vector, query_vector))
                    if similarity > 0:
                        scored.append((similarity, row["source_id"], row))
                for rank, (_, source_id, row) in enumerate(sorted(scored, key=lambda v: (-v[0], v[1]))[:64], 1):
                    records[source_id] = row
                    ranks[source_id] = ranks.get(source_id, 0) + 1 / (60 + rank)
                    semantic_rank[source_id] = rank
                semantic_used = bool(scored)
            except Exception as exc:
                error = type(exc).__name__
        hits = []
        for source_id in sorted(ranks, key=lambda key: (-ranks[key], key))[:limit]:
            row = records[source_id]
            snippet = row["body"][:500]
            # Find a useful literal excerpt, leaving full evidence behind read().
            position = next((row["body"].lower().find(term.lower()) for term in terms
                             if term.lower() in row["body"].lower()), 0)
            offset = max(0, position - 100)
            snippet = row["body"][offset:offset + 500]
            hits.append(self._metadata(row) | {"offset": offset, "snippet": snippet,
                "rrf_score": ranks[source_id], "lexical_rank": lexical_rank.get(source_id),
                "semantic_rank": semantic_rank.get(source_id)})
        return {"hits": hits, "method": "fts5_bm25+cosine_rrf" if semantic_used else "fts5_bm25",
                "namespace_sha256": self.namespace_sha256, "verified_only": verified_only,
                "excluded_task_ids": list(exclude_task_ids),
                "semantic_candidate_limit": self.semantic_candidate_limit,
                "semantic_candidates_truncated": truncated, "embedding_error": error,
                "embedding": self.embedder.metadata() if self.embedder is not None and hasattr(self.embedder, "metadata") else None}

    def invalidate(self, source_id: str, reason: str) -> None:
        if not reason.strip() or len(reason) > 1000:
            raise ValueError("invalidation reason needs 1..1000 characters")
        with self._connect() as db:
            cursor = db.execute("UPDATE knowledge SET status='stale',verified=0,updated_at=?,"
                "verification_json=? WHERE source_id=? AND namespace=?", (time.time(),
                json.dumps({"kind": "invalidated", "reason": reason}), source_id, self.namespace))
            if not cursor.rowcount:
                raise ValueError("source ID was not found in this namespace")

    def stats(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT count(*) AS total,sum(embedding_json IS NOT NULL) AS embedded,"
                "sum(embedding_error IS NOT NULL) AS embedding_errors FROM knowledge WHERE namespace=?",
                (self.namespace,)).fetchone()
        return {"namespace_sha256": self.namespace_sha256, "total": rows["total"],
                "embedded": rows["embedded"] or 0, "embedding_errors": rows["embedding_errors"] or 0,
                "embedding": self.embedder.metadata() if self.embedder is not None and hasattr(self.embedder, "metadata") else None}

    def index_pending(self, limit: int = 100) -> dict[str, Any]:
        """Backfill vectors without holding the database lock during HTTP calls."""
        if self.embedder is None or not 1 <= limit <= 1000:
            raise ValueError("indexing requires an embedder and limit 1..1000")
        identity = self.embedder.identity
        with self._connect() as db:
            rows = db.execute("SELECT * FROM knowledge WHERE namespace=? AND status='active' "
                "AND (embedding_json IS NULL OR embedding_model!=?) ORDER BY source_id LIMIT ?",
                (self.namespace, identity, limit)).fetchall()
        indexed = failed = concurrently_changed = 0
        for row in rows:
            error = vector = None
            try:
                vector = json.dumps(_vector(self.embedder.embed((row['title'] + '\n' + row['body'])[:MAX_CONTENT_CHARS])))
            except Exception as exc:
                error = type(exc).__name__
            with self._connect() as db:
                cursor = db.execute("UPDATE knowledge SET embedding_json=?,embedding_model=?,embedding_error=? "
                    "WHERE namespace=? AND source_id=? AND content_sha256=? AND updated_at=?",
                    (vector, identity if vector else None, error, self.namespace, row["source_id"],
                     row["content_sha256"], row["updated_at"]))
            if not cursor.rowcount:
                concurrently_changed += 1
            elif vector is None:
                failed += 1
            else:
                indexed += 1
        return {"indexed": indexed, "failed": failed, "concurrently_changed": concurrently_changed,
                "embedding_model": identity, "stats": self.stats()}

    def ingest_run(self, run_dir: Path | str, ttl_days: float | None = 90) -> dict[str, Any]:
        """Import trusted harness output, verifying files and independent check provenance.

        Hashes detect mismatch, not adversarial forgery. The operator must trust the
        harness run directory. Narratives/notes/trajectory are not verification.
        """
        directory = Path(run_dir).resolve()
        def load(name: str, cap: int) -> tuple[bytes, Any]:
            path = directory / name
            if path.is_symlink() or not path.resolve().is_relative_to(directory) or path.stat().st_size > cap:
                raise ValueError("run artifact violates path or size bounds")
            raw = path.read_bytes()
            if len(raw) > cap:
                raise ValueError("run artifact exceeds size budget")
            return raw, json.loads(raw) if name.endswith(".json") else raw.decode("utf-8")
        spec_raw, spec = load("spec.json", 1024 * 1024)
        result_raw, result = load("result.json", 16 * 1024 * 1024)
        patch_raw, patch = load("model.patch", 8 * 1024 * 1024)
        if not isinstance(spec, dict) or not isinstance(result, dict):
            raise ValueError("run spec and result must be objects")
        task = spec.get("task", {})
        if not isinstance(task, dict) or not all(isinstance(task.get(k), str) for k in
                                               ("instance_id", "repo", "base_commit", "problem_statement")):
            raise ValueError("run spec needs repository task provenance")
        from .repository import RepoTask
        if RepoTask.parse(task).fingerprint() != spec.get("task_hash"):
            raise ValueError("task fingerprint does not match the frozen spec")
        if (result.get("instance_id") != task["instance_id"] or result.get("model_patch") != patch or
                result.get("patch_sha256") != _hash(patch_raw)):
            raise ValueError("result and patch do not match the frozen run")
        provenance = result.get("provenance", {})
        for field in ("task", "task_hash", "implementation_sha256", "upstream_version", "upstream_commit", "config"):
            if field not in spec or provenance.get(field) != spec[field]:
                raise ValueError("result provenance does not match spec")
        checks = result.get("public_checks", [])
        if not isinstance(checks, list) or len(checks) > 8 or any(not isinstance(c, dict) for c in checks):
            raise ValueError("run public_checks must be a bounded list of check records")
        expected = task.get("public_checks", [])
        verified = (result.get("fixture") is False and result.get("provider") in {"litellm", "ollama_native"} and
            result.get("status") == "submitted" and result.get("exit_status") == "Submitted" and result.get("patch_valid") is True and
            isinstance(checks, list) and bool(expected) and len(checks) == len(expected) and
            all(isinstance(c, dict) and c.get("command") == command and c.get("returncode") == 0
                and c.get("passed") is True for c, command in zip(checks, expected)))
        evidence = {"kind": "independent_public_checks" if verified else "unverified_run_artifacts",
            "spec_sha256": _hash(spec_raw), "result_sha256": _hash(result_raw), "patch_sha256": _hash(patch_raw),
            "patch_valid": result.get("patch_valid"), "run_status": result.get("status"),
            "checks": [{k: c.get(k) for k in ("command", "returncode", "passed")} for c in checks if isinstance(c, dict)],
            "official_resolved": None, "trust_boundary": "operator-imported harness artifacts; hashes are not signatures"}
        body = ("Task requirement (public):\n" + task["problem_statement"][:6000] +
                "\n\nIndependent public check outputs:\n" +
                "\n".join(str(c.get("output", ""))[:1500] for c in checks[:8] if isinstance(c, dict)) +
                "\n\nDelivered patch (excerpt):\n" + patch[:18000])[:MAX_CONTENT_CHARS]
        # Absolute local/company paths never become source identifiers.
        return self.put(title=f"{task['instance_id']}: repository delivery", body=body,
            source_type="repository_run", source_ref="run:" + _hash(spec_raw + patch_raw),
            repo=task["repo"], base_commit=task["base_commit"], task_id=task["instance_id"],
            verified=verified, verification=evidence, ttl_days=ttl_days)

    def close(self) -> None:
        """Compatibility hook; connections are scoped to individual operations."""
