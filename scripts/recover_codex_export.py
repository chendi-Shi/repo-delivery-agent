"""Recover a failed public export from immutable local results, without inference.

The original summary/input stay byte-for-byte in place. A hash-bound receipt
selects recovered-summary.json only after every new artifact has been staged.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.repository import RepoTask, atomic_json
from scripts import run_codex_trial as exporter
from scripts.grade_experiments import artifact, load_plan
from scripts.ingest_verified_delivery import canonical, digest, require

RECEIPT = "export-recovery.json"
RECOVERED = "recovered-summary.json"
COUNTERS = ("context_requests", "attempt_started", "attempted_count")
DERIVED = ("action_audit", "knowledge_observation", "export_recovery")
PRIVATE_INPUTS = {"result": "result.json", "spec": "spec.json", "patch": "model.patch",
    "request_audit": "agent/request-views.jsonl", "action_audit": "agent/action-audit.jsonl",
    "checkpoint": "agent/checkpoint.json"}


def _helpers():
    from scripts import summarize_codex_transfer
    return summarize_codex_transfer


def _read(path, maximum=16 * 1024 * 1024):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum,
            "recovery input is missing, linked, or oversized")
    return path.read_bytes()


def _private_snapshot(trial):
    require(not any(path.is_symlink() for path in (trial, *trial.parents)), "linked private recovery input")
    result = {}
    for name, relative in PRIVATE_INPUTS.items():
        path = trial / relative
        require(not any(part.is_symlink() for part in (path, *path.parents)), "linked recovery input")
        if not path.exists() and name in {"action_audit", "checkpoint"}:
            result[name] = None
        else:
            raw = _read(path)
            result[name] = {"sha256": digest(raw), "bytes": len(raw)}
    return result


def _same(left, right, message):
    require(canonical(left) == canonical(right), message)


def _preflight(root, label, trial):
    helper = _helpers()
    plan, plan_sha, planned = helper._plan(root)
    wanted = next((row for row in planned if row["label"] == label), None)
    require(wanted is not None, "recovery label is not a frozen target")
    public = root / "targets" / label
    require(public.is_dir() and not public.is_symlink(), "original public directory is missing or linked")
    require({path.name for path in public.iterdir()} == {"summary.json", "knowledge-input.json"},
            "recovery requires an untouched failed export with no existing generated artifacts")
    original, original_raw = helper._json(public / "summary.json")
    package, package_raw = helper._json(public / "knowledge-input.json")
    require(original.get("schema") == 1 and original.get("kind") == "real_codex_source_trial"
            and original.get("fixture") is False and original.get("label") == label
            and original.get("profile") == wanted["profile"] and original.get("provider") == plan["provider"]
            and original.get("model_name") == plan["model"]
            and original.get("source_task_sha256") == wanted["frozen_task_sha256"]
            and original.get("source_task_fingerprint") == wanted["task"].fingerprint()
            and exporter.safe_error_type(original.get("export_error_type")) is not None,
            "original failed export differs from its frozen task/model/label")
    require(type(original.get("source_tracked_tree_clean")) is bool
            and isinstance(original.get("source_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", original["source_commit"]), "original source provenance is incomplete")
    helper._budgets(original.get("budgets"), plan)
    # This repair is intentionally limited to the empty controls. It must not
    # re-read a mutable runtime knowledge database or invent a seed origin.
    require(wanted["knowledge"] == "empty", "this recovery supports only frozen empty-knowledge controls")
    helper._knowledge_input(package, original.get("knowledge_input"), digest(package_raw), wanted, None)
    require(package["namespace_sha256"] == digest("codex-transfer-v1"), "recovery knowledge namespace differs")
    before = _private_snapshot(trial)
    result, _ = helper._json(trial / "result.json", 16 * 1024 * 1024)
    spec, _ = helper._json(trial / "spec.json", 2 * 1024 * 1024)
    require(RepoTask.parse(spec.get("task")) == wanted["task"]
            and spec.get("task_hash") == wanted["task"].fingerprint(), "saved task is not the frozen target")
    provenance = result.get("provenance")
    require(isinstance(provenance, dict), "saved result lacks original provenance")
    for key in ("task", "task_hash", "implementation_sha256", "upstream_version", "upstream_commit", "config"):
        require(key in spec and key in provenance, "saved provenance binding is absent")
        _same(spec[key], provenance[key], "result and original spec differ")
    require(isinstance(spec["implementation_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", spec["implementation_sha256"]), "saved implementation identity is invalid")
    config = spec["config"]
    require(isinstance(config, dict), "saved runtime config is absent")
    for key, value in {"model": plan["model"], "profile": wanted["profile"], "provider": plan["provider"],
            "backend": "files", "context_tokens": 64000, "max_steps": 24, "wall_seconds": 1800,
            "request_timeout": 120, "reasoning_effort": "low",
            "knowledge_namespace_sha256": package["namespace_sha256"]}.items():
        _same(config.get(key), value, "saved runtime config differs from the frozen trial")
    require(result.get("instance_id") == wanted["task"].instance_id and result.get("fixture") is False
            and result.get("provider") == plan["provider"]
            and result.get("model_name_or_path") == plan["model"], "saved result changes the measured trial")
    for key, result_key in (("model_status", "status"), ("exit_status", "exit_status"), ("seconds", "seconds"),
            ("patch_valid", "patch_valid"), ("patch_sha256", "patch_sha256"), ("error_type", "error_type")):
        require(key in original and result_key in result, "saved result measurement is absent")
        _same(original[key], result[result_key], "saved result differs from original model outcome")
    _same(original.get("model_identity"), provenance.get("model_identity"), "original model identity differs")
    patch = _read(trial / "model.patch", 8 * 1024 * 1024)
    require(isinstance(result.get("model_patch"), str) and result["model_patch"].encode() == patch
            and digest(patch) == original["patch_sha256"], "saved patch bytes differ from the original result")
    usage, calls = exporter.normalized_usage(result, trial)
    require(type(calls) is int and calls > 0 and calls == exporter.actual_request_count(trial),
            "recovery requires matching real provider requests")
    _same(original.get("usage"), usage, "recovery cannot change already reported usage")
    _same(original.get("model_calls"), calls, "recovery cannot add or remove model calls")
    helper._measurements(original)
    counters = {"context_requests": calls, "attempt_started": True, "attempted_count": 1}
    for key, value in counters.items():
        require(key in original, "original request accounting field is absent")
        if original[key] is not None:
            _same(original[key], value, "original measured request accounting conflicts with audit")
    require(original.get("submitted") is (result["status"] == "submitted" and result["exit_status"] == "Submitted"),
            "original submission state differs")
    return public, wanted, plan_sha, original, original_raw, package, package_raw, result, before, counters


def validate_recovered_summary(public, original, original_raw):
    """Verify the public recovery graph without reading any private material."""
    helper = _helpers()
    if not (public / RECEIPT).exists():
        require(not (public / RECOVERED).exists() and "export_recovery" not in original,
                "recovered summary lacks its completion receipt")
        return original, original_raw
    receipt, _ = helper._json(public / RECEIPT)
    recovery = receipt.get("recovery")
    require(receipt.get("schema") == 1 and receipt.get("kind") == "offline_codex_export_recovery_receipt"
            and isinstance(recovery, dict), "invalid recovery receipt")
    for key, value in {"schema": 1, "kind": "offline_codex_export_recovery", "new_model_attempt": False,
            "model_calls_added": 0, "export_complete": True, "original_private_inputs_unchanged": True}.items():
        _same(recovery.get(key), value, "recovery changes model-attempt accounting")
    _same(recovery.get("original_summary"), {"path": "summary.json", "sha256": digest(original_raw)},
          "recovery original summary identity differs")
    package_raw = _read(public / "knowledge-input.json", 2 * 1024 * 1024)
    _same(recovery.get("original_knowledge_input"), {"path": "knowledge-input.json", "sha256": digest(package_raw)},
          "recovery original knowledge identity differs")
    _same(recovery.get("original_export_error_type"), original.get("export_error_type"), "recovery hides original export failure")
    require(exporter.safe_error_type(original.get("export_error_type")) is not None,
            "recovery lacks an original recorded export failure")
    _same(recovery.get("frozen_task_sha256"), original.get("source_task_sha256"), "recovery changes frozen task")
    for key in ("plan_sha256", "recovery_script_sha256"):
        require(isinstance(recovery.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", recovery[key]),
                "recovery provenance hash is absent")
    require(recovery["plan_sha256"] == helper._plan(public.parent.parent)[1],
            "recovery plan hash differs from the frozen cohort")
    private = recovery.get("private_inputs")
    require(isinstance(private, dict) and set(private) == set(PRIVATE_INPUTS), "recovery private hashes are incomplete")
    for key, value in private.items():
        if value is None and key in {"action_audit", "checkpoint"}:
            continue
        require(isinstance(value, dict) and set(value) == {"sha256", "bytes"}
                and isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                and type(value["bytes"]) is int and 0 <= value["bytes"] <= 16 * 1024 * 1024,
                "invalid private recovery hash")
    require(private["patch"]["sha256"] == original.get("patch_sha256"), "recovered private patch differs")
    expected = {RECOVERED, "repository-ablation.json", "knowledge-observation.json",
        "model-runs/trial/task.json", "model-runs/trial/model.patch", "model-runs/trial/evidence.json",
        "model-runs/trial/action-audit.jsonl"}
    value, raw = helper._json(public / RECOVERED)
    if value.get("action_audit", {}).get("available") is not True:
        require(_read(public / "model-runs/trial/action-audit.jsonl", 8 * 1024 * 1024) == b"",
                "unavailable recovery audit cannot hide executed actions")
    files = receipt.get("generated_files")
    require(isinstance(files, list) and len(files) == len(expected)
            and all(isinstance(item, dict) for item in files)
            and {item.get("path") for item in files} == expected, "recovery generated inventory differs")
    for item in files:
        blob = _read(artifact(public.resolve(), item["path"], max_bytes=8 * 1024 * 1024), 8 * 1024 * 1024)
        require(type(item.get("bytes")) is int and item["bytes"] == len(blob)
                and item.get("sha256") == digest(blob), "recovered artifact bytes differ")
    _same(value.get("export_recovery"), recovery, "recovered summary differs from its receipt")
    evidence, _ = helper._json(public / "model-runs/trial/evidence.json")
    _same(evidence.get("export_recovery"), recovery, "recovered evidence differs from its receipt")
    calls = original.get("model_calls")
    require(type(calls) is int and calls > 0, "recovery lacks a recorded original model invocation")
    counters = {"context_requests": calls, "attempt_started": True, "attempted_count": 1}
    _same(recovery.get("recovered_counters"), counters, "recovery changes original request denominator")
    for key, expected_value in counters.items():
        require(key in original and (original[key] is None or canonical(original[key]) == canonical(expected_value)),
                "recovery overwrites conflicting measured accounting")
        _same(value.get(key), expected_value, "recovered accounting differs")
    unchanged = set(original) - set(COUNTERS) - set(DERIVED)
    require(set(value) == set(original) | set(COUNTERS) | set(DERIVED), "recovery changes summary field inventory")
    for key in unchanged:
        _same(value[key], original[key], "recovery rewrites original model measurements or source provenance")
    for key in ("action_audit", "knowledge_observation"):
        if original.get(key) is not None:
            _same(value.get(key), original[key], "recovery contradicts an existing derived artifact")
    return value, raw


def recover(root, label, trial):
    helper = _helpers()
    root, trial = helper.extended_path(Path(root).resolve()), helper.extended_path(Path(trial).resolve())
    public, wanted, plan_sha, original, original_raw, package, package_raw, result, before, counters = _preflight(root, label, trial)
    # A new sibling avoids all overwrites of original files during export. On
    # failure it remains available for diagnosis and never acts as a receipt.
    stage = Path(tempfile.mkdtemp(prefix=".export-stage-", dir=public.parent))
    (stage / "summary.json").write_bytes(original_raw)
    (stage / "knowledge-input.json").write_bytes(package_raw)
    value = copy.deepcopy(original)
    value.update(counters)
    opts = SimpleNamespace(profile=wanted["profile"], model=original["model_name"], label=label)
    run_id = wanted["task"].instance_id + "-" + wanted["profile"] + "-" + label
    exporter.export_trial(result, wanted["task"], opts, trial, stage, run_id, value, package["records"])
    _same(_private_snapshot(trial), before, "private trial changed during export recovery")
    recovery = {"schema": 1, "kind": "offline_codex_export_recovery", "new_model_attempt": False,
        "model_calls_added": 0, "export_complete": True, "original_private_inputs_unchanged": True,
        "original_summary": {"path": "summary.json", "sha256": digest(original_raw)},
        "original_knowledge_input": {"path": "knowledge-input.json", "sha256": digest(package_raw)},
        "original_export_error_type": original["export_error_type"], "private_inputs": before,
        "frozen_task_sha256": wanted["frozen_task_sha256"], "plan_sha256": plan_sha,
        "recovered_counters": counters, "recovery_script_sha256": digest(Path(__file__).read_bytes())}
    value["export_recovery"] = recovery
    evidence_path = stage / "model-runs/trial/evidence.json"
    evidence, _ = helper._json(evidence_path)
    evidence["export_recovery"] = recovery
    atomic_json(evidence_path, evidence)
    atomic_json(stage / RECOVERED, value)
    generated = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name not in {"summary.json", "knowledge-input.json"}:
            blob = path.read_bytes()
            generated.append({"path": path.relative_to(stage).as_posix(), "sha256": digest(blob), "bytes": len(blob)})
    atomic_json(stage / RECEIPT, {"schema": 1, "kind": "offline_codex_export_recovery_receipt",
                                "recovery": recovery, "generated_files": generated})
    validate_recovered_summary(stage, original, original_raw)
    load_plan(stage / "repository-ablation.json")
    # Recheck immutable originals and the private result immediately before
    # publishing; the completion receipt is installed last.
    require(_read(public / "summary.json") == original_raw and _read(public / "knowledge-input.json") == package_raw,
            "original public evidence changed during recovery")
    _same(_private_snapshot(trial), before, "private trial changed before recovery publication")
    names = ("model-runs", "knowledge-observation.json", "repository-ablation.json", RECOVERED, RECEIPT)
    require(all(not (public / name).exists() for name in names), "recovery cannot overwrite generated artifacts")
    # Remove only our two exact staging copies before committing anything. A
    # cleanup error here cannot leave a completion receipt that reports failure.
    (stage / "summary.json").unlink()
    (stage / "knowledge-input.json").unlink()
    for name in names:
        os.rename(stage / name, public / name)
    validate_recovered_summary(public, original, original_raw)
    try:
        stage.rmdir()  # Empty directories contain no evidence and are not Git artifacts.
    except OSError:
        pass
    return {"label": label, "export_complete": True, "model_calls_added": 0, "original_model_calls": value["model_calls"],
            "receipt": "targets/" + label + "/" + RECEIPT}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--trial", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(recover(args.root, args.label, args.trial)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
