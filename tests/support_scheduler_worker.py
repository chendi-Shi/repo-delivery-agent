"""Trusted subprocess executor for actual HTTP integration, never selected by jobs."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from delivery_agent.scheduler_worker import Client, Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--mode", choices=["run", "crash", "cancel"], default="run")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.1)
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    client = Client(args.url, os.environ["DELIVERY_SCHEDULER_TOKEN"], timeout=3)
    if args.mode == "crash":
        client.register(args.id)
        claim = client.claim(args.id)
        (root / "crashed-claim.json").write_text(json.dumps(claim), encoding="utf-8")
        return 0

    def trusted_executor(claim, directory, cancelled, local_options):
        directory.mkdir(parents=True)
        (root / (claim["job_id"] + ".started")).write_text(str(os.getpid()), encoding="utf-8")
        started = time.monotonic()
        while time.monotonic() - started < args.delay:
            if cancelled.wait(0.02):
                (root / (claim["job_id"] + ".cancelled")).write_text("observed", encoding="utf-8")
                return {"outcome": "failed", "result": {"cancelled": True}, "patch": "", "evidence": {}}
        patch = "diff --git a/result.txt b/result.txt\n+" + claim["job_id"] + "\n"
        return {"outcome": "succeeded", "result": {"pid": os.getpid(), "worker_id": args.id,
                "generation": claim["generation"]}, "patch": patch, "evidence": {"trace.json": "a" * 64}}

    worker = Worker(client, args.id, root / "attempts", executor=trusted_executor, poll_seconds=0.05)
    results = worker.run(max_jobs=args.jobs, idle_seconds=10)
    (root / "commits.json").write_text(json.dumps(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
