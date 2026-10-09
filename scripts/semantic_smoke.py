"""Real Ollama/BGE smoke on independent public documentation, never task answers.

This is a small functional retrieval check, not a model-quality benchmark or CI
grading result. Knowledge records stay unverified and search explicitly opts in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.knowledge import KnowledgeStore, OllamaEmbedder

CORPUS = [
    {"title": "Iterator protocol", "source_ref": "https://docs.python.org/3/library/stdtypes.html#iterator-types",
     "task_id": "prior-iterator-protocol",
     "body": "Iterators yield successive elements through __next__. StopIteration signals exhaustion. Iteration resumes from retained position; consumed generators cannot rewind."},
    {"title": "Socket networking", "source_ref": "https://docs.python.org/3/library/socket.html",
     "task_id": "prior-socket-networking",
     "body": "TCP sockets transport bytes between peers. Connection establishment, receive operations and timeouts require handling network errors."},
    {"title": "SQLite transactions", "source_ref": "https://www.sqlite.org/lang_transaction.html",
     "task_id": "prior-sqlite-transactions",
     "body": "SQLite permits concurrent readers but serializes writers. COMMIT finalizes changes; ROLLBACK cancels pending modifications. Busy conflicts need deliberate retry handling."},
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="bge-m3")
    parser.add_argument("--output", default="experiments/semantic-search.json")
    args = parser.parse_args(argv)
    started = time.monotonic()
    outcome = {"schema_version": 1, "kind": "real_embedding_functional_smoke",
        "fixture": False, "model": args.model, "success": False,
        "scope": "three independent public-document summaries; no coding-task answers",
        "knowledge_verified": False, "official_benchmark_result": False,
        "limitation": "small smoke corpus; no claimed improvement in coding-agent success rate",
        "corpus": [{"title": c["title"], "source_ref": c["source_ref"],
                    "body_sha256": hashlib.sha256(c["body"].encode()).hexdigest()} for c in CORPUS]}
    try:
        embedder = OllamaEmbedder(args.model, args.endpoint, timeout=90)
        with tempfile.TemporaryDirectory(prefix="delivery-semantic-") as temporary:
            path = Path(temporary) / "knowledge.sqlite"
            store = KnowledgeStore(path, "semantic-smoke-public-docs-v1", embedder)
            ids = {}
            for c in CORPUS:
                record = store.put(**c, source_type="public_document_summary", verified=False,
                    verification={"kind": "source_reference_only", "not_an_independent_coding_check": True})
                if record["embedding_error"]:
                    raise RuntimeError("document embedding failed")
                ids[c["title"]] = record["source_id"]
            query = "Do not scan this collection twice because it gets depleted"
            lexical = KnowledgeStore(path, "semantic-smoke-public-docs-v1").search(query, verified_only=False)
            semantic = store.search(query, verified_only=False)
            expected = ids["Iterator protocol"]
            semantic_matches = bool(semantic["hits"]) and semantic["hits"][0]["source_id"] == expected
            foreign = KnowledgeStore(path, "semantic-smoke-other-namespace")
            foreign_record = foreign.put(title="Foreign namespace sentinel", body="generator iterator protocol exhaustion",
                source_type="namespace_test", source_ref="smoke:foreign", verified=False)
            scoped = store.search("generator iterator exhaustion", verified_only=False)
            namespace_ok = foreign_record["source_id"] not in {h["source_id"] for h in scoped["hits"]}
            try:
                foreign.read(expected)
            except ValueError:
                foreign_read_blocked = True
            else:
                foreign_read_blocked = False
            excluded = store.search(query, verified_only=False, exclude_task_ids=["prior-iterator-protocol"])
            active_task_excluded = expected not in {h["source_id"] for h in excluded["hits"]}
            metadata = embedder.metadata()
            metadata.pop("endpoint", None)  # Do not publish host-specific endpoints.
            outcome.update(embedding=metadata, embedded_count=store.stats()["embedded"], query=query,
                lexical_hit_count=len(lexical["hits"]), semantic_method=semantic["method"],
                semantic_top_matches_expected=semantic_matches,
                semantic_hits=[{k: h[k] for k in ("source_id", "title", "source_ref", "lexical_rank", "semantic_rank", "rrf_score")}
                               for h in semantic["hits"]],
                namespace_isolated=namespace_ok, cross_namespace_read_blocked=foreign_read_blocked,
                active_task_excluded=active_task_excluded,
                success=bool(not lexical["hits"] and semantic_matches and namespace_ok and
                             foreign_read_blocked and active_task_excluded and not semantic["embedding_error"]))
    except Exception as exc:
        outcome["error_type"] = type(exc).__name__
    outcome["seconds"] = time.monotonic() - started
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(outcome, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    return 0 if outcome["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
