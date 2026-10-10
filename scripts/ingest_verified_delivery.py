"""Import one externally verified source delivery into a fresh knowledge store.

The operator must first authenticate/download the trusted verifier artifacts.
Hashes bind their contents; they are not signatures. This importer never runs a
model, executes repository code, edits original results, or invents internal tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.knowledge import KnowledgeStore, MAX_CONTENT_CHARS
from delivery_agent.repository import RepoTask
from scripts.grade_experiments import load_plan

SHA = re.compile(r"[0-9a-f]{64}")
FROZEN_NAMES = ("more-itertools-first-factory.json", "more-itertools-last-factory.json",
                "more-itertools-split-after.json")
EXPECTED_ERRORS = {"AssertionError", "TypeError", "UnicodeDecodeError", "AttributeError", "KeyError"}


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_json(path, cap=2 * 1024 * 1024):
    path = Path(path).absolute()
    require(not any(parent.is_symlink() for parent in (path, *path.parents)), "symlink artifact")
    require(path.is_file() and path.stat().st_size <= cap, "missing or oversized JSON artifact")
    raw = path.read_bytes()
    require(len(raw) <= cap, "oversized JSON artifact")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
    require(isinstance(value, dict), "JSON artifact must be an object")
    return value, raw


def frozen_tasks():
    suite = ROOT / "examples/delivery-suite-v1"
    manifest, _ = read_json(suite / "manifest.json", 256 * 1024)
    result = []
    for name in FROZEN_NAMES:
        value, raw = read_json(suite / name, 256 * 1024)
        task = RepoTask.parse(value)
        rows = [row for row in manifest["tasks"] if row.get("path") == name]
        require(len(rows) == 1 and rows[0].get("sha256") == digest(raw)
                and rows[0].get("fingerprint") == task.fingerprint()
                and rows[0].get("instance_id") == task.instance_id, "frozen task differs from suite manifest")
        result.append((task, digest(raw)))
    return result


def _public_content(value):
    sensitive = re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{16,}"
        r"|gh[pousr]_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._~+/-]{12,}"
        r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:AKIA|ASIA)[A-Z0-9]{16}", re.I)
    normalized = value.replace("\\", "/").casefold()
    require(not sensitive.search(value) and str(ROOT).replace("\\", "/").casefold() not in normalized
            and "/home/runner/" not in normalized and not re.search(r"[a-z]:/users/", normalized),
            "knowledge source contains private or credential content")


def prepare_record(manifest_path, verification_path, guard_path):
    """Validate trusted, hash-bound artifacts before creating any database."""
    manifest, manifest_raw = read_json(manifest_path, 256 * 1024)
    runs = load_plan(manifest_path)
    require(len(runs) == 1, "source import requires exactly one declared run")
    run = runs[0]
    task, evidence, patch = run["task"], run["evidence"], run["patch"]
    source, *targets = frozen_tasks()
    require(task.fingerprint() == source[0].fingerprint(), "source is not the frozen first-factory requirement")
    require(evidence.get("provider") == "codex_cli" and evidence.get("inference_backend") == "files"
            and evidence.get("fixture") is False and evidence.get("status") == "submitted"
            and evidence.get("exit_status") == "Submitted" and type(evidence.get("patch_valid")) is bool
            and evidence.get("task_hash") == task.fingerprint() and bool(patch),
            "source requires a genuine submitted files-backend delivery")
    # The shared grader validates paths and model identity; additionally reject
    # ambiguous JSON documents before treating their hashes as stable evidence.
    for item in manifest["runs"]:
        read_json(Path(manifest_path).resolve().parent / item["task"], 256 * 1024)
        read_json(Path(manifest_path).resolve().parent / item["evidence"], 512 * 1024)
    report, report_raw = read_json(verification_path)
    manifest_sha, report_sha, patch_sha = digest(manifest_raw), digest(report_raw), digest(patch)
    require(type(report.get("schema")) is int and report["schema"] == 1
            and report.get("kind") == "independent_public_repository_verification"
            and report.get("manifest_sha256") == manifest_sha and report.get("infrastructure_failures") == []
            and "infrastructure_error_type" not in report, "independent verification is absent or unbound")
    for field in ("attempts", "verified_attempts", "submitted", "delivery_successes"):
        require(type(report.get(field)) is int and report[field] == 1, "source verification is incomplete")
    require(isinstance(report.get("runs"), list) and len(report["runs"]) == 1, "source report must contain one run")
    verified = report["runs"][0]
    require(isinstance(verified, dict), "source verification run must be an object")
    bindings = {"id": run["id"], "instance_id": task.instance_id, "profile": evidence["profile"],
        "provider": evidence["provider"], "model_name": evidence["model_name"], "model_digest": None,
        "model_identity": evidence["model_identity"], "model_status": "submitted", "patch_sha256": patch_sha,
        "task_sha256": run["task_sha256"], "evidence_sha256": run["evidence_sha256"]}
    require(all(key in verified and verified[key] == value for key, value in bindings.items()),
            "independent report differs from actual source artifacts")
    require(all(verified.get(key) is True for key in
                ("submitted", "patch_valid", "public_check_eligible", "delivery_success"))
            and verified.get("verification_status") == "passed"
            and verified.get("public_delivery_status") == "passed", "independent source checks did not pass")
    image = verified.get("resolved_image")
    require(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image), "verifier image identity is absent")
    checks = verified.get("public_checks")
    require(isinstance(checks, list) and len(checks) == len(task.public_checks) > 0,
            "independent public checks are incomplete")
    for check, command in zip(checks, task.public_checks):
        require(isinstance(check, dict) and check.get("command") == command and check.get("passed") is True
                and type(check.get("returncode")) is int and check["returncode"] == 0
                and isinstance(check.get("output_sha256"), str) and SHA.fullmatch(check["output_sha256"])
                and isinstance(check.get("output_preview"), str) and len(check["output_preview"]) <= 4000,
                "independent check differs from a complete passing frozen command")
    guard, guard_raw = read_json(guard_path)
    guard_bindings = {"manifest_sha256": manifest_sha, "verification_sha256": report_sha,
        "source_run_id": run["id"], "source_task_sha256": source[1],
        "source_evidence_sha256": run["evidence_sha256"], "source_patch_sha256": patch_sha, "resolved_image": image}
    require(type(guard.get("schema")) is int and guard["schema"] == 1
            and guard.get("kind") == "source_seed_leakage_guard"
            and all(guard.get(key) == value for key, value in guard_bindings.items()), "source guard is unbound")
    require(guard.get("eligible") is True and guard.get("violations") == []
            and type(guard.get("omitted_violations")) is int and guard["omitted_violations"] == 0,
            "source static leakage guard failed")
    static = guard.get("targets")
    require(isinstance(static, list) and len(static) == 2 and all(isinstance(row, dict) for row in static)
            and {row.get("function") for row in static} == {"last", "split_after"}, "protected target definitions are missing")
    for row in static:
        require(isinstance(row.get("old_ast_sha256"), str) and SHA.fullmatch(row["old_ast_sha256"])
                and row["old_ast_sha256"] == row.get("new_ast_sha256"), "source changed a protected target")
    functional = guard.get("functional_targets")
    require(isinstance(functional, list) and len(functional) == 2
            and all(isinstance(row, dict) for row in functional), "target feature checks are missing")
    for target, task_sha in targets:
        matched = [row for row in functional if row.get("instance_id") == target.instance_id]
        require(len(matched) == 1, "target feature check is missing or repeated")
        row = matched[0]
        require(row.get("task_sha256") == task_sha and row.get("command_sha256") == digest(target.public_checks[-1])
                and row.get("target_feature_still_missing") is True
                and type(row.get("returncode")) is int and 0 < row["returncode"] <= 255
                and isinstance(row.get("output_sha256"), str) and SHA.fullmatch(row["output_sha256"])
                and isinstance(row.get("exception_types"), list) and bool(row["exception_types"])
                and all(isinstance(name, str) and name in EXPECTED_ERRORS for name in row["exception_types"]),
                "source does not preserve a frozen target feature gap")
    identity = {"run_id": run["id"], "task_sha256": run["task_sha256"],
                "evidence_sha256": run["evidence_sha256"], "patch_sha256": patch_sha}
    run_sha = digest(canonical(identity))
    verification = {"schema": 1, "kind": "external_independent_public_checks", "verification_backend": "external_docker",
        **identity, "run_sha256": run_sha, "manifest_sha256": manifest_sha, "verification_sha256": report_sha,
        "guard_sha256": digest(guard_raw), "frozen_source_task_sha256": source[1],
        "patch_valid": True, "source_patch_valid": evidence["patch_valid"],
        "run_status": "submitted", "internal_verification_status": "deferred",
        "resolved_image": image, "model_identity": evidence["model_identity"],
        "checks": [{key: row[key] for key in ("command", "returncode", "passed", "output_sha256")} for row in checks],
        "official_resolved": None, "trust_boundary": "operator-authenticated verifier artifacts; hashes are not signatures"}
    body = ("Task requirement (public):\n" + task.problem_statement[:6000]
        + "\n\nIndependent public check output previews (bounded):\n"
        + "\n".join(row["output_preview"][:1500] for row in checks)
        + "\n\nDelivered patch (excerpt):\n" + patch.decode("utf-8")[:18000])[:MAX_CONTENT_CHARS]
    _public_content(body)
    record = {"title": task.instance_id + ": repository delivery", "body": body, "source_type": "repository_run",
        "source_ref": "run:" + run_sha, "repo": task.repo, "base_commit": task.base_commit,
        "task_id": task.instance_id, "verified": True, "verification": verification}
    require(len(json.dumps(verification, ensure_ascii=False, sort_keys=True)) <= 12000,
            "verification metadata exceeds the knowledge store budget")
    return record


def ingest_verified_delivery(manifest, verification, guard, database, namespace, output_package):
    """Create a new database/package only after all source gates pass."""
    record = prepare_record(manifest, verification, guard)
    database, output_package = Path(database).absolute(), Path(output_package).absolute()
    require(database != output_package and not database.exists() and not output_package.exists(),
            "database and package must be fresh distinct paths")
    require(not any(path.is_symlink() for path in (database, output_package, *database.parents, *output_package.parents)),
            "symlink output is prohibited")
    require(not any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")),
            "database sidecar already exists")
    database.parent.mkdir(parents=True, exist_ok=True)
    output_package.parent.mkdir(parents=True, exist_ok=True)
    with database.open("xb"):
        pass
    store = KnowledgeStore(database, namespace)
    metadata = store.put(**record, ttl_days=None)
    store.close()
    origin = {key: metadata[key] for key in
              ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref")}
    origin["verification_sha256"] = digest(canonical(record["verification"]))
    package = {"schema": 1, "kind": "external_verified_delivery_seed", "fixture": False, "eligible": True,
        "record": record, "origin": origin, "internal_checks_passed": None, "external_checks_passed": True,
        "limitations": ["Source-only internal checks remain deferred.",
            "Operator authenticates verifier artifacts; hashes alone do not establish trust.",
            "A verified source record does not prove target retrieval, use, or benefit."]}
    with output_package.open("xb") as handle:
        handle.write((json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    return package


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "verification", "guard", "database", "namespace", "output-package"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        package = ingest_verified_delivery(args.manifest, args.verification, args.guard, args.database,
                                           args.namespace, args.output_package)
    except Exception as exc:
        print(json.dumps({"eligible": False, "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"eligible": True, "origin": package["origin"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
