import hashlib
import json
import math
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from delivery_agent.knowledge import KnowledgeStore, OllamaEmbedder, _vector
from delivery_agent.repository import RepoTask


class FakeEmbedder:
    identity = "test:fixed-space@digest"
    def embed(self, text):
        return [1, 0] if any(t in text.lower() for t in ("generator", "one-shot", "single traversal")) else [0, 1]
    def metadata(self):
        return {"model": "test-fixed", "model_digest": "digest", "dimension": 2}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "knowledge.sqlite"
        self.store = KnowledgeStore(self.path, "project-a")

    def put(self, store=None, **updates):
        values = {"title": "Iterator exhaustion", "body": "Consume a generator only once.",
                  "source_type": "curated", "source_ref": "doc:one", "verified": True,
                  "verification": {"kind": "operator_review", "evidence": "review-1"}}
        return (store or self.store).put(**(values | updates))

    def test_fts_rank_persistent_ids_and_pagination(self):
        one = self.put(body="a generator " * 1000)
        self.put(title="Network", body="Socket timeout behavior", source_ref="doc:two")
        reopened = KnowledgeStore(self.path, "project-a")
        hit = reopened.search("generator")
        self.assertEqual(hit["hits"][0]["source_id"], one["source_id"])
        self.assertEqual(hit["method"], "fts5_bm25")
        first = reopened.read(one["source_id"], 0, 100)
        second = reopened.read(one["source_id"], first["next_offset"], 100)
        self.assertEqual(first["text"] + second["text"], ("a generator " * 1000)[:200])

    def test_namespace_isolation_and_parameterized_fts_query(self):
        source = self.put()
        other = KnowledgeStore(self.path, "project-b")
        self.put(other)
        with self.assertRaises(ValueError):
            other.read(source["source_id"])
        self.assertNotEqual(other.namespace_sha256, self.store.namespace_sha256)
        self.assertEqual(len(self.store.search('generator OR "timeout"')['hits']), 1)
        with self.assertRaises(ValueError):
            self.store.search("*")

    def test_active_task_exclusion_applies_to_lexical_and_semantic_candidates(self):
        store = KnowledgeStore(self.path, "project-a", FakeEmbedder())
        same = self.put(store, task_id="current-task")
        prior = self.put(store, title="Previous iterator failure", source_ref="prior", task_id="prior-task")
        hits = store.search("generator", exclude_task_ids=["current-task"])
        self.assertEqual([h["source_id"] for h in hits["hits"]], [prior["source_id"]])
        self.assertNotIn(same["source_id"], str(hits))
        with self.assertRaises(ValueError):
            store.read(same["source_id"], exclude_task_ids=["current-task"])

    def test_dedup_update_expiry_and_invalidation(self):
        source = self.put()
        updated = self.put(body="Updated generator advice")
        self.assertEqual(source["source_id"], updated["source_id"])
        self.assertNotEqual(source["content_sha256"], updated["content_sha256"])
        self.assertEqual(self.store.stats()["total"], 1)
        expired = self.put(title="Expired", source_ref="old", ttl_days=0)
        self.assertEqual(self.store.read(expired["source_id"])["status"], "expired")
        self.assertEqual(len(self.store.search("generator")["hits"]), 1)
        self.store.invalidate(source["source_id"], "superseded by a new API")
        self.assertEqual(self.store.search("generator")["hits"], [])
        self.assertFalse(self.store.read(source["source_id"])["verified"])

    def test_unverified_records_are_opt_in_and_vectors_are_hybrid(self):
        store = KnowledgeStore(self.path, "project-a", FakeEmbedder())
        relevant = self.put(store)
        self.put(store, title="Sockets", body="TCP timeout", source_ref="tcp")
        self.put(store, title="Agent guess", body="one-shot might work", source_ref="guess", verified=False)
        hits = store.search("single traversal")
        self.assertEqual(hits["method"], "fts5_bm25+cosine_rrf")
        self.assertEqual(hits["hits"][0]["source_id"], relevant["source_id"])
        self.assertIsNone(hits["hits"][0]["lexical_rank"])
        self.assertEqual(store.stats()["embedded"], 3)
        self.assertEqual(len(store.search("one-shot", verified_only=False)["hits"]), 2)

    def test_embedding_failure_preserves_lexical_search_and_can_be_backfilled(self):
        class Broken:
            identity = "test:bad"
            def embed(self, text):
                raise TimeoutError("possibly sensitive diagnostics")
        broken = KnowledgeStore(self.path, "project-a", Broken())
        source = self.put(broken)
        self.assertEqual(source["embedding_error"], "TimeoutError")
        self.assertEqual(broken.search("generator")["embedding_error"], "TimeoutError")
        indexed = KnowledgeStore(self.path, "project-a", FakeEmbedder()).index_pending()
        self.assertEqual(indexed["indexed"], 1)
        self.assertEqual(indexed["failed"], 0)

    def test_invalid_bounds_vectors_and_missing_verification(self):
        for vector in ([math.nan], [0, 0], [True], [math.inf], "text"):
            with self.assertRaises(ValueError):
                _vector(vector)
        with self.assertRaises(ValueError):
            self.put(verification=None)
        with self.assertRaises(ValueError):
            self.put(body="x" * 32769)
        with self.assertRaises(ValueError):
            self.store.read("k" + "0" * 32, limit=4001)

    def test_cross_process_writes_are_atomic_and_fts_has_no_duplicates(self):
        code = "from delivery_agent.knowledge import KnowledgeStore; import sys; " \
               "s=KnowledgeStore(sys.argv[1],'project-a'); " \
               "[s.put(title='Shared',body='generator '+str(i),source_type='curated',source_ref='shared') for i in range(8)]"
        with ThreadPoolExecutor(max_workers=3) as pool:
            completed = list(pool.map(lambda _: subprocess.run([sys.executable, "-c", code, str(self.path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60), range(3)))
        self.assertTrue(all(r.returncode == 0 for r in completed), [r.stderr for r in completed])
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(len(self.store.search("generator", verified_only=False)["hits"]), 1)

    def artifacts(self, verified=True):
        directory = self.root / "run"
        directory.mkdir(exist_ok=True)
        task = RepoTask.parse({"instance_id": "owner__repo-1", "repo": "owner/repo", "base_commit": "a" * 40,
            "image": "python:3.12-slim", "problem_statement": "Handle generator exhaustion",
            "public_checks": ["python -m unittest"]})
        spec = {"task": asdict(task), "task_hash": task.fingerprint(), "config": {"model": "model"},
                "implementation_sha256": "b" * 64, "upstream_version": "2.4.6", "upstream_commit": "c" * 40}
        patch = "diff --git a/test.py b/test.py\n+fixed generator\n"
        result = {"instance_id": task.instance_id, "provider": "litellm", "fixture": False,
                  "status": "submitted", "exit_status": "Submitted", "patch_valid": True, "model_patch": patch,
                  "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(), "provenance": spec,
                  "public_checks": [{"command": "python -m unittest", "passed": verified,
                                     "returncode": 0 if verified else 1, "output": "Ran regression checks"}],
                  "official_resolved": True, "submission": "Agent claims everything is fixed"}
        for filename, content in (("spec.json", spec), ("result.json", result)):
            (directory / filename).write_text(json.dumps(content), encoding="utf-8")
        (directory / "model.patch").write_text(patch, encoding="utf-8", newline="\n")
        return directory, result

    def test_ingest_only_promotes_matched_independent_checks_not_agent_claims(self):
        directory, result = self.artifacts()
        record = self.store.ingest_run(directory)
        self.assertTrue(record["verified"])
        self.assertEqual(record["verification"]["kind"], "independent_public_checks")
        self.assertIsNone(record["verification"]["official_resolved"])
        self.assertNotIn("Agent claims", self.store.read(record["source_id"], limit=4000)["text"])
        directory, result = self.artifacts(verified=False)
        unverified = self.store.ingest_run(directory)
        self.assertFalse(unverified["verified"])
        result["public_checks"][0]["command"] = "echo fake"
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        self.assertFalse(self.store.ingest_run(directory)["verified"])

    def test_ingest_rejects_patch_mismatch_and_spec_mismatch(self):
        directory, result = self.artifacts()
        (directory / "model.patch").write_text("different", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.ingest_run(directory)
        directory, result = self.artifacts()
        result["provenance"]["config"]["model"] = "different"
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.ingest_run(directory)


class OllamaHTTPTests(unittest.TestCase):
    def test_real_http_protocol_digest_dimension_and_mutable_tag_detection(self):
        class Handler(BaseHTTPRequestHandler):
            digest = "d" * 64
            def log_message(self, *args):
                pass
            def send(self, data):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                self.send({"models": [{"name": "bge-m3:latest", "digest": self.digest}]})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.last_body = body
                self.send({"model": "bge-m3", "embeddings": [[3, 4]], "prompt_eval_count": 3})
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            embedder = OllamaEmbedder(base_url=f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(embedder.embed("generator"), [0.6, 0.8])
            self.assertFalse(server.last_body["truncate"])
            self.assertEqual(embedder.metadata()["dimension"], 2)
            self.assertIn("d" * 64, embedder.identity)
            Handler.digest = "e" * 64
            with self.assertRaises(ValueError):
                embedder.embed("new query")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
