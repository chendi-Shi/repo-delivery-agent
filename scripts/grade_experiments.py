"""Verify saved real-model repository patches in fresh Docker workspaces.

This is public-check delivery verification, not official SWE-bench scoring.
Model failures stay in the public-check eligible denominator; tasks with no
public checks remain unknown. Total attempts are retained. Infrastructure
failures exit 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.repository import RepoTask, atomic_json, checkout, git
from delivery_agent.runtime import DockerSession
from delivery_agent.scheduler import IDENTIFIER, DIGEST

MAX_PATCH_BYTES = 8 * 1024 * 1024


def valid_remote_identity(evidence):
    """A recorded remote model alias identifies a service, never frozen weights."""
    identity = evidence.get("model_identity")
    return (isinstance(identity, dict)
        and set(identity) <= {"kind", "model", "cli_version", "binary_sha256", "weight_digest_available", "reasoning_effort"}
        and identity.get("kind") == "codex_cli"
        and identity.get("model") == evidence.get("model_name")
        and isinstance(identity.get("model"), str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}", identity["model"]) is not None
        and isinstance(identity.get("cli_version"), str)
        and re.fullmatch(r"codex-cli [A-Za-z0-9_.+-]{1,60}", identity["cli_version"]) is not None
        and isinstance(identity.get("binary_sha256"), str)
        and DIGEST.fullmatch(identity["binary_sha256"]) is not None
        and identity.get("weight_digest_available") is False
        and identity.get("reasoning_effort", "low") in {"low", "medium", "high", "xhigh"}
        and evidence.get("model_digest") is None)


def artifact(root, value, *, max_bytes):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise ValueError("Invalid artifact reference")
    relative = Path(value)
    if relative.is_absolute() or any(part in {"..", ".git"} for part in relative.parts):
        raise ValueError("Artifact reference must stay within experiments root")
    candidate = root / relative
    if any(p.is_symlink() for p in [candidate, *candidate.parents] if p.is_relative_to(root)):
        raise ValueError("Symlink artifacts are prohibited")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file() or resolved.stat().st_size > max_bytes:
        raise ValueError("Missing, escaped, or oversized artifact")
    return resolved


def load_plan(manifest):
    manifest = Path(manifest).resolve()
    root = manifest.parent
    if not manifest.is_file() or manifest.stat().st_size > 256 * 1024:
        raise ValueError("Missing or oversized experiment manifest")
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    if (not isinstance(plan, dict) or set(plan) - {"schema", "kind", "runs", "limitations"}
            or {"schema", "kind", "runs"} - set(plan)
            or type(plan["schema"]) is not int or plan["schema"] != 1
            or plan["kind"] != "real_model_repository_ablation"
            or not isinstance(plan["runs"], list) or not 1 <= len(plan["runs"]) <= 200):
        raise ValueError("Invalid real-model experiment manifest")
    limitations = plan.get("limitations", [])
    if (not isinstance(limitations, list) or len(limitations) > 20
            or any(not isinstance(value, str) or len(value) > 2000 for value in limitations)):
        raise ValueError("Invalid experiment limitations")
    seen, runs = set(), []
    for item in plan["runs"]:
        if (not isinstance(item, dict) or set(item) != {"id", "task", "patch", "evidence"}
                or not isinstance(item["id"], str) or not IDENTIFIER.fullmatch(item["id"])
                or item["id"] in seen):
            raise ValueError("Invalid or repeated run ID")
        seen.add(item["id"])
        task_path = artifact(root, item["task"], max_bytes=256 * 1024)
        patch_path = artifact(root, item["patch"], max_bytes=MAX_PATCH_BYTES)
        evidence_path = artifact(root, item["evidence"], max_bytes=512 * 1024)
        task = RepoTask.parse(json.loads(task_path.read_text(encoding="utf-8")))
        patch = patch_path.read_bytes()
        patch.decode("utf-8", "strict")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        required = {"fixture", "provider", "status", "exit_status", "profile", "model_name", "model_digest",
                    "patch_sha256", "usage", "seconds", "context_requests", "verification_status"}
        if not isinstance(evidence, dict) or required - set(evidence):
            raise ValueError("Incomplete real-model evidence")
        requests = evidence["context_requests"]
        valid_requests = ((type(requests) is int and 0 <= requests <= 10000)
                          or (isinstance(requests, list) and len(requests) <= 1000
                              and len(json.dumps(requests, allow_nan=False).encode()) <= 256 * 1024))
        digest = evidence["model_digest"]
        provider = evidence["provider"]
        valid_identity = ((isinstance(digest, str) and bool(digest)) or (
            digest is None and evidence["status"] in {"failed", "stopped"})) if provider == "ollama_native" else (
            valid_remote_identity(evidence) if provider == "codex_cli" else False)
        if (evidence["fixture"] is not False or provider not in {"ollama_native", "codex_cli"}
                or evidence["profile"] not in {"baseline", "recent", "retrieval"}
                or evidence["status"] not in {"submitted", "stopped", "failed"}
                or not isinstance(evidence["model_name"], str) or not evidence["model_name"]
                or not valid_identity
                or not isinstance(evidence["patch_sha256"], str) or not DIGEST.fullmatch(evidence["patch_sha256"])
                or evidence["patch_sha256"] != hashlib.sha256(patch).hexdigest()
                or type(evidence["seconds"]) not in {int, float} or not math.isfinite(evidence["seconds"])
                or evidence["seconds"] < 0 or not (isinstance(evidence["usage"], dict) or
                    (evidence["usage"] is None and evidence["status"] in {"failed", "stopped"}))
                or not valid_requests
                or evidence["verification_status"] != "deferred"):
            raise ValueError("Invalid or mismatched real-model evidence")
        if evidence["status"] == "submitted" and evidence["exit_status"] != "Submitted":
            raise ValueError("Submitted status lacks actual upstream exit protocol")
        runs.append({"id": item["id"], "task": task, "patch": patch, "evidence": evidence,
                     "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
                     "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()})
    return runs


def verify_one(run, *, directory, command_timeout=90, session_factory=DockerSession, checkout_factory=checkout):
    task, evidence, patch = run["task"], run["evidence"], run["patch"]
    eligible = bool(task.public_checks)
    record = {"id": run["id"], "instance_id": task.instance_id, "profile": evidence["profile"],
        "model_name": evidence["model_name"], "model_digest": evidence["model_digest"],
        "model_identity_status": ("remote_alias_not_weight_pinned" if evidence["provider"] == "codex_cli"
                                  else "resolved" if evidence["model_digest"] else "unresolved"),
        "provider": evidence["provider"], "model_identity": evidence.get("model_identity"),
        "model_status": evidence["status"], "submitted": evidence["status"] == "submitted",
        "patch_sha256": evidence["patch_sha256"], "task_sha256": run["task_sha256"],
        "evidence_sha256": run["evidence_sha256"], "patch_valid": False,
        "public_checks": [], "public_check_eligible": eligible,
        "delivery_success": False if eligible else None,
        "public_delivery_status": "pending" if eligible else "unknown_no_public_checks",
        "official_resolved": None}
    work = checkout_factory(task, Path(directory) / "workspace")
    if patch:
        applicable = git(work, "apply", "--check", "--whitespace=nowarn", "-", input=patch, check=False)
        if applicable.returncode:
            record["verification_status"] = "patch_invalid"
            if eligible:
                record["public_delivery_status"] = "failed"
            return record
        git(work, "apply", "--whitespace=nowarn", "-", input=patch)
    record["patch_valid"] = True
    if not task.public_checks:
        record["verification_status"] = "no_public_checks"
        return record
    with session_factory(work, task.image, timeout=command_timeout) as env:
        record["resolved_image"] = env.image_id
        for command in task.public_checks:
            try:
                result = env.execute({"command": command})
                raw = result.get("output", "")
                record["public_checks"].append({"command": command, "returncode": result["returncode"],
                    "passed": result["returncode"] == 0, "output_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    "output_preview": raw[:4000]})
            except RuntimeError as exc:
                # A confirmed command budget stop is a failed public check. A
                # cleanup/preflight failure must remain an infrastructure failure.
                if str(exc) not in {"Command timeout; session stopped", "Output limit exceeded; session stopped"}:
                    raise
                record["public_checks"].append({"command": command, "passed": False,
                    "returncode": None, "budget_exhausted": True})
                break
    passed = (len(record["public_checks"]) == len(task.public_checks)
              and all(check["passed"] for check in record["public_checks"]))
    record["verification_status"] = "passed" if passed else "failed"
    record["delivery_success"] = record["submitted"] and passed
    record["public_delivery_status"] = "passed" if record["delivery_success"] else "failed"
    return record


def grade(manifest, output, *, pull_images=False, command_timeout=90, verifier=verify_one):
    runs = load_plan(manifest)
    records, failures = [], []
    if pull_images:
        for image in sorted({run["task"].image for run in runs if run["task"].public_checks}):
            try:
                subprocess.run(["docker", "pull", image], check=True, capture_output=True, timeout=300)
            except Exception as exc:
                failures.append({"stage": "image_pull", "error_type": type(exc).__name__})
                break
    if not failures:
        for run in runs:
            try:
                with tempfile.TemporaryDirectory(prefix="delivery-verify-") as directory:
                    records.append(verifier(run, directory=directory, command_timeout=command_timeout))
            except Exception as exc:
                failures.append({"id": run["id"], "stage": "independent_verification", "error_type": type(exc).__name__})
    count = len(runs)
    def metrics(attempts, verified):
        eligible_ids = {run["id"] for run in attempts if run["task"].public_checks}
        eligible_verified = [row for row in verified if row["id"] in eligible_ids]
        known = [row for row in eligible_verified if type(row["delivery_success"]) is bool]
        successes = sum(row["delivery_success"] is True for row in known)
        unknown_eligible = len(eligible_ids) - len(known)
        public_rate = successes / len(eligible_ids) if eligible_ids and not unknown_eligible else None
        return {"public_check_eligible_attempts": len(eligible_ids),
                "public_check_eligible_verified_attempts": len(eligible_verified),
                "no_public_checks_attempts": len(attempts) - len(eligible_ids),
                "public_delivery_denominator": len(eligible_ids),
                "public_delivery_unknown_attempts": len(attempts) - len(known),
                "public_delivery_rate": public_rate,
                "public_delivery_failures": sum(row["delivery_success"] is False for row in known),
                "delivery_successes": successes,
                # Preserve the old interpretation only for fully eligible
                # experiments. Mixed/no-check inputs have no all-attempt public
                # outcome, so callers must use the explicit eligible rate.
                "delivery_rate_all_attempts": public_rate if len(eligible_ids) == len(attempts) else None}
    overall = metrics(runs, records)
    by_profile = {}
    for profile in sorted({run["evidence"]["profile"] for run in runs}):
        attempts = [run for run in runs if run["evidence"]["profile"] == profile]
        verified = [row for row in records if row["profile"] == profile]
        by_profile[profile] = {"attempts": len(attempts), "verified_attempts": len(verified),
            "submitted": sum(run["evidence"]["status"] == "submitted" for run in attempts),
            **metrics(attempts, verified)}
    report = {"schema": 1, "kind": "independent_public_repository_verification",
        "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
        "attempts": count, "verified_attempts": len(records),
        "submitted": sum(run["evidence"]["status"] == "submitted" for run in runs),
        **overall,
        "by_profile": by_profile,
        "official_resolved": None, "runs": records, "infrastructure_failures": failures}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="experiments/repository-ablation.json")
    parser.add_argument("--output", default="runs/repository-verification.json")
    parser.add_argument("--pull-images", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=90)
    args = parser.parse_args(argv)
    if not 1 <= args.command_timeout <= 600:
        parser.error("Command timeout must be 1..600 seconds")
    try:
        report = grade(args.manifest, args.output, pull_images=args.pull_images, command_timeout=args.command_timeout)
    except Exception as exc:
        failure = {"schema": 1, "kind": "independent_public_repository_verification",
                   "infrastructure_error_type": type(exc).__name__, "delivery_rate_all_attempts": None,
                   "public_delivery_rate": None, "public_delivery_denominator": None}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output, failure)
        print(json.dumps({"infrastructure_error_type": type(exc).__name__}))
        return 2
    print(json.dumps({k: report[k] for k in ("attempts", "submitted", "public_delivery_denominator", "no_public_checks_attempts",
                                             "delivery_successes", "public_delivery_rate", "delivery_rate_all_attempts")},
                     ensure_ascii=False))
    return 2 if report["infrastructure_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
