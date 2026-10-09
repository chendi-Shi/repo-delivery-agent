"""Download a pinned official dataset and disclose only three public tasks.

Requires pyarrow for Parquet. Raw dataset and private grading JSON remain in the
ignored data directory. Selection uses only repository and public issue length.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.benchmark import freeze_dataset, load_manifest

DATASET = "SWE-bench/SWE-bench_Verified"
REVISION = "03e151cf5560b1af6a4363c6a9d766deaaea6b56"


def get(url, cap=32 * 1024 * 1024):
    with urllib.request.urlopen(url, timeout=120) as response:
        parts, size, started = [], 0, time.monotonic()
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            if size > cap or time.monotonic() - started > 600:
                raise ValueError("download exceeds size or total time bound")
    return b"".join(parts)


def main():
    import pyarrow.parquet as parquet
    root = Path(__file__).resolve().parents[1]
    data = root / "data" / "official-smoke"
    data.mkdir(parents=True, exist_ok=True)
    meta_url = f"https://huggingface.co/api/datasets/{DATASET}/revision/{REVISION}"
    metadata = json.loads(get(meta_url))
    if metadata.get("sha") != REVISION:
        raise ValueError("remote dataset revision mismatch")
    tree_url = f"https://huggingface.co/api/datasets/{DATASET}/tree/{REVISION}?recursive=true"
    tree = json.loads(get(tree_url))
    candidates = [r for r in tree if r.get("path", "").endswith(".parquet") and "test" in r["path"]]
    if len(candidates) != 1:
        raise ValueError("expected one pinned test parquet")
    item = candidates[0]
    print(json.dumps({"phase": "pinned_metadata_verified", "path": item["path"],
                      "bytes": item.get("size"), "lfs": item.get("lfs", {}).get("oid")}), flush=True)
    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{item['path']}"
    target = data / "verified-test.parquet"
    if not target.exists():
        target.write_bytes(get(url + "?download=true"))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    expected = item.get("lfs", {}).get("oid")
    if expected and digest != expected:
        raise ValueError("downloaded parquet does not match pinned LFS SHA-256")
    rows = parquet.read_table(target).to_pylist()
    # Private answers never appear in printed output or committed examples.
    source = data / "grading-source.json"
    source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    groups = {}
    for repo in ("psf/requests", "pallets/flask", "more-itertools/more-itertools"):
        groups[repo] = sorted((r for r in rows if r["repo"] == repo),
                             key=lambda r: (len(r["problem_statement"]), r["instance_id"]))
    chosen = [group[0] for group in groups.values() if group]
    if len(chosen) < 3:
        selected = {r["instance_id"] for r in chosen}
        extra = sorted((r for group in groups.values() for r in group if r["instance_id"] not in selected),
                       key=lambda r: (len(r["problem_statement"]), r["instance_id"]))
        chosen += extra[:3 - len(chosen)]
    if len(chosen) != 3:
        raise ValueError("fewer than three preferred repository tasks")
    output = root / "examples" / "official-smoke"
    selected_ids = [r["instance_id"] for r in chosen]
    if (output / "manifest.json").exists():
        existing = load_manifest(output / "manifest.json")
        snapshot = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
        if ([r["instance_id"] for r in existing] != selected_ids or snapshot.get("dataset_revision") != REVISION
                or snapshot.get("snapshot_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()):
            raise ValueError("existing frozen examples disagree with the pinned private source")
        frozen = snapshot
    else:
        frozen = freeze_dataset(source, output, DATASET, REVISION, instance_ids=selected_ids)
    public = load_manifest(output / "manifest.json")
    for task in public:
        # Prefer the exact image supplied by the current official dataset.
        repository_task = {k: v for k, v in task.items() if k != "version"}
        original = next(r for r in rows if r["instance_id"] == task["instance_id"])
        image = original.get("image") or "swebench/sweb.eval.x86_64." + task["instance_id"].replace("__", "_1776_").lower() + ":latest"
        repository_task.update(image=image,
                               public_checks=[])
        (output / (task["instance_id"] + ".json")).write_text(
            json.dumps(repository_task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence = {"dataset": DATASET, "revision": REVISION, "revision_verified_remotely": True,
        "parquet_url": url, "parquet_sha256": digest, "lfs_sha256": expected,
        "record_count": len(rows), "selection": "preferred repositories, then shortest public problem statements; no gold consulted",
        "instance_ids": [t["instance_id"] for t in public],
        "preferred_repo_counts": {repo: len(group) for repo, group in groups.items()},
        "manifest_sha256": frozen["manifest_sha256"], "private_grading_source": "data/official-smoke/grading-source.json",
        "answers_committed": False}
    evidence["grading_metadata"] = [{"instance_id": r["instance_id"], "image": r.get("image"),
        "eval_script_sha256": hashlib.sha256(str(r.get("eval_script", "")).encode()).hexdigest(),
        "has_v5_grading_fields": all(k in r for k in ("image", "eval_script", "log_parser", "eval_type"))}
        for r in chosen]
    (output / "source.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
