"""Merge independently executed real-model shards without importing trajectories."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.repository import atomic_json
from grade_experiments import artifact, load_plan


def merge(shards, output, *, expected_runs=6):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Collector output must be fresh")
    manifests = sorted(Path(shards).resolve().glob("*/repository-ablation.json"))
    if len(manifests) != expected_runs:
        raise ValueError("Missing experiment shard manifest")
    output.mkdir(parents=True)
    plan = {"schema": 1, "kind": "real_model_repository_ablation", "runs": [], "limitations": []}
    seen = set()
    for manifest in manifests:
        loaded = load_plan(manifest)
        if len(loaded) != 1:
            raise ValueError("Each shard must contain exactly one actual attempt")
        original = json.loads(manifest.read_text(encoding="utf-8"))
        for value in original.get("limitations", []):
            if value not in plan["limitations"]:
                plan["limitations"].append(value)
        for item in original["runs"]:
            if item["id"] in seen:
                raise ValueError("Duplicate immutable run ID across shards")
            seen.add(item["id"])
            destination = output / "model-runs" / item["id"]
            destination.mkdir(parents=True)
            merged = {"id": item["id"]}
            for field, name, limit in (("task", "task.json", 256 * 1024),
                                       ("patch", "model.patch", 8 * 1024 * 1024),
                                       ("evidence", "evidence.json", 512 * 1024)):
                source = artifact(manifest.parent, item[field], max_bytes=limit)
                shutil.copyfile(source, destination / name)
                merged[field] = f"model-runs/{item['id']}/{name}"
            plan["runs"].append(merged)
        # Only explicitly named bounded setup provenance joins the public artifact.
        setup = manifest.parent / "ci-environment.json"
        if setup.is_file() and not setup.is_symlink() and setup.stat().st_size <= 128 * 1024:
            provenance = json.loads(setup.read_text(encoding="utf-8"))
            atomic_json(output / ("ci-environment-" + loaded[0]["id"] + ".json"), provenance)
    atomic_json(output / "repository-ablation.json", plan)
    load_plan(output / "repository-ablation.json")
    return {"attempts": len(plan["runs"]), "shards": len(manifests)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-runs", type=int, default=6)
    args = parser.parse_args(argv)
    if not 1 <= args.expected_runs <= 200:
        parser.error("Expected runs must be 1..200")
    print(json.dumps(merge(args.shards, args.output, expected_runs=args.expected_runs)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
