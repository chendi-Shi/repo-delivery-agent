"""Recheck the fixed ten-trial Codex transfer cohort without mutating evidence."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import sys
from pathlib import Path
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.grade_experiments import artifact, load_plan
from scripts.ingest_verified_delivery import (SHA, canonical, digest, frozen_tasks, prepare_record,
                                             read_json, require)

ORIGIN_KEYS = ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref", "verification_sha256")
METRICS = ("input_tokens", "output_tokens", "model_calls", "seconds")


def extended_path(path):
    """Opt into Win32 long paths for stat, enumeration and actual file reads."""
    path = Path(path)
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _json(path, cap=2 * 1024 * 1024):
    path = extended_path(path)
    if not path.exists():
        raise FileNotFoundError("planned artifact is absent")
    return read_json(path, cap)


def _equal(left, right, reason):
    require(canonical(left) == canonical(right), reason)


def _number(value, integer=False):
    return type(value) in ((int,) if integer else (int, float)) and math.isfinite(value) and value >= 0


def _plan(root):
    plan, raw = _json(root / "plan.json", 256 * 1024)
    expected_targets = [
        {"task": "examples/delivery-suite-v1/more-itertools-last-factory.json",
         "arms": ["baseline-empty", "retrieval-empty", "retrieval-verified"]},
        {"task": "examples/delivery-suite-v1/more-itertools-split-after.json",
         "arms": ["baseline-empty", "retrieval-empty"]}]
    require(plan.get("schema") == 1 and plan.get("kind") == "predeclared_real_codex_knowledge_transfer"
            and plan.get("fixture") is False and plan.get("provider") == "codex_cli"
            and plan.get("model") == "gpt-6-astra" and plan.get("source_profile") == "baseline"
            and plan.get("source_task") == "examples/delivery-suite-v1/more-itertools-first-factory.json"
            and plan.get("repetitions") == [1, 2] and plan.get("targets") == expected_targets
            and type(plan.get("planned_target_trials")) is int and plan["planned_target_trials"] == 10,
            "plan differs from the predeclared ten-trial cohort")
    _equal(plan.get("common"), {"selected_context_bytes": 64000, "max_steps": 24, "wall_seconds": 1800,
                              "request_timeout": 120, "reasoning_effort": "low"}, "frozen budgets differ")
    _, last, split = frozen_tasks()
    rows = []
    for prefix, target, task in zip(("last", "split"), expected_targets, (last, split)):
        for arm in target["arms"]:
            for repetition in plan["repetitions"]:
                rows.append({"label": f"{prefix}-{arm}-r{repetition}", "task_group": prefix, "arm": arm,
                    "repetition": repetition, "profile": arm.split("-", 1)[0],
                    "knowledge": "verified_seed" if arm.endswith("verified") else "empty",
                    "task": task[0], "frozen_task_sha256": task[1]})
    return plan, digest(raw), rows


def _measurements(evidence):
    calls, seconds, usage = evidence.get("model_calls"), evidence.get("seconds"), evidence.get("usage")
    require(calls is None or _number(calls, True), "invalid model call measurement")
    require(seconds is None or _number(seconds), "invalid elapsed measurement")
    result = {"model_calls": calls, "seconds": seconds, "input_tokens": None, "output_tokens": None,
              "usage_complete": None, "unknown_usage_requests": None}
    if usage is not None:
        require(isinstance(usage, dict) and type(usage.get("complete")) is bool, "invalid token measurement")
        for key in ("input_tokens", "output_tokens", "calls_with_usage", "unknown_requests"):
            require(_number(usage.get(key), True), "invalid token measurement")
        if calls is not None:
            require(usage["calls_with_usage"] + usage["unknown_requests"] == calls,
                    "usage accounting differs from actual CLI invocations")
        require(usage["complete"] is (usage["unknown_requests"] == 0), "usage completeness contradicts unknown requests")
        result.update(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                      usage_complete=usage["complete"], unknown_usage_requests=usage["unknown_requests"])
    return result


def _budgets(budget, plan):
    require(isinstance(budget, dict), "trial budgets are absent")
    for key, common_key in (("selected_context_byte_budget", "selected_context_bytes"), ("max_steps", "max_steps"),
            ("wall_seconds", "wall_seconds"), ("request_timeout", "request_timeout"), ("reasoning_effort", "reasoning_effort")):
        _equal(budget.get(key), plan["common"][common_key], "trial budget differs from frozen plan")
    require(budget.get("seed_enforced") is False and budget.get("generation_token_cap_enforced") is False
            and budget.get("cli_prompt_overhead_in_selected_context_budget") is False
            and type(budget.get("output_and_protocol_reserve")) is int
            and budget["output_and_protocol_reserve"] == 3048,
            "remote trial budget semantics differ from the frozen runner")


def _trial(root, profile, task, frozen_sha, plan, expected_label):
    root = extended_path(root)
    summary, summary_raw = _json(root / "summary.json")
    from scripts.recover_codex_export import validate_recovered_summary
    original_summary_raw = summary_raw
    summary, summary_raw = validate_recovered_summary(root, summary, summary_raw)
    manifest, manifest_raw = _json(root / "repository-ablation.json", 256 * 1024)
    runs = load_plan(root / "repository-ablation.json")
    require(len(runs) == 1 and runs[0]["task"] == task, "trial is not its planned frozen task")
    run, entry = runs[0], manifest["runs"][0]
    evidence = run["evidence"]
    require(summary.get("label") == expected_label
            and run["id"] == task.instance_id + "-" + profile + "-" + expected_label,
            "trial label or run ID differs from its predeclared repetition")
    require(summary.get("fixture") is False and evidence.get("provider") == plan["provider"]
            and evidence.get("model_name") == plan["model"] and evidence.get("profile") == profile
            and evidence.get("task_hash") == task.fingerprint()
            and evidence.get("inference_backend") == "files"
            and summary.get("source_task_sha256") == frozen_sha
            and summary.get("source_task_fingerprint") == task.fingerprint(), "trial identity differs from plan")
    for key, evidence_key in (("provider", "provider"), ("profile", "profile"), ("model_name", "model_name"),
            ("model_identity", "model_identity"), ("model_status", "status"), ("exit_status", "exit_status"),
            ("patch_sha256", "patch_sha256"), ("patch_valid", "patch_valid"), ("usage", "usage"),
            ("model_calls", "model_calls"), ("context_requests", "context_requests"), ("seconds", "seconds"),
            ("action_audit", "action_audit"), ("budgets", "budgets")):
        require(key in summary and evidence_key in evidence, "required summary/evidence binding is missing")
        _equal(summary[key], evidence[evidence_key], "summary differs from exported evidence")
    require(evidence.get("model_call_unit") == "cli_invocations"
            and summary.get("model_call_unit") == "cli_invocations"
            and evidence.get("underlying_transport_request_count") is None, "calls must retain CLI invocation units")
    require(all(isinstance(evidence.get(key), str) and SHA.fullmatch(evidence[key])
                for key in ("implementation_sha256", "configuration_sha256")), "implementation/configuration identity is absent")
    _budgets(evidence["budgets"], plan)
    calls = evidence.get("context_requests")
    require(_number(calls, True) and evidence.get("model_calls") == calls
            and summary.get("attempt_started") is (calls > 0)
            and type(summary.get("attempted_count")) is int and summary["attempted_count"] == int(calls > 0),
            "trial attempt accounting differs from observed CLI invocations")
    submitted = evidence["status"] == "submitted" and evidence["exit_status"] == "Submitted"
    require(summary.get("submitted") is submitted, "submission claim differs from upstream protocol")
    require(not submitted or calls > 0, "a submitted model delivery requires an observed CLI invocation")
    summary_path = "recovered-summary.json" if "export_recovery" in summary else "summary.json"
    refs = {"summary": {"path": summary_path, "sha256": digest(summary_raw)},
            "manifest": {"path": "repository-ablation.json", "sha256": digest(manifest_raw)},
            "task": {"path": entry["task"], "sha256": run["task_sha256"]},
            "evidence": {"path": entry["evidence"], "sha256": run["evidence_sha256"]},
            "patch": {"path": entry["patch"], "sha256": digest(run["patch"])}}
    if "export_recovery" in summary:
        refs["original_summary"] = {"path": "summary.json", "sha256": digest(original_summary_raw)}
        refs["export_recovery"] = {"path": "export-recovery.json", "sha256": digest(_json(root / "export-recovery.json")[1])}
    return run, summary, entry, refs, {**_measurements(evidence), "submitted": submitted,
        "attempt_started": calls > 0, "status": evidence["status"], "exit_status": evidence["exit_status"],
        "model_identity": evidence["model_identity"], "implementation_sha256": evidence.get("implementation_sha256")}


def _external(path, run, manifest_sha):
    report, raw = _json(path)
    require(report.get("schema") == 1 and report.get("kind") == "independent_public_repository_verification"
            and report.get("manifest_sha256") == manifest_sha, "external report is unbound")
    require(report.get("attempts") == 1 and isinstance(report.get("runs"), list)
            and isinstance(report.get("infrastructure_failures"), list), "external report is incomplete")
    if report["infrastructure_failures"]:
        require(not report["runs"], "external infrastructure failure has conflicting run result")
        return None, digest(raw), "verification_infrastructure_failure", None
    require(len(report["runs"]) == 1, "external run is missing or repeated")
    row, evidence, task = report["runs"][0], run["evidence"], run["task"]
    expected = {"id": run["id"], "instance_id": task.instance_id, "profile": evidence["profile"],
        "provider": evidence["provider"], "model_name": evidence["model_name"], "model_digest": None,
        "model_identity": evidence["model_identity"], "model_status": evidence["status"],
        "patch_sha256": digest(run["patch"]), "task_sha256": run["task_sha256"], "evidence_sha256": run["evidence_sha256"]}
    require(isinstance(row, dict), "external run is malformed")
    for key, value in expected.items():
        require(key in row, "external identity field is missing")
        _equal(row[key], value, "external report differs from actual trial artifacts")
    submitted = evidence["status"] == "submitted" and evidence["exit_status"] == "Submitted"
    require(row.get("submitted") is submitted and row.get("public_check_eligible") is True,
            "external submission/check eligibility differs")
    checks = row.get("public_checks")
    require(isinstance(checks, list) and len(checks) <= len(task.public_checks), "external checks are malformed")
    if row.get("verification_status") == "patch_invalid":
        require(row.get("patch_valid") is False and checks == [] and row.get("delivery_success") is False,
                "invalid patch cannot be a delivered result")
        return False, digest(raw), "patch_invalid", None
    require(row.get("patch_valid") is True and isinstance(row.get("resolved_image"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", row["resolved_image"]), "independent patch/image validation is absent")
    for check, command in zip(checks, task.public_checks):
        require(isinstance(check, dict) and check.get("command") == command, "external check command differs")
        code = check.get("returncode")
        require((type(code) is int and check.get("passed") is (code == 0)) or
                (code is None and check.get("budget_exhausted") is True and check.get("passed") is False),
                "external check outcome is inconsistent")
    require(len(checks) == len(task.public_checks) or
            (bool(checks) and checks[-1].get("budget_exhausted") is True), "external checks stopped without an outcome")
    passed = len(checks) == len(task.public_checks) and all(check["passed"] for check in checks)
    require(row.get("verification_status") == ("passed" if passed else "failed")
            and row.get("delivery_success") is (submitted and passed)
            and row.get("public_delivery_status") == ("passed" if submitted and passed else "failed"),
            "external success contradicts its complete public checks")
    return submitted and passed, digest(raw), None, row["resolved_image"]


def _source(root, plan):
    task, frozen_sha = frozen_tasks()[0]
    run, summary, entry, refs, metrics = _trial(root / "source", plan["source_profile"], task, frozen_sha, plan,
                                              "codex-transfer-v1-source")
    folder = root / "source-verification/source"
    record = prepare_record(root / "source/repository-ablation.json", folder / "verification.json", folder / "source-guard.json")
    package, raw = _json(root / "source-package.json")
    require(package.get("schema") == 1 and package.get("kind") == "external_verified_delivery_seed"
            and package.get("fixture") is False and package.get("eligible") is True,
            "source package is not independently verified")
    _equal(package.get("record"), record, "seed body or provenance differs from actual verified source")
    origin = package.get("origin")
    require(isinstance(origin, dict) and set(origin) == set(ORIGIN_KEYS)
            and origin.get("verified") is True and origin.get("task_id") == task.instance_id
            and origin.get("content_sha256") == digest(record["body"])
            and origin.get("source_ref") == record["source_ref"]
            and origin.get("verification_sha256") == digest(canonical(record["verification"]))
            and isinstance(origin.get("namespace_sha256"), str) and SHA.fullmatch(origin["namespace_sha256"]),
            "source package origin differs")
    identity = digest(json.dumps([record["source_type"], record["source_ref"], record["title"]], separators=(",", ":")))
    require(origin["source_id"] == "k" + digest(origin["namespace_sha256"] + identity)[:32], "source identifier differs")
    return package, {"evidence_status": "validated", "delivery_success": True, **metrics,
        "artifact_root": "source", "artifacts": refs,
        "source_package": {"path": "source-package.json", "sha256": digest(raw)},
        "verification": {"path": "source-verification/source/verification.json",
                         "sha256": record["verification"]["verification_sha256"]},
        "guard": {"path": "source-verification/source/source-guard.json", "sha256": record["verification"]["guard_sha256"]},
        "source_package_sha256": digest(raw), "excluded_from_target_denominator": True}


def _reads(rows, audit, records):
    matches = []
    for item in records:
        origin, body = item["origin"], item["record"]["body"]
        for index, row in enumerate(rows, 1):
            command = row.get("command_preview")
            if not (row.get("event") == "action" and row.get("outcome") == "completed"
                    and type(row.get("returncode")) is int and row["returncode"] == 0
                    and row.get("action_kind") == "delivery-knowledge/read" and row.get("knowledge_operation") == "read"
                    and row.get("knowledge_namespace_sha256") == origin["namespace_sha256"]
                    and isinstance(row.get("knowledge_records"), list)
                    and any(isinstance(value, dict) and all(type(value.get(key)) is type(origin[key])
                        and value.get(key) == origin[key] for key in ORIGIN_KEYS) for value in row["knowledge_records"])
                    and isinstance(command, str) and row.get("command_chars") == len(command)
                    and row.get("command_sha256") == digest(command)):
                continue
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
                lexer.whitespace_split, lexer.commenters = True, ""
                tokens = list(lexer)
                if not 3 <= len(tokens) <= 5 or tokens[:3] != ["delivery-knowledge", "read", origin["source_id"]]:
                    continue
                offset, limit = int(tokens[3]) if len(tokens) > 3 else 0, int(tokens[4]) if len(tokens) > 4 else 2000
                if not 0 <= offset < len(body) or not 1 <= limit <= 4000:
                    continue
            except (ValueError, TypeError):
                continue
            matches.append({"audit_line": index, "call": row.get("call"), "ordinal": row.get("ordinal"),
                "source_id": origin["source_id"], "content_sha256": origin["content_sha256"],
                "verification_sha256": origin["verification_sha256"], "command_sha256": digest(command),
                "offset": offset, "limit": limit, "returned_chars": len(body[offset:offset + limit]),
                "slice_sha256": digest(body[offset:offset + limit])})
    complete = audit.get("complete") is True
    return {"knowledge_observed_read": True if matches else False if complete else None,
        "nonempty_read_count": len(matches) if complete else None,
        "observed_nonempty_read_count_lower_bound": len(matches), "reads": matches, "audit_complete": complete}


def _knowledge_input(package, receipt, package_sha, planned, source):
    """Use the same input checks for model trials and archived setup failures."""
    require(isinstance(receipt, dict) and package.get("schema") == 1
            and package.get("kind") == "frozen_codex_trial_knowledge_input" and package.get("tools_enabled") is True
            and package.get("mode") == planned["knowledge"] and package.get("target_records_reused") is False
            and receipt.get("package") == "knowledge-input.json" and receipt.get("package_sha256") == package_sha,
            "knowledge input is unbound or changes planned tools")
    records = package.get("records")
    require(isinstance(records, list), "knowledge input records are malformed")
    if planned["knowledge"] == "empty":
        require(records == [] and package.get("source_database_sha256") is None, "empty control contains knowledge")
    else:
        require(source is not None and len(records) == 1 and isinstance(records[0], dict), "verified arm lacks its validated source")
        _equal(records[0].get("record"), source["record"], "target knowledge differs from frozen source")
        _equal(records[0].get("origin"), source["origin"], "target source identity differs")
        require(records[0].get("expires_at") is None and isinstance(package.get("source_database_sha256"), str)
                and SHA.fullmatch(package["source_database_sha256"]), "source database identity/expiry differs")
    require(isinstance(package.get("namespace_sha256"), str) and SHA.fullmatch(package["namespace_sha256"]),
            "knowledge namespace identity is missing")
    if source is not None:
        require(package["namespace_sha256"] == source["origin"]["namespace_sha256"], "knowledge namespace differs across arms")
    for key, expected in (("tools_enabled", True), ("mode", package["mode"]), ("record_count", len(records)),
            ("namespace_sha256", package["namespace_sha256"]), ("source_database_sha256", package["source_database_sha256"]),
            ("origins", [item["origin"] for item in records])):
        _equal(receipt.get(key), expected, "knowledge receipt differs from input package")
    return records


def _knowledge(directory, run, summary, entry, planned, source):
    package, raw = _json(directory / "knowledge-input.json")
    observation, observation_raw = _json(directory / "knowledge-observation.json")
    receipt = run["evidence"].get("knowledge_input")
    _equal(receipt, summary.get("knowledge_input"), "summary knowledge input differs from evidence")
    records = _knowledge_input(package, receipt, digest(raw), planned, source)
    audit = run["evidence"].get("action_audit")
    require(isinstance(audit, dict), "action audit metadata is absent")
    audit_relative = str(Path(entry["evidence"]).with_name("action-audit.jsonl")).replace("\\", "/")
    require(observation.get("input_package") == "knowledge-input.json"
            and observation.get("input_package_sha256") == digest(raw)
            and observation.get("action_audit") == audit_relative, "knowledge observation artifacts are unbound")
    _equal(observation, summary.get("knowledge_observation"), "summary observation differs from saved observation")
    _equal(observation, run["evidence"].get("knowledge_observation"), "evidence observation differs from saved observation")
    if audit.get("available") is not True:
        expected = {"knowledge_observed_read": None, "nonempty_read_count": None, "reads": [], "audit_complete": False}
    else:
        path = artifact(directory.resolve(), audit_relative, max_bytes=8 * 1024 * 1024)
        blob = path.read_bytes()
        require(digest(blob) == audit.get("public_audit_sha256") and digest(blob) == observation.get("public_audit_sha256"),
                "knowledge action audit bytes differ")
        rows = [json.loads(line) for line in blob.decode("utf-8").splitlines()]
        require(len(rows) <= 10000 and all(isinstance(row, dict) for row in rows)
                and type(audit.get("actions")) is int and len(rows) == audit["actions"], "audit action count differs")
        expected = _reads(rows, audit, records)
    for key, value in expected.items():
        _equal(observation.get(key), value, "knowledge read claim differs from actual nonempty audited read")
    return {**expected, "source_database_sha256": package.get("source_database_sha256"),
        "input_sha256": digest(raw), "observation_sha256": digest(observation_raw),
        "action_audit_sha256": audit.get("public_audit_sha256")}


def _aggregate(rows):
    success = sum(row.get("delivery_success") is True for row in rows)
    failed = sum(row.get("delivery_success") is False for row in rows)
    totals = {}
    for key in METRICS:
        values = [row[key] for row in rows if _number(row.get(key), key != "seconds")]
        totals[key] = {"known_subtotal": sum(values) if values else None, "known_trials": len(values),
                       "unknown_trials": len(rows) - len(values)}
    return {"planned_trials": len(rows), "successes": success, "failures": failed, "unknown": len(rows) - success - failed,
        "success_rate_all_planned": success / len(rows) if rows and success + failed == len(rows) else None,
        "attempts_confirmed": sum(row.get("attempt_started") is True for row in rows),
        "attempts_unknown": sum(row.get("attempt_started") is None for row in rows),
        "observed_source_reads": sum(row.get("knowledge_observed_read") is True for row in rows),
        "source_read_unknown": sum(row.get("knowledge_observed_read") is None for row in rows),
        "usage_incomplete_or_unknown_trials": sum(row.get("usage_complete") is not True for row in rows),
        "all_attempt_measurements": totals}


def _setup_history(root, plan, plan_sha, planned, source):
    path = root / "retry-receipt.json"
    correction_path = root / "retry-receipt-correction.json"
    if not path.exists():
        require(not (root / "setup-failures-attempt1").exists() and not correction_path.exists(),
                "preserved setup failures lack their retry receipt")
        return None
    receipt, raw = _json(path)
    require(type(receipt.get("schema")) is int and receipt["schema"] == 1
            and receipt.get("kind") == "predeclared_setup_retry" and receipt.get("plan_sha256") == plan_sha
            and receipt.get("preserved_directory") == "setup-failures-attempt1"
            and isinstance(receipt.get("original_attempt_source_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", receipt["original_attempt_source_commit"]), "retry receipt is unbound")
    for key, expected in (("preserved_launcher_trials", 10), ("original_model_calls", 0), ("original_model_attempts", 0)):
        require(type(receipt.get(key)) is int and receipt[key] == expected, "retry receipt changes the setup denominator")
    retry = receipt.get("retry")
    require(isinstance(retry, dict) and retry.get("target_directory") == "targets"
            and type(retry.get("planned_trials")) is int and retry["planned_trials"] == 10
            and retry.get("task_model_budget_arms_and_source_knowledge_unchanged") is True,
            "retry changes the frozen trial plan")
    diagnosis = receipt.get("diagnosis")
    require(isinstance(diagnosis, dict) and diagnosis.get("stage") == "repository.checkout.git_fetch"
            and diagnosis.get("reproduced_without_model") is True and diagnosis.get("platform") == "Windows"
            and isinstance(diagnosis.get("error"), str) and 0 < len(diagnosis["error"]) <= 1000,
            "retry lacks its explicit setup-only diagnosis")
    require(isinstance(retry.get("private_output"), str) and len(retry["private_output"]) <= 200
            and re.fullmatch(r"runs/[A-Za-z0-9_.-]+", retry["private_output"])
            and isinstance(retry.get("runtime_fix"), str) and 0 < len(retry["runtime_fix"]) <= 1000,
            "retry must state its bounded local fix and fresh private output")
    expected_paths = {"setup-failures-attempt1/execution.json"}
    for row in planned:
        for filename in ("summary.json", "knowledge-input.json"):
            expected_paths.add("setup-failures-attempt1/targets/" + row["label"] + "/" + filename)
    files = receipt.get("preserved_files")
    require(isinstance(files, list) and len(files) == 21 and all(isinstance(item, dict) for item in files),
            "retry receipt must preserve every setup artifact")
    require({item.get("path") for item in files} == expected_paths, "retry receipt omits or repeats a setup artifact")
    # Keep the published original receipt byte-for-byte. The correction records
    # task exports that ordinary Win32 stat/enumeration silently missed at >260
    # characters; it must bind the original receipt and exactly those ten files.
    correction, correction_raw = _json(correction_path)
    require(type(correction.get("schema")) is int and correction["schema"] == 1
            and correction.get("kind") == "windows_archive_inventory_correction"
            and correction.get("original_receipt_sha256") == digest(raw)
            and correction.get("original_receipt_unchanged") is True
            and type(correction.get("complete_preserved_file_count")) is int
            and correction["complete_preserved_file_count"] == 31
            and type(correction.get("original_model_calls")) is int and correction["original_model_calls"] == 0,
            "archive correction is unbound or changes the setup accounting")
    expected_task_exports = {}
    for wanted in planned:
        run_id = wanted["task"].instance_id + "-" + wanted["profile"] + "-" + wanted["label"]
        relative = "setup-failures-attempt1/targets/" + wanted["label"] + "/model-runs/" + run_id + "/task.json"
        expected_task_exports[relative] = asdict(wanted["task"])
        expected_task_exports[relative]["public_checks"] = list(wanted["task"].public_checks)
    additional = correction.get("additional_preserved_files")
    require(isinstance(additional, list) and len(additional) == 10
            and all(isinstance(item, dict) for item in additional)
            and {item.get("path") for item in additional} == set(expected_task_exports),
            "archive correction must contain exactly the ten planned task exports")
    files = files + additional
    expected_paths.update(expected_task_exports)
    # A zero-call retry may not omit an unlisted model evidence artifact from
    # its archive. Bound enumeration and reject links before reading entries.
    actual_paths = set()
    for index, candidate in enumerate((root / receipt["preserved_directory"]).rglob("*")):
        require(index < 64 and not candidate.is_symlink(), "setup archive contains a link or excessive entries")
        if candidate.is_file():
            actual_paths.add(candidate.relative_to(root).as_posix())
    require(actual_paths == expected_paths, "setup archive has missing or unlisted artifacts")
    loaded = {}
    for item in files:
        resolved = artifact(root, item["path"], max_bytes=2 * 1024 * 1024)
        value, blob = _json(resolved)
        require(type(item.get("bytes")) is int and item["bytes"] == len(blob)
                and item.get("sha256") == digest(blob), "preserved setup artifact hash/length differs")
        loaded[item["path"]] = value
        if item["path"] in expected_task_exports:
            _equal(value, expected_task_exports[item["path"]], "archived task export differs from the frozen task")
    execution = loaded["setup-failures-attempt1/execution.json"]
    require(type(execution.get("schema")) is int and execution["schema"] == 1
            and execution.get("fixture") is False and execution.get("plan_sha256") == plan_sha
            and type(execution.get("max_parallel_trials")) is int and execution["max_parallel_trials"] == 2,
            "original launcher execution differs from plan")
    ordered, outcomes = execution.get("planned_order"), execution.get("outcomes")
    labels = {row["label"] for row in planned}
    require(isinstance(ordered, list) and len(ordered) == 10 and all(isinstance(row, dict) for row in ordered)
            and {row.get("label") for row in ordered} == labels
            and isinstance(outcomes, list) and len(outcomes) == 10 and all(isinstance(row, dict) for row in outcomes)
            and {row.get("label") for row in outcomes} == labels, "original launcher trial inventory differs")
    require([row["label"] for row in ordered] == [row["label"] for row in sorted(planned, key=lambda row: row["repetition"])],
            "original launcher ordering differs from the declared repetition order")
    setup_rows = []
    for wanted in planned:
        label = wanted["label"]
        value = loaded["setup-failures-attempt1/targets/" + label + "/summary.json"]
        package_path = "setup-failures-attempt1/targets/" + label + "/knowledge-input.json"
        package = loaded[package_path]
        package_ref = next(item for item in files if item["path"] == package_path)
        expected = {"schema": 1, "kind": "real_codex_source_trial", "fixture": False,
            "source_commit": receipt["original_attempt_source_commit"], "source_tracked_tree_clean": True,
            "source_task_sha256": wanted["frozen_task_sha256"], "source_task_fingerprint": wanted["task"].fingerprint(),
            "model_name": plan["model"], "provider": plan["provider"], "profile": wanted["profile"], "label": label,
            "model_call_unit": "cli_invocations", "attempt_started": False, "attempted_count": 0,
            "context_requests": 0, "model_calls": 0, "model_status": "failed", "submitted": False,
            "model_identity": None, "patch_sha256": None, "patch_valid": None, "exit_status": None,
            "knowledge_observation": None, "complete": False, "delivery_success": None, "official_resolved": None,
            "underlying_transport_request_count": None, "error_type": "RuntimeError", "export_error_type": "ValueError"}
        for key, expected_value in expected.items():
            require(key in value, "setup outcome field is missing")
            _equal(value[key], expected_value, "setup outcome contains a model attempt or differs from frozen plan")
        measured = _measurements(value)
        require(measured["usage_complete"] is True and measured["input_tokens"] == measured["output_tokens"] == 0,
                "setup-only receipt contains model token usage")
        require(_number(measured["seconds"]), "setup-only duration is missing")
        _budgets(value.get("budgets"), plan)
        knowledge = value.get("knowledge_input")
        _knowledge_input(package, knowledge, package_ref["sha256"], wanted, source)
        original_plan = next(row for row in ordered if row["label"] == label)
        for key, expected_value in (("arm", wanted["arm"]), ("profile", wanted["profile"]),
                ("repetition", wanted["repetition"]), ("task_sha256", wanted["frozen_task_sha256"]),
                ("task", plan["targets"][0 if wanted["task_group"] == "last" else 1]["task"])):
            _equal(original_plan.get(key), expected_value, "original launcher row differs from frozen plan")
        outcome = next(row for row in outcomes if row["label"] == label)
        require(type(outcome.get("launcher_returncode")) is int and outcome["launcher_returncode"] != 0
                and outcome.get("launcher_timed_out") is False and outcome.get("summary_present") is True,
                "original setup launcher did not record its failure")
        require(_number(outcome.get("launcher_elapsed_seconds"))
                and outcome["launcher_elapsed_seconds"] >= measured["seconds"],
                "setup launcher duration is missing or shorter than its trial")
        setup_rows.append({"label": label, "model_calls": measured["model_calls"], "model_attempts": 0,
            "input_tokens": measured["input_tokens"], "output_tokens": measured["output_tokens"],
            "seconds": measured["seconds"], "launcher_elapsed_seconds": outcome["launcher_elapsed_seconds"],
            "error_type": value.get("error_type"),
            "summary_sha256": next(item["sha256"] for item in files if item["path"] ==
                                   "setup-failures-attempt1/targets/" + label + "/summary.json")})
    return {"receipt": {"path": "retry-receipt.json", "sha256": digest(raw)}, "evidence_status": "validated",
        "inventory_correction": {"path": "retry-receipt-correction.json", "sha256": digest(correction_raw),
            "original_receipt_sha256": digest(raw), "original_receipt_file_count": 21, "additional_task_exports": 10,
            "complete_preserved_file_count": 31},
        "preserved_directory": receipt["preserved_directory"], "original_source_commit": receipt["original_attempt_source_commit"],
        "preserved_artifacts": files, "launcher_failures": 10, "model_calls": 0, "model_attempts": 0,
        "setup_rows": setup_rows, "excluded_from_model_attempt_denominator": True,
        "trial_seconds_sum": sum(row["seconds"] for row in setup_rows),
        "launcher_seconds_sum": sum(row["launcher_elapsed_seconds"] for row in setup_rows),
        "time_scope": "Summed setup durations overlap under two concurrent launchers; they are not wall-clock batch latency.",
        "interpretation": "All ten original setup failures are preserved; recorded zero model attempts are checked against their original summaries."}


def summarize(root, verification):
    root, verification = extended_path(Path(root).resolve()), extended_path(Path(verification).resolve())
    plan, plan_sha, planned = _plan(root)
    source = None
    try:
        source, source_cost = _source(root, plan)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        source_cost = {"evidence_status": "missing" if isinstance(exc, FileNotFoundError) else "invalid",
                       "error_type": type(exc).__name__, "delivery_success": None, "excluded_from_target_denominator": True}
    # A present but corrupted retry history is fatal, rather than silently
    # publishing a clean-looking second attempt with the first one omitted.
    setup_history = _setup_history(root, plan, plan_sha, planned, source)
    from scripts.retry_codex_transfer import summary_history
    subset_setup_history = summary_history(root)
    require(subset_setup_history is None or setup_history is not None,
            "the second setup retry cannot omit the first preserved setup history")
    rows = []
    for wanted in planned:
        row = {key: value for key, value in wanted.items() if key != "task"}
        row.update(evidence_status="missing", verification_status="missing", delivery_success=None,
                   submitted=None, attempt_started=None, knowledge_observed_read=None,
                   input_tokens=None, output_tokens=None, model_calls=None, seconds=None, usage_complete=None)
        directory = root / "targets" / wanted["label"]
        try:
            run, summary, entry, refs, measured = _trial(directory, wanted["profile"], wanted["task"], wanted["frozen_task_sha256"], plan,
                                                        wanted["label"])
            # Preserve corroborated measurements even if a later knowledge or
            # grading artifact is invalid; a failed gate cannot erase spent calls.
            row.update(measured, evidence_status="validated", artifact_root="targets/" + wanted["label"], artifacts=refs)
            knowledge = _knowledge(directory, run, summary, entry, wanted, source)
            row.update(knowledge=knowledge,
                       knowledge_observed_read=knowledge["knowledge_observed_read"])
            try:
                outcome, report_sha, reason, image = _external(verification / wanted["label"] / "verification.json",
                    run, refs["manifest"]["sha256"])
                row.update(delivery_success=outcome, verification_status="validated" if reason is None else reason,
                           verification_sha256=report_sha, verification_image=image,
                           verification_artifact={"relative_to": "verification_root",
                               "path": wanted["label"] + "/verification.json", "sha256": report_sha})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                row.update(verification_status="missing" if isinstance(exc, FileNotFoundError) else "invalid",
                           verification_error_type=type(exc).__name__)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row.update(evidence_status="missing" if isinstance(exc, FileNotFoundError) else "invalid",
                       evidence_error_type=type(exc).__name__)
            if (directory / "summary.json").is_file():
                try:
                    partial, blob = _json(directory / "summary.json")
                    row["uncorroborated_summary_measurements"] = _measurements(partial)
                    row["uncorroborated_summary_sha256"] = digest(blob)
                except (ValueError, OSError, KeyError, TypeError):
                    pass
        rows.append(row)
    paired = []
    for repetition in plan["repetitions"]:
        empty, seeded = [next(row for row in rows if row["label"] == f"last-retrieval-{arm}-r{repetition}")
                         for arm in ("empty", "verified")]
        comparable = (empty["delivery_success"] is True and seeded["delivery_success"] is True
            and empty.get("model_identity") == seeded.get("model_identity")
            and empty.get("implementation_sha256") == seeded.get("implementation_sha256")
            and empty.get("verification_image") == seeded.get("verification_image"))
        differences = {}
        for key in METRICS:
            known = comparable and _number(empty.get(key)) and _number(seeded.get(key))
            if key.endswith("tokens"):
                known = known and empty.get("usage_complete") is True and seeded.get("usage_complete") is True
            differences[key] = {"verified_minus_empty": seeded[key] - empty[key] if known else None,
                "relative_change": (seeded[key] - empty[key]) / empty[key] if known and empty[key] else None}
        paired.append({"repetition": repetition, "empty": empty["label"], "verified": seeded["label"],
            "both_independently_delivered_and_comparable": comparable,
            "knowledge_observed_read": seeded["knowledge_observed_read"], "differences": differences})
    groups = {group + "/" + arm: _aggregate([row for row in rows if row["task_group"] == group and row["arm"] == arm])
              for group, arm in dict.fromkeys((row["task_group"], row["arm"]) for row in rows)}
    return {"schema": 1, "kind": "independently_checked_codex_knowledge_transfer_summary", "fixture": False,
        "plan_sha256": plan_sha, "planned_target_trials": 10, "rows": rows, "groups": groups,
        "setup_retry_history": setup_history,
        "additional_setup_retry_history": subset_setup_history,
        "preserved_zero_call_launcher_failures": sum(history["launcher_failures"]
            for history in (setup_history, subset_setup_history) if history is not None),
        "targets": _aggregate(rows), "source": source_cost, "knowledge_only_pairs": paired,
        "complete": all(row["delivery_success"] is not None for row in rows),
        "limits": ["Two repetitions are not controlled seeded samples; no statistical or causal conclusion.",
            "Knowledge reads prove nonempty tool delivery, not incorporation or usefulness.",
            "Elapsed repository wall time includes host scheduling and concurrent trial contention; it is not pure LLM latency.",
            "CLI invocation counts do not expose internal transport retries; token figures are CLI-reported measurements.",
            "Known token subtotals with incomplete usage are lower bounds; missing trials are never assigned zero.",
            "Remote weights and dollar costs are unknown; source cost is separate from the target denominator."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(not args.output.exists(), "summary output must be fresh")
    result = summarize(args.root, args.verification)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"planned": 10, **{key: result["targets"][key] for key in ("successes", "failures", "unknown")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
