"""Predeclare and launch only the four frozen split trials with zero model calls.

Archive the original public directories first; this script never moves or edits
them. --prepare writes a fresh receipt without invoking a model. Publish it, then
run without --prepare to revalidate its complete snapshot before launching.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.repository import atomic_json
from scripts.grade_experiments import valid_remote_identity
from scripts.ingest_verified_delivery import canonical, digest, require

RECEIPT = "retry-receipt-attempt2.json"
ARCHIVE = "setup-failures-attempt2"
EXECUTION = "retry-execution.json"
NAMESPACE = "codex-transfer-v1"


def _helpers():
    # Deferred import keeps the independently usable summary consumer acyclic.
    from scripts import summarize_codex_transfer
    return summarize_codex_transfer


def _subset(planned):
    return sorted((row for row in planned if row["task_group"] == "split"),
                  key=lambda row: (row["repetition"], row["profile"]))


def _rows(planned):
    return [{"label": row["label"], "task": "examples/delivery-suite-v1/more-itertools-split-after.json",
             "task_sha256": row["frozen_task_sha256"], "arm": row["arm"], "profile": row["profile"],
             "repetition": row["repetition"], "private_slot": "t" + str(index)}
            for index, row in enumerate(_subset(planned), 1)]


def _private_relative(output):
    path = Path(output)
    if path.is_absolute():
        path = path.resolve().relative_to(ROOT.resolve())
    value = path.as_posix()
    require(re.fullmatch(r"runs/[A-Za-z0-9_-][A-Za-z0-9_/-]{0,150}", value) is not None,
            "retry private output must be a new bounded directory under project runs")
    return value


def _snapshot(root):
    """Validate frozen identities and zero calls before returning file hashes."""
    helper = _helpers()
    root = helper.extended_path(Path(root).resolve())
    plan, plan_sha, planned = helper._plan(root)
    selected = _subset(planned)
    execution, execution_raw = helper._json(root / "execution.json", 256 * 1024)
    require(type(execution.get("schema")) is int and execution["schema"] == 1
            and execution.get("fixture") is False and execution.get("plan_sha256") == plan_sha
            and type(execution.get("max_parallel_trials")) is int and execution["max_parallel_trials"] == 2,
            "original execution is not the frozen ten-trial batch")
    ordered, outcomes = execution.get("planned_order"), execution.get("outcomes")
    labels = {row["label"] for row in planned}
    require(isinstance(ordered, list) and len(ordered) == 10 and all(isinstance(row, dict) for row in ordered)
            and {row.get("label") for row in ordered} == labels
            and isinstance(outcomes, list) and len(outcomes) == 10 and all(isinstance(row, dict) for row in outcomes)
            and {row.get("label") for row in outcomes} == labels,
            "original batch must be finished with all ten launcher outcomes")
    for wanted in planned:
        actual = next(row for row in ordered if row["label"] == wanted["label"])
        for key, value in (("profile", wanted["profile"]), ("arm", wanted["arm"]),
                           ("repetition", wanted["repetition"]), ("task_sha256", wanted["frozen_task_sha256"]),
                           ("task", plan["targets"][0 if wanted["task_group"] == "last" else 1]["task"])):
            helper._equal(actual.get(key), value, "original execution changes a frozen task or arm")
    diagnosis_path = ARCHIVE + "/checkpoint-path-diagnosis.json"
    allowed, required = {diagnosis_path}, {diagnosis_path}
    task_exports = {}
    for wanted in selected:
        label = wanted["label"]
        prefix = ARCHIVE + "/targets/" + label + "/"
        run_id = wanted["task"].instance_id + "-" + wanted["profile"] + "-" + label
        model = prefix + "model-runs/" + run_id + "/"
        required.update({prefix + "summary.json", prefix + "knowledge-input.json"})
        allowed.update({prefix + name for name in ("summary.json", "knowledge-input.json",
                                                    "knowledge-observation.json", "repository-ablation.json")})
        allowed.update({model + name for name in ("task.json", "model.patch", "action-audit.jsonl", "evidence.json")})
        task_exports[model + "task.json"] = asdict(wanted["task"])
        task_exports[model + "task.json"]["public_checks"] = list(wanted["task"].public_checks)
    archive = root / ARCHIVE
    require(archive.is_dir() and not archive.is_symlink(), "the four old public directories must be archived first")
    files, blobs = [], {}
    for index, path in enumerate(archive.rglob("*")):
        require(index < 128 and not path.is_symlink(), "retry archive has links or excessive entries")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        require(relative in allowed and path.stat().st_size <= 2 * 1024 * 1024,
                "retry archive contains an unplanned or oversized artifact")
        raw = path.read_bytes()
        files.append({"path": relative, "sha256": digest(raw), "bytes": len(raw)})
        blobs[relative] = raw
        if relative in task_exports:
            value, _ = helper._json(path)
            helper._equal(value, task_exports[relative], "archived exported task differs from the frozen task")
        elif relative.endswith(("/model.patch", "/action-audit.jsonl")):
            require(raw == b"", "a zero-call setup retry cannot contain a patch or executed tool action")
    require(required <= set(blobs), "retry archive lacks a planned summary or knowledge input")
    diagnosis, _ = helper._json(root / diagnosis_path)
    for key, expected_value in {"schema": 1, "kind": "real_windows_checkpoint_path_probe", "fixture": True,
            "model_calls": 0, "operation": "CheckpointStore.write", "original_model_trials_modified": False,
            "error_type": "FileNotFoundError", "errno": 2, "checkpoint_exists": False}.items():
        helper._equal(diagnosis.get(key), expected_value, "checkpoint diagnosis is not the zero-model-call probe")
    require(type(diagnosis.get("checkpoint_path_chars")) is int
            and type(diagnosis.get("temporary_path_chars")) is int
            and 0 < diagnosis["checkpoint_path_chars"] < 260 < diagnosis["temporary_path_chars"],
            "checkpoint diagnosis does not record the observed temporary-path overflow")
    setup_rows = []
    for wanted in selected:
        label = wanted["label"]
        prefix = ARCHIVE + "/targets/" + label + "/"
        value, _ = helper._json(root / (prefix + "summary.json"))
        expected = {"schema": 1, "kind": "real_codex_source_trial", "fixture": False,
            "source_task_sha256": wanted["frozen_task_sha256"],
            "source_task_fingerprint": wanted["task"].fingerprint(), "model_name": plan["model"],
            "provider": plan["provider"], "profile": wanted["profile"], "label": label,
            "model_call_unit": "cli_invocations", "attempt_started": False, "attempted_count": 0,
            "context_requests": 0, "model_calls": 0, "model_status": "failed", "submitted": False,
            "exit_status": None, "patch_valid": None, "complete": False, "delivery_success": None,
            "official_resolved": None, "underlying_transport_request_count": None,
            "knowledge_observation": None, "error_type": "FileNotFoundError", "export_error_type": "FileNotFoundError"}
        for key, expected_value in expected.items():
            require(key in value, "archived zero-call summary lacks a required field")
            helper._equal(value[key], expected_value, "a retry cannot conceal a model invocation or change its frozen identity")
        require(isinstance(value.get("source_commit"), str)
                and re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]), "original source commit is absent")
        require(type(value.get("source_tracked_tree_clean")) is bool,
                "original working-tree cleanliness must remain an observed boolean")
        require(value.get("patch_sha256") in (None, digest(b"")), "zero-call retry has a nonempty patch identity")
        identity = value.get("model_identity")
        require(identity is None or valid_remote_identity({"model_identity": identity,
            "model_name": plan["model"], "model_digest": None}), "archived model identity is malformed")
        measured = helper._measurements(value)
        require(measured["usage_complete"] is True and measured["input_tokens"] == measured["output_tokens"] == 0
                and helper._number(measured["seconds"]), "retry requires complete zero model usage and known setup time")
        helper._budgets(value.get("budgets"), plan)
        package, package_raw = helper._json(root / (prefix + "knowledge-input.json"))
        helper._knowledge_input(package, value.get("knowledge_input"), digest(package_raw), wanted, None)
        require(package["namespace_sha256"] == digest(NAMESPACE), "retry changes the frozen empty knowledge namespace")
        run_id = wanted["task"].instance_id + "-" + wanted["profile"] + "-" + label
        evidence_path = prefix + "model-runs/" + run_id + "/evidence.json"
        if evidence_path in blobs:
            evidence, _ = helper._json(root / evidence_path)
            for key, summary_key in (("fixture", "fixture"), ("provider", "provider"), ("model_name", "model_name"),
                    ("profile", "profile"), ("model_calls", "model_calls"), ("context_requests", "context_requests"),
                    ("usage", "usage"), ("seconds", "seconds"), ("status", "model_status"),
                    ("exit_status", "exit_status"), ("patch_sha256", "patch_sha256"), ("model_identity", "model_identity")):
                require(key in evidence, "archived partial evidence lacks measured identity")
                helper._equal(evidence[key], value[summary_key], "archived evidence contradicts zero-call summary")
        if prefix + "knowledge-observation.json" in blobs:
            observation, _ = helper._json(root / (prefix + "knowledge-observation.json"))
            require(observation.get("knowledge_observed_read") in (None, False) and observation.get("reads") == []
                    and observation.get("nonempty_read_count") in (None, 0), "zero-call retry contains a knowledge read")
        outcome = next(row for row in outcomes if row["label"] == label)
        require(type(outcome.get("launcher_returncode")) is int and outcome["launcher_returncode"] != 0
                and outcome.get("launcher_timed_out") is False and outcome.get("summary_present") is True
                and helper._number(outcome.get("launcher_elapsed_seconds"))
                and outcome["launcher_elapsed_seconds"] >= measured["seconds"], "original launcher did not record this setup failure")
        setup_rows.append({"label": label, "source_commit": value["source_commit"], "model_calls": 0,
            "source_tracked_tree_clean": value["source_tracked_tree_clean"],
            "model_attempts": 0, "input_tokens": 0, "output_tokens": 0, "seconds": measured["seconds"],
            "launcher_elapsed_seconds": outcome["launcher_elapsed_seconds"], "error_type": value["error_type"],
            "summary_sha256": digest(blobs[prefix + "summary.json"])})
    require(len({row["source_commit"] for row in setup_rows}) == 1, "archived subset comes from different source revisions")
    return {"plan": plan, "plan_sha256": plan_sha, "rows": _rows(planned), "setup_rows": setup_rows,
        "preserved_files": sorted(files, key=lambda item: item["path"]),
        "original_execution": {"path": "execution.json", "sha256": digest(execution_raw), "bytes": len(execution_raw)}}


def prepare_receipt(root, output="runs/ct3"):
    """Write the predeclaration only; never launch, move or delete anything."""
    helper = _helpers()
    root = helper.extended_path(Path(root).resolve())
    snapshot = _snapshot(root)
    relative = _private_relative(output)
    require(not (root / RECEIPT).exists() and not (root / EXECUTION).exists(), "retry receipt/execution must be fresh")
    require(not helper.extended_path(ROOT / relative).exists(), "retry private directory must be fresh")
    require(all(not (root / "targets" / row["label"]).exists() for row in snapshot["rows"]),
            "archive all four old target directories before preparing the retry")
    receipt = {"schema": 1, "kind": "predeclared_zero_call_subset_retry", "fixture": False,
        "plan_sha256": snapshot["plan_sha256"], "original_execution": snapshot["original_execution"],
        "preserved_directory": ARCHIVE, "preserved_launcher_trials": 4, "original_model_calls": 0,
        "original_model_attempts": 0, "preserved_files": snapshot["preserved_files"],
        "original_setup_rows": snapshot["setup_rows"],
        "original_source_commit": snapshot["setup_rows"][0]["source_commit"],
        "retry": {"target_directory": "targets", "private_output": relative, "planned_trials": 4,
            "max_parallel_trials": 2, "planned_order": snapshot["rows"], "common": snapshot["plan"]["common"],
            "model": snapshot["plan"]["model"], "provider": snapshot["plan"]["provider"],
            "knowledge_namespace": NAMESPACE, "knowledge_mode": "empty", "tasks_budgets_and_labels_unchanged": True,
            "runner_sha256": digest((ROOT / "scripts/run_codex_trial.py").read_bytes()),
            "launcher_sha256": digest(Path(__file__).read_bytes())},
        "interpretation": "Only four recorded zero-call setup failures are retried. Their original public bytes remain archived; canonical comparison still has ten planned target samples."}
    with (root / RECEIPT).open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return receipt


def validate_receipt(root, *, launching=False, output=None):
    helper = _helpers()
    root = helper.extended_path(Path(root).resolve())
    receipt, raw = helper._json(root / RECEIPT)
    snapshot = _snapshot(root)
    expected = {"schema": 1, "kind": "predeclared_zero_call_subset_retry", "fixture": False,
        "plan_sha256": snapshot["plan_sha256"], "original_execution": snapshot["original_execution"],
        "preserved_directory": ARCHIVE, "preserved_launcher_trials": 4, "original_model_calls": 0,
        "original_model_attempts": 0, "preserved_files": snapshot["preserved_files"],
        "original_setup_rows": snapshot["setup_rows"],
        "original_source_commit": snapshot["setup_rows"][0]["source_commit"]}
    for key, value in expected.items():
        helper._equal(receipt.get(key), value, "subset retry receipt differs from its preserved original evidence")
    retry = receipt.get("retry")
    require(isinstance(retry, dict), "subset retry plan is missing")
    for key, value in {"target_directory": "targets", "planned_trials": 4, "max_parallel_trials": 2,
            "planned_order": snapshot["rows"], "common": snapshot["plan"]["common"],
            "model": snapshot["plan"]["model"], "provider": snapshot["plan"]["provider"],
            "knowledge_namespace": NAMESPACE, "knowledge_mode": "empty", "tasks_budgets_and_labels_unchanged": True}.items():
        helper._equal(retry.get(key), value, "subset retry changes the predeclared task/model/budget/knowledge plan")
    require(_private_relative(retry.get("private_output", "")) == retry["private_output"], "invalid retry private directory")
    for key in ("runner_sha256", "launcher_sha256"):
        require(isinstance(retry.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", retry[key]), "retry executable hash is absent")
    if launching:
        require(_private_relative(output) == retry["private_output"], "launch output differs from the predeclaration")
        require(retry["runner_sha256"] == digest((ROOT / "scripts/run_codex_trial.py").read_bytes())
                and retry["launcher_sha256"] == digest(Path(__file__).read_bytes()), "retry executable changed after predeclaration")
        require(not (root / EXECUTION).exists() and not helper.extended_path(ROOT / retry["private_output"]).exists()
                and all(not (root / "targets" / row["label"]).exists() for row in snapshot["rows"]),
                "retry cannot overwrite an execution, private directory or canonical target")
    return receipt, snapshot, digest(raw)


def summary_history(root):
    """Public consumer for the second receipt and the optional live launcher log."""
    helper = _helpers()
    root = helper.extended_path(Path(root).resolve())
    if not (root / RECEIPT).exists():
        require(not (root / ARCHIVE).exists() and not (root / EXECUTION).exists(),
                "second setup archive/execution lacks its predeclared receipt")
        return None
    receipt, snapshot, receipt_sha = validate_receipt(root)
    observed = None
    if (root / EXECUTION).exists():
        execution, blob = helper._json(root / EXECUTION, 256 * 1024)
        expected = {"schema": 1, "kind": "zero_call_subset_retry_execution", "fixture": False,
            "plan_sha256": snapshot["plan_sha256"], "retry_receipt_sha256": receipt_sha,
            "original_execution_sha256": snapshot["original_execution"]["sha256"],
            "max_parallel_trials": 2, "planned_order": snapshot["rows"]}
        for key, value in expected.items():
            helper._equal(execution.get(key), value, "retry execution differs from its predeclaration")
        outcomes = execution.get("outcomes")
        labels = {row["label"] for row in snapshot["rows"]}
        require(isinstance(outcomes, list) and len(outcomes) <= 4 and all(isinstance(row, dict) for row in outcomes)
                and len({row.get("label") for row in outcomes}) == len(outcomes)
                and {row.get("label") for row in outcomes} <= labels, "retry execution repeats or adds an unplanned trial")
        for row in outcomes:
            if "launcher_error_type" in row:
                require(isinstance(row["launcher_error_type"], str)
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", row["launcher_error_type"]),
                        "invalid retry launcher exception type")
            else:
                require(type(row.get("launcher_returncode")) is int and type(row.get("launcher_timed_out")) is bool
                        and helper._number(row.get("launcher_elapsed_seconds"))
                        and type(row.get("summary_present")) is bool, "invalid retry launcher outcome")
        observed = {"path": EXECUTION, "sha256": digest(blob), "outcomes": outcomes,
                    "launcher_outcomes_recorded": len(outcomes), "launcher_batch_complete": len(outcomes) == 4}
    else:
        require(all(not (root / "targets" / row["label"]).exists() for row in snapshot["rows"]),
                "canonical retry targets exist without their launcher execution record")
    rows = snapshot["setup_rows"]
    return {"receipt": {"path": RECEIPT, "sha256": receipt_sha}, "evidence_status": "validated",
        "preserved_directory": ARCHIVE, "preserved_artifacts": snapshot["preserved_files"],
        "original_execution": snapshot["original_execution"], "original_source_commit": receipt["original_source_commit"],
        "launcher_failures": 4, "model_calls": 0, "model_attempts": 0, "setup_rows": rows,
        "excluded_from_model_attempt_denominator": True, "retry_execution": observed,
        "trial_seconds_sum": sum(row["seconds"] for row in rows),
        "launcher_seconds_sum": sum(row["launcher_elapsed_seconds"] for row in rows),
        "interpretation": "Four additional zero-call setup failures are retained separately; their replacement trials occupy the same four of ten planned target slots."}


def launch(root, output, codex_binary):
    helper = _helpers()
    root = helper.extended_path(Path(root).resolve())
    receipt, snapshot, receipt_sha = validate_receipt(root, launching=True, output=output)
    require(isinstance(codex_binary, str) and bool(codex_binary.strip()), "a worker-local Codex binary is required")
    private = ROOT / receipt["retry"]["private_output"]
    private.mkdir(parents=True)
    execution = {"schema": 1, "kind": "zero_call_subset_retry_execution", "fixture": False,
        "plan_sha256": snapshot["plan_sha256"], "retry_receipt_sha256": receipt_sha,
        "original_execution_sha256": snapshot["original_execution"]["sha256"], "max_parallel_trials": 2,
        "planned_order": snapshot["rows"], "outcomes": [],
        "time_scope": "Concurrent local repository runs; elapsed time includes shared-host and remote-service contention."}
    atomic_json(root / EXECUTION, execution)

    def run(row):
        arguments = [sys.executable, str(ROOT / "scripts/run_codex_trial.py"), "--task", str(ROOT / row["task"]),
            "--model", receipt["retry"]["model"], "--codex-binary", codex_binary, "--profile", row["profile"],
            "--max-steps", "24", "--context-tokens", "64000", "--wall-seconds", "1800", "--request-timeout", "120",
            "--knowledge-empty", "--knowledge-namespace", NAMESPACE, "--output", str(private / row["private_slot"]),
            "--evidence", str(root / "targets" / row["label"]), "--label", row["label"]]
        started, timed_out = time.monotonic(), False
        with (private / (row["label"] + ".launch.log")).open("wb") as log:
            process = subprocess.Popen(arguments, stdout=log, stderr=log, cwd=ROOT, start_new_session=os.name != "nt")
            try:
                process.wait(timeout=2100)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=log, stderr=log, timeout=15)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
        return {"label": row["label"], "launcher_returncode": process.returncode, "launcher_timed_out": timed_out,
            "launcher_elapsed_seconds": time.monotonic() - started,
            "summary_present": (root / "targets" / row["label"] / "summary.json").is_file()}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run, row): row for row in snapshot["rows"]}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"label": row["label"], "launcher_error_type": type(exc).__name__}
            execution["outcomes"].append(result)
            atomic_json(root / EXECUTION, execution)
            print(json.dumps(result), flush=True)
    return 0 if all(row.get("launcher_returncode") == 0 for row in execution["outcomes"]) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", default="runs/ct3")
    parser.add_argument("--codex-binary")
    parser.add_argument("--prepare", action="store_true", help="Write the predeclaration only, then stop for publication")
    args = parser.parse_args(argv)
    if args.prepare:
        receipt = prepare_receipt(args.root, args.output)
        print(json.dumps({"prepared": True, "planned_trials": 4, "model_calls": 0,
                          "preserved_files": len(receipt["preserved_files"])}))
        return 0
    return launch(args.root, args.output, args.codex_binary)


if __name__ == "__main__":
    raise SystemExit(main())
