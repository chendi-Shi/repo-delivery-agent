"""Summarize a predeclared coding-agent cohort without dropping failed attempts.

The plan freezes model/common configuration, targets, seeds and arms. Each target
may name an arm subset; its Cartesian product with seeds is the denominator.
Observations reference typed ``closure_trial_receipt`` JSON files, which in turn
bind the runner's safe summary/evidence/verification artifacts by relative path
and SHA256. Source seed attempts are a separate cohort, never target successes.

This is an artifact contract, not a provider or benchmark verifier. Receipts must
be constructed from actual runner results and independent verification. No
policy estimate is accepted as token usage. Input fixtures are rejected; unit
tests of this contract do not constitute model-performance evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import shlex
from pathlib import Path

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 2048
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
STATUSES = {"submitted", "stopped", "failed", "infrastructure_failed"}
BUDGET_FIELDS = {"max_steps", "context_tokens", "max_output_tokens", "wall_seconds", "request_timeout",
                 "command_timeout", "cost_limit", "temperature"}
TASK_FIELDS = {"task_sha256", "task_fingerprint", "repo", "base_commit", "inference_image_id",
               "public_base_image_id", "public_checks_sha256"}
COMMON_FIELDS = {"provider", "backend", "tools_sha256", "implementation_sha256", "budgets"}


class SummaryError(ValueError):
    """Invalid, contradictory or unbound evidence; messages contain no raw data."""


class MissingArtifact(SummaryError):
    """A declared row is unavailable and remains in the cohort denominator."""


def _check(condition, message):
    if not condition:
        raise SummaryError(message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(value, message):
    _check(isinstance(value, dict), message)
    return value


def _label(value):
    _check(isinstance(value, str) and LABEL.fullmatch(value), "identifier must be bounded public metadata")
    return value


def _integer(value, *, nullable=False):
    _check((nullable and value is None) or (type(value) is int and 0 <= value <= 10 ** 15),
           "counter must be a bounded nonnegative integer")
    return value


def _number(value, *, nullable=False):
    _check((nullable and value is None) or (type(value) in (int, float) and math.isfinite(value) and
                                          0 <= value <= 10 ** 15), "measurement must be finite and nonnegative")
    return value


def _boolean(value, *, nullable=False):
    _check(type(value) is bool or (nullable and value is None), "outcome must be a boolean or explicit null")
    return value


def _pairs(values):
    result = {}
    for key, value in values:
        _check(key not in result, "duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _read(path):
    path = Path(path)
    if not path.is_file():
        raise MissingArtifact("declared artifact is missing")
    _check(not path.is_symlink() and path.stat().st_size <= MAX_JSON_BYTES, "JSON artifact violates bounds")
    raw = path.read_bytes()
    _check(len(raw) <= MAX_JSON_BYTES, "JSON artifact changed beyond bounds")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SummaryError("nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SummaryError("artifact is not bounded UTF-8 JSON") from exc
    return _object(value, "artifact must be a JSON object"), _sha(raw)


def _artifact_path(root, ref):
    ref = _object(ref, "artifact reference must be an object")
    _check(set(ref) == {"path", "sha256"} and SHA.fullmatch(str(ref.get("sha256", ""))),
           "artifact reference must include SHA256")
    value = ref.get("path")
    _check(isinstance(value, str) and len(value) <= 300 and "\\" not in value and ":" not in value,
           "artifact path must be a bounded portable relative path")
    parts = value.split("/")
    devices = {"con", "prn", "aux", "nul", *("com" + str(x) for x in range(1, 10)),
               *("lpt" + str(x) for x in range(1, 10))}
    _check(bool(parts) and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", part) and
                              not part.endswith(".") and part.split(".")[0].lower() not in devices for part in parts),
           "artifact path may not escape or name devices")
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    _check(path.resolve().is_relative_to(root) and not any(item.is_symlink() for item in
           [path, *list(path.parents)[:len(parts) - 1]]), "artifact path may not escape or traverse symlinks")
    return path


def _reference(root, ref):
    path = _artifact_path(root, ref)
    value, digest = _read(path)
    _check(digest == ref["sha256"], "artifact SHA256 does not match its receipt")
    return value, {"path": ref["path"], "sha256": digest}


def _common(value):
    value = _object(value, "common configuration must be an object")
    _check(set(value) == COMMON_FIELDS, "common configuration fields differ from the contract")
    _check(isinstance(value["provider"], str) and isinstance(value["backend"], str) and
           value["provider"] in {"ollama_native", "litellm"} and value["backend"] in {"docker", "files"},
           "cohort requires a real provider and recorded execution backend")
    _check(all(SHA.fullmatch(str(value[key])) for key in ("tools_sha256", "implementation_sha256")),
           "tool and implementation identities must be SHA256")
    budgets = _object(value["budgets"], "budgets must be an object")
    _check(set(budgets) == BUDGET_FIELDS, "all common budgets must be frozen")
    for key, item in budgets.items():
        _number(item)
        if key not in {"temperature", "cost_limit"}:
            _integer(item)
            _check(item > 0, "integer budgets must be positive")
    _check(budgets["cost_limit"] > 0 and budgets["max_output_tokens"] + 1000 < budgets["context_tokens"],
           "invalid context reservation or cost budget")
    return value


def _model(value, *, unresolved=False):
    value = _object(value, "model identity must be an object")
    _check(set(value) == {"name", "digest"}, "model identity fields differ from the contract")
    _check(isinstance(value["name"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", value["name"]),
           "model name must be bounded metadata")
    _check((unresolved and value["digest"] is None) or SHA.fullmatch(str(value["digest"])),
           "model weight identity must be resolved SHA256")
    return value


def _task(value, *, unresolved=False):
    _object(value, "task identity must be an object")
    _check(set(value) == TASK_FIELDS, "task identity fields differ from the contract")
    for key in ("task_sha256", "task_fingerprint", "public_checks_sha256"):
        _check(SHA.fullmatch(str(value[key])), "source and check identities must be SHA256")
    _check(isinstance(value["repo"], str) and len(value["repo"]) <= 200 and
           re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repo"]),
           "repository identity must be public owner/name")
    _check(re.fullmatch(r"[0-9a-f]{40}", str(value["base_commit"])), "repository revision must be immutable")
    for key in ("inference_image_id", "public_base_image_id"):
        _check((unresolved and value[key] is None) or IMAGE.fullmatch(str(value[key])),
               "runtime image identity must be immutable")
    return value


def _plan(value):
    _check(value.get("schema") == 1 and value.get("kind") == "fixed_closure_cohort" and value.get("fixture") is False
           and value.get("frozen_before_execution") is True, "plan must declare a real pre-execution frozen cohort")
    _label(value.get("suite_id"))
    _model(value.get("model")); _common(value.get("common"))
    seeds = value.get("seeds")
    _check(isinstance(seeds, list) and 1 <= len(seeds) <= 64 and all(type(x) is int for x in seeds)
           and len(set(seeds)) == len(seeds),
           "seeds must be bounded and unique")
    for seed in seeds:
        _integer(seed)
    arms = value.get("arms")
    _check(isinstance(arms, list) and 1 <= len(arms) <= 16, "arms must be a bounded list")
    arm_map = {}
    for arm in arms:
        _object(arm, "arm must be an object")
        _check(set(arm) == {"id", "profile", "knowledge"}, "arm fields differ from the contract")
        name = _label(arm["id"])
        _check(name not in arm_map and isinstance(arm["profile"], str) and isinstance(arm["knowledge"], str) and
               arm["profile"] in {"baseline", "recent", "retrieval"} and
               arm["knowledge"] in {"none", "verified_source"}, "invalid or duplicate arm")
        arm_map[name] = arm
    reference = value.get("reference_arm", arms[0]["id"])
    _check(isinstance(reference, str) and reference in arm_map, "reference arm must be predeclared")
    targets = value.get("targets")
    _check(isinstance(targets, list) and 1 <= len(targets) <= 128, "targets must be a bounded list")
    target_map, expected = {}, {}
    for target in targets:
        _object(target, "target must be an object")
        _check(set(target) in (TASK_FIELDS | {"instance_id", "role"}, TASK_FIELDS | {"instance_id", "role", "arms"}),
               "target fields differ from the contract")
        identity = _label(target["instance_id"])
        _check(identity not in target_map and isinstance(target["role"], str) and
               target["role"] in {"near_transfer", "holdout"}, "invalid target identity or role")
        _task({key: target[key] for key in TASK_FIELDS})
        selected = target.get("arms", list(arm_map))
        _check(isinstance(selected, list) and selected and all(isinstance(x, str) and x in arm_map for x in selected)
               and len(set(selected)) == len(selected) and reference in selected, "target arms must name a unique reference-inclusive subset")
        _check(target["role"] == "near_transfer" or all(arm_map[x]["knowledge"] == "none" for x in selected),
               "verified-source transfer arms must be separate from holdouts")
        target_map[identity] = target
        for seed, arm in itertools.product(seeds, selected):
            expected[(identity, seed, arm)] = target
    _check(len(expected) <= MAX_ROWS, "cohort exceeds the trial bound")
    if "expected" in value:
        rows = value["expected"]
        _check(isinstance(rows, list) and len(rows) == len(expected), "explicit expected rows must equal the frozen Cartesian cohort")
        keys = [_key(row) for row in rows]
        _check(len(set(keys)) == len(keys) and set(keys) == set(expected), "explicit expected rows differ from frozen cohort")
    sources = value.get("source_attempts", [])
    _check(isinstance(sources, list) and len(sources) <= MAX_ROWS, "source attempts must be bounded")
    source_keys = []
    for source in sources:
        _object(source, "source attempt must be an object")
        _check(set(source) == {"instance_id", "seed"}, "predeclared source attempts contain only identity and seed")
        source_keys.append((_label(source["instance_id"]), _integer(source["seed"])))
    _check(len(set(source_keys)) == len(source_keys) and not any(x[0] in target_map for x in source_keys),
           "source attempts must be separate unique task identities")
    return arm_map, target_map, expected, reference, source_keys


def _key(value):
    _object(value, "trial row must be an object")
    return (_label(value.get("instance_id")), _integer(value.get("seed")), _label(value.get("arm")))


def _checks(internal, external):
    return True if internal is True and external is True else False if internal is False or external is False else None


def _check_rows(rows, expected_sha):
    """Bind actual check outcomes to the complete frozen command list."""
    _check(isinstance(rows, list) and bool(rows), "known verification requires nonempty actual checks")
    commands = []
    for row in rows:
        _object(row, "verification check must be an object")
        command = row.get("command")
        _check(isinstance(command, str) and bool(command.strip()), "verification check requires its actual command")
        _check(type(row.get("returncode")) is int and type(row.get("passed")) is bool,
               "verification check requires an actual return code and outcome")
        _check(row["passed"] is (row["returncode"] == 0), "verification check outcome contradicts its return code")
        commands.append(command)
    digest = _sha(json.dumps(commands, ensure_ascii=False, separators=(",", ":")).encode())
    _check(digest == expected_sha, "verification commands differ from the frozen task")
    return all(row["passed"] for row in rows)


def _observed_read(root, evidence_ref, evidence, plan, plan_sha256, source_attempts):
    """Recheck a positive read claim from public audit and verified seed artifacts."""
    _check(SHA.fullmatch(str(plan_sha256)), "observed source read requires the exact frozen plan digest")
    package, package_hash = _read(Path(root) / "seed-package.json")
    _check(package.get("schema") == 1 and package.get("kind") == "verified_closure_seed_package"
           and package.get("fixture") is False and package.get("eligible") is True
           and package.get("plan_sha256") == plan_sha256, "source read requires an eligible frozen seed package")
    source, source_ref = _reference(root, package.get("source_receipt"))
    matched = [row for row in source_attempts if row.get("receipt") == source_ref]
    _check(len(matched) == 1 and matched[0].get("delivery_success") is True
           and source.get("role") == "source" and source.get("knowledge") == "none"
           and source.get("knowledge_observed_reuse") is False,
           "seed source must be the independently delivered source in this cohort")
    guard, guard_ref = _reference(root, package.get("guard"))
    targets = guard.get("functional_targets")
    _check(guard.get("schema") == 1 and guard.get("kind") == "source_seed_leakage_guard"
           and guard.get("eligible") is True and guard.get("violations") == []
           and guard.get("omitted_violations") == 0 and isinstance(targets, list)
           and len(targets) == len(plan["targets"]), "source read requires its complete leakage guard")
    _check(all(isinstance(row, dict) and row.get("target_feature_still_missing") is True
               and type(row.get("returncode")) is int and row["returncode"] != 0 for row in targets)
           and sorted(row.get("instance_id", "") for row in targets) ==
               sorted(row["instance_id"] for row in plan["targets"]),
           "source guard must preserve every predeclared target feature gap")
    origin = _object(package.get("origin"), "seed package requires source identity")
    record = _object(package.get("record"), "seed package requires verified source content")
    required = ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref", "verification_sha256")
    _check(all(field in origin for field in required) and origin["verified"] is True and record.get("verified") is True
           and record.get("task_id") == origin["task_id"] == source.get("instance_id")
           and record.get("source_ref") == origin["source_ref"]
           and isinstance(record.get("body"), str) and _sha(record["body"].encode()) == origin["content_sha256"]
           and isinstance(plan.get("knowledge_namespace"), str)
           and _sha(plan["knowledge_namespace"].encode()) == origin["namespace_sha256"],
           "seed content and namespace differ from the frozen verified origin")
    verification = _object(record.get("verification"), "seed content requires verification identity")
    _check(_sha(json.dumps(verification, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode()) == origin["verification_sha256"],
           "seed verification differs from the audited origin")
    source_evidence, _ = _reference(root, source["artifacts"]["evidence"])
    _check(verification.get("kind") == "independent_public_checks" and verification.get("patch_valid") is True
           and verification.get("run_status") == "submitted"
           and SHA.fullmatch(str(verification.get("patch_sha256")))
           and verification["patch_sha256"] == source_evidence.get("patch_sha256")
           and package.get("source_task_sha256") == source["task"]["task_sha256"],
           "seed verification differs from the delivered source task and patch")
    _check(_check_rows(verification.get("checks"), source["task"]["public_checks_sha256"]),
           "seed verification requires its actual passing source checks")
    _check(record.get("repo") == source["task"]["repo"] and record.get("base_commit") == source["task"]["base_commit"],
           "seed repository differs from its delivered source")
    identity = _sha(json.dumps([record.get("source_type"), record.get("source_ref"), record.get("title")],
                               separators=(",", ":")).encode())
    _check(origin["source_id"] == "k" + _sha((origin["namespace_sha256"] + identity).encode())[:32],
           "seed source ID differs from its source record")
    audit_metadata = _object(evidence.get("action_audit"), "source read requires the model action audit")
    _check(audit_metadata.get("available") is True, "source read requires an available action audit")
    relative = evidence_ref["path"].rsplit("/", 1)
    audit_ref = {"path": (relative[0] + "/" if len(relative) == 2 else "") + "action-audit.jsonl",
                 "sha256": audit_metadata.get("public_audit_sha256")}
    path = _artifact_path(root, audit_ref)
    if not path.is_file():
        raise MissingArtifact("declared action audit is missing")
    _check(path.stat().st_size <= MAX_AUDIT_BYTES, "source read audit exceeds its byte bound")
    raw = path.read_bytes()
    _check(len(raw) <= MAX_AUDIT_BYTES and _sha(raw) == audit_ref["sha256"], "source read audit differs from its evidence digest")
    try:
        lines = raw.decode("utf-8").splitlines()
        _check(len(lines) <= 10000, "source read audit exceeds its action bound")
        rows = [json.loads(line, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SummaryError("nonfinite audit number")))
                for line in lines]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SummaryError("source read audit is not bounded UTF-8 JSONL") from exc
    _check(len(rows) == audit_metadata.get("actions") and all(isinstance(row, dict) for row in rows),
           "source read audit action count differs from its evidence")
    def nonempty_slice(row):
        # The real read implementation verifies the body hash and returns this
        # character slice. A full hash-bound command plus the frozen body proves
        # nonempty content even when the response preview contains only metadata.
        command = row.get("command_preview")
        if (not isinstance(command, str) or row.get("command_chars") != len(command)
                or row.get("command_sha256") != _sha(command.encode())):
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split, lexer.commenters = True, ""
            tokens = list(lexer)
            if (not 3 <= len(tokens) <= 5 or tokens[:3] != ["delivery-knowledge", "read", origin["source_id"]]
                    or any(set(token) <= set(";&|<>()") for token in tokens)):
                return False
            offset = int(tokens[3]) if len(tokens) > 3 else 0
            limit = int(tokens[4]) if len(tokens) > 4 else 2000
            return 0 <= offset < len(record["body"]) and 1 <= limit <= 4000
        except ValueError:
            return False
    found = any(row.get("event") == "action" and row.get("outcome") == "completed"
        and type(row.get("returncode")) is int and row["returncode"] == 0
        and row.get("action_kind") == "delivery-knowledge/read" and row.get("knowledge_operation") == "read"
        and row.get("knowledge_namespace_sha256") == origin["namespace_sha256"]
        and isinstance(row.get("knowledge_records"), list)
        and any(isinstance(item, dict) and all(type(item.get(field)) is type(origin[field])
                and item[field] == origin[field] for field in required) for item in row["knowledge_records"])
        and nonempty_slice(row)
        for row in rows)
    _check(found, "positive source read claim has no matching successful audited nonempty read")
    return {"action_audit": audit_ref, "seed_package": {"path": "seed-package.json", "sha256": package_hash},
            "source_receipt": source_ref, "source_guard": guard_ref}


def _receipt(value, key, target, arm, plan, root, *, plan_sha256=None, source_attempts=()):
    _check(value.get("schema") == 1 and value.get("kind") == "closure_trial_receipt" and value.get("fixture") is False,
           "trial receipt must bind a real run, never a fixture")
    _check(_key(value) == key and value.get("role") == target["role"], "trial identity differs from frozen cohort")
    status = value.get("status")
    _check(isinstance(status, str) and status in STATUSES, "trial status is unsupported")
    infra = _boolean(value.get("infrastructure_failed", status == "infrastructure_failed"))
    _check(status != "infrastructure_failed" or infra, "infrastructure status must remain unknown")
    started = _boolean(value.get("attempt_started"))
    attempted = _integer(value.get("attempted_count"))
    _check(attempted == int(started) and (started or infra), "actual attempt count must distinguish blocked-before-model rows")
    model = _model(value.get("model"), unresolved=infra)
    _check(model["name"] == plan["model"]["name"] and model["digest"] in
           ({plan["model"]["digest"], None} if infra else {plan["model"]["digest"]}), "trial model differs from frozen weights")
    _check(_common(value.get("common")) == plan["common"], "trial provider, tools, implementation or budgets differ")
    _check(value.get("profile") == arm["profile"] and value.get("knowledge") == arm["knowledge"], "trial arm differs from its intended intervention")
    task = _task(value.get("task"), unresolved=infra)
    _check(all(task[field] == target[field] or (infra and field.endswith("image_id") and task[field] is None)
               for field in TASK_FIELDS), "trial repository, checks or runtime images differ from frozen target")
    submitted = _boolean(value.get("submitted"))
    exit_status = value.get("exit_status")
    if exit_status is not None:
        _label(exit_status)
    _check(submitted == (status == "submitted") and submitted == (status == "submitted" and exit_status == "Submitted") and
           (exit_status != "Submitted" or submitted or (infra and status == "failed")),
           "submission requires the actual Submitted exit status")
    internal = _boolean(value.get("internal_checks_passed"), nullable=True)
    external = _boolean(value.get("external_checks_passed"), nullable=True)
    delivery = None if infra else False if not submitted else _checks(internal, external)
    _check(_boolean(value.get("delivery_success"), nullable=True) is delivery, "delivery success contradicts submission and independent checks")
    calls, seconds = _integer(value.get("model_calls"), nullable=True), _number(value.get("seconds"), nullable=True)
    _check(started or (not submitted and calls == 0), "blocked-before-model trials cannot claim model calls or submission")
    usage = _object(value.get("usage"), "actual provider usage must be an object")
    _check(set(usage) == {"complete", "input_tokens", "output_tokens", "calls_with_usage", "unknown_requests"},
           "usage must identify actual coverage, never policy estimates")
    _boolean(usage["complete"])
    input_tokens, output_tokens = (_integer(usage[x], nullable=True) for x in ("input_tokens", "output_tokens"))
    with_usage, unknown = (_integer(usage[x]) for x in ("calls_with_usage", "unknown_requests"))
    measured = (usage["complete"] is True and calls is not None and calls == with_usage and unknown == 0 and
                input_tokens is not None and output_tokens is not None)
    artifacts = _object(value.get("artifacts"), "trial must bind source evidence artifacts")
    _check(set(artifacts) == {"summary", "evidence", "verification"}, "trial artifact bindings differ from the contract")
    refs, bound = {}, {}
    for name, ref in artifacts.items():
        if ref is None:
            _check(infra and name != "summary", "non-infrastructure trials require all evidence artifacts")
            refs[name] = None
        else:
            source, refs[name] = _reference(root, ref)
            _check(source.get("fixture") is not True, "fixture artifacts cannot supply model-effect evidence")
            bound[name] = source
    actual_summary = bound["summary"]
    requests = _integer(actual_summary.get("observed_provider_requests"), nullable=True)
    _check(requests is None or (requests > 0) is started,
           "observed provider request count contradicts whether a model attempt started")
    for field, expected_value in {"instance_id": key[0], "attempt_started": started,
            "attempted_count": attempted, "model_status": status, "status": status,
            "exit_status": exit_status, "model_digest": model["digest"], "seconds": seconds,
            "internal_checks_passed": internal, "external_checks_passed": external,
            "delivery_success": delivery}.items():
        if field in actual_summary:
            _check(type(actual_summary[field]) is type(expected_value) and actual_summary[field] == expected_value,
                   "receipt differs from its actual trial summary")
    evidence = bound.get("evidence")
    if evidence is not None:
        _check(evidence.get("fixture") is False and evidence.get("provider") == plan["common"]["provider"] and
               evidence.get("model_digest") == model["digest"] and evidence.get("profile") == arm["profile"] and
               evidence.get("status") == status and evidence.get("exit_status") == exit_status,
               "receipt differs from its actual model evidence")
        _check(evidence.get("implementation_sha256") == plan["common"]["implementation_sha256"],
               "receipt implementation differs from actual evidence")
        expected_budgets = plan["common"]["budgets"]
        evidence_budgets = _object(evidence.get("budgets"), "evidence must record actual budgets")
        _check(all(evidence_budgets.get(key) == item for key, item in expected_budgets.items() if key != "temperature"),
               "receipt budgets differ from actual evidence")
        _check(evidence_budgets.get("seed") == key[1], "receipt seed differs from actual provider configuration")
        if "temperature" in evidence_budgets:
            _check(evidence_budgets["temperature"] == expected_budgets["temperature"], "actual sampling temperature differs")
        if "model_calls" in evidence:
            _check(evidence["model_calls"] == calls, "actual provider call count differs from evidence")
        recorded_usage = _object(evidence.get("usage", {}), "evidence usage must be an object")
        _check(not measured or all(recorded_usage.get(field) == usage[field] for field in usage),
               "complete token measurement differs from actual provider evidence")
        _check(evidence.get("seconds") == seconds, "elapsed time differs from actual evidence")
        internal_status = evidence.get("internal_verification_status")
        if internal_status in {"passed", "failed"}:
            actual_internal = _check_rows(evidence.get("internal_public_checks"), task["public_checks_sha256"])
            _check(actual_internal is (internal_status == "passed") and internal is actual_internal,
                   "internal check receipt differs from actual verification evidence")
        else:
            _check(internal is None, "unknown internal verification cannot claim a check outcome")
        bindings = _object(evidence.get("bindings"), "evidence must record task/runtime bindings")
        for evidence_field, task_field in (("source_task_sha256", "task_sha256"),
                                          ("source_task_fingerprint", "task_fingerprint"),
                                          ("inference_image_id", "inference_image_id"),
                                          ("public_base_image_id", "public_base_image_id")):
            _check(bindings.get(evidence_field) == task[task_field], "receipt source or image identity differs from actual evidence")
    verification = bound.get("verification")
    if verification is not None:
        failures = verification.get("infrastructure_failures", [])
        _check(isinstance(failures, list), "verification infrastructure outcomes must be a list")
        records = verification.get("runs", [])
        _check(isinstance(records, list), "verification records must be a list")
        matched = [row for row in records if isinstance(row, dict) and row.get("instance_id") == key[0]]
        if not infra:
            _check(not failures and len(matched) == 1, "non-infrastructure receipt requires one known independent outcome")
            verified = matched[0]
            outcome = verified.get("verification_status")
            _check(verified.get("model_digest") == model["digest"] and
                   (verified.get("resolved_image") == task["public_base_image_id"] or
                    (outcome == "patch_invalid" and verified.get("resolved_image") is None)),
                   "independent verifier model/image identity differs")
            _check(verified.get("submitted") is submitted, "independent verifier submission differs")
            _check(outcome in {"passed", "failed", "patch_invalid"},
                   "independent public check outcome must be known")
            checks = verified.get("public_checks")
            _check(isinstance(checks, list), "independent verifier must preserve its checks")
            if outcome in {"passed", "failed"}:
                actual_external = _check_rows(checks, task["public_checks_sha256"])
                _check(actual_external is (outcome == "passed"), "independent outcome contradicts its checks")
            _check(external is (verified["verification_status"] == "passed"), "external check receipt differs from independent outcome")
        else:
            _check(external is None, "infrastructure failure cannot claim known independent checks")
    observed = value.get("knowledge_observed_reuse", False)
    _boolean(observed)
    _check(not observed or arm["knowledge"] == "verified_source", "observed knowledge reuse requires a transfer arm")
    read_evidence = {}
    if observed:
        _check(started and evidence is not None, "positive source read requires a started model trial and its evidence")
        read_evidence = _observed_read(root, refs["evidence"], evidence, plan, plan_sha256, source_attempts)
    return {"instance_id": key[0], "seed": key[1], "arm": key[2], "role": target["role"], "status": status,
            "submitted": submitted, "infrastructure_failed": infra, "attempt_started": started, "attempted_count": attempted,
            "all_checks_passed": None if infra else _checks(internal, external),
            "delivery_success": delivery, "usage_complete": measured,
            "actual_input_tokens": input_tokens if measured else None,
            "actual_output_tokens": output_tokens if measured else None, "actual_model_calls": calls,
            "actual_seconds": seconds, "observed_provider_requests": requests,
            "knowledge_observed_read": observed, "knowledge_observed_reuse": observed,
            "knowledge_read_evidence": read_evidence, "artifacts": refs}


def _missing(key, target, *, reason="missing_trial"):
    return {"instance_id": key[0], "seed": key[1], "arm": key[2], "role": target["role"], "status": reason,
            "submitted": None, "infrastructure_failed": None, "attempt_started": None, "attempted_count": None,
            "all_checks_passed": None, "delivery_success": None, "usage_complete": False,
            "actual_input_tokens": None, "actual_output_tokens": None, "actual_model_calls": None,
            "actual_seconds": None, "observed_provider_requests": None,
            "knowledge_observed_read": None, "knowledge_observed_reuse": None,
            "knowledge_read_evidence": {}, "artifacts": {}}


def _total(rows, field):
    return sum(row[field] for row in rows) if rows and all(row[field] is not None for row in rows) else None


def _arm_stats(rows):
    denominator = len(rows)
    success = sum(row["delivery_success"] is True for row in rows)
    unknown = sum(row["delivery_success"] is None for row in rows)
    passed = sum(row["all_checks_passed"] is True for row in rows)
    checks_unknown = sum(row["all_checks_passed"] is None for row in rows)
    submitted = sum(row["submitted"] is True for row in rows)
    submission_unknown = sum(row["submitted"] is None for row in rows)
    totals = {field: _total(rows, field) for field in
              ("actual_input_tokens", "actual_output_tokens", "actual_model_calls", "actual_seconds")}
    tokens = (totals["actual_input_tokens"] + totals["actual_output_tokens"]
              if totals["actual_input_tokens"] is not None and totals["actual_output_tokens"] is not None else None)
    return {"expected_attempts": denominator, "observed_attempts": sum(not row["status"].startswith("missing_") for row in rows),
            "status_counts": {status: sum(row["status"] == status for row in rows) for status in sorted({row["status"] for row in rows})},
            "submitted_count": submitted, "submission_unknown_count": submission_unknown,
            "infrastructure_failure_count": sum(row["infrastructure_failed"] is True for row in rows),
            "actual_model_attempt_count": sum(row["attempted_count"] == 1 for row in rows),
            "actual_attempt_unknown_count": sum(row["attempted_count"] is None for row in rows),
            "blocked_before_model_count": sum(row["attempt_started"] is False for row in rows),
            "submitted_rate": submitted / denominator if denominator and not submission_unknown else None,
            "all_check_success_count": passed, "all_check_unknown_count": checks_unknown,
            "all_check_success_rate": passed / denominator if denominator and not checks_unknown else None,
            "delivery_success_count": success, "delivery_failure_count": sum(row["delivery_success"] is False for row in rows),
            "delivery_unknown_count": unknown, "delivery_success_rate": success / denominator if denominator and not unknown else None,
            "confirmed_delivery_success_fraction": success / denominator if denominator else None,
            "token_usage_covered_attempts": sum(row["usage_complete"] for row in rows), **{"total_" + key: item for key, item in totals.items()},
            "actual_provider_tokens_per_delivery_success": tokens / success if success and not unknown and tokens is not None else None,
            "actual_seconds_per_delivery_success": totals["actual_seconds"] / success if success and not unknown and totals["actual_seconds"] is not None else None,
            "knowledge_observed_read_count": sum(row["knowledge_observed_read"] is True for row in rows),
            "knowledge_observed_reuse_count": sum(row["knowledge_observed_read"] is True for row in rows)}


def _paired(rows, reference, arm_map):
    indexed = {(row["instance_id"], row["seed"], row["arm"]): row for row in rows}
    # Contrasts depend only on the frozen interventions, never on their outcomes.
    contrasts = [(name, reference, name) for name in arm_map if name != reference]
    for left, right in itertools.product(sorted(arm_map), repeat=2):
        if (left != reference and arm_map[left]["profile"] == arm_map[right]["profile"]
                and arm_map[left]["knowledge"] == "none" and arm_map[right]["knowledge"] == "verified_source"):
            contrasts.append((left + "->" + right, left, right))
    pairs = []
    summaries = {}
    for identity, reference_name, challenger in contrasts:
        left_arm, right_arm = arm_map[reference_name], arm_map[challenger]
        changes = [field for field in ("profile", "knowledge") if left_arm[field] != right_arm[field]]
        intervention = ("context_and_knowledge" if len(changes) == 2 else "knowledge_only" if changes == ["knowledge"]
                        else "context_only" if changes == ["profile"] else "same_intervention")
        for key, right in sorted(indexed.items()):
            if key[2] != challenger or (key[0], key[1], reference_name) not in indexed:
                continue
            left = indexed[(key[0], key[1], reference_name)]
            a, b = left["delivery_success"], right["delivery_success"]
            discordance = ("unknown" if a is None or b is None else "both_success" if a and b else
                           "reference_only" if a else "challenger_only" if b else "both_failure")
            successful = a is True and b is True
            deltas = {"delta_" + field: right[field] - left[field] if successful and left[field] is not None and right[field] is not None else None
                      for field in ("actual_input_tokens", "actual_output_tokens", "actual_model_calls", "actual_seconds")}
            pairs.append({"comparison_id": identity, "intervention": intervention, "instance_id": key[0],
                          "seed": key[1], "role": right["role"], "reference_arm": reference_name,
                          "challenger_arm": challenger, "reference_delivery_success": a, "challenger_delivery_success": b,
                          "success_discordance": discordance, "successful_pair_scope": successful, **deltas})
        selected = [row for row in pairs if row["comparison_id"] == identity]
        successful = [row for row in selected if row["successful_pair_scope"]]
        summaries[identity] = {"reference_arm": reference_name, "challenger_arm": challenger,
            "intervention": intervention, "expected_pairs": len(selected), "successful_pairs": len(successful),
            "success_discordance": {name: sum(row["success_discordance"] == name for row in selected)
                                    for name in ("both_success", "both_failure", "reference_only", "challenger_only", "unknown")},
            "efficiency_scope": "both arms delivered successfully; delta = challenger minus reference",
            **{"mean_delta_" + field: _total(successful, "delta_" + field) / len(successful)
               if successful and _total(successful, "delta_" + field) is not None else None
               for field in ("actual_input_tokens", "actual_output_tokens", "actual_model_calls", "actual_seconds")}}
    return {"reference_arm": reference, "contrast_policy": "frozen reference comparisons plus all same-profile unseeded-to-verified-source contrasts",
            "comparisons": summaries, "pairs": pairs}


def _sources(plan_keys, observations, root, plan):
    supplied = {}
    _check(isinstance(observations, list) and len(observations) <= MAX_ROWS, "observed sources must be bounded")
    for row in observations:
        _object(row, "source observation must be an object")
        _check(set(row) == {"instance_id", "seed", "receipt"}, "source observation fields differ from the contract")
        key = (_label(row["instance_id"]), _integer(row["seed"]))
        _check(key in plan_keys and key not in supplied, "source observation must match a unique frozen source attempt")
        supplied[key] = row
    records = []
    for key in plan_keys:
        record = {"instance_id": key[0], "seed": key[1], "status": "missing_trial", "submitted": None,
                  "delivery_success": None, "usage_complete": False, "actual_input_tokens": None,
                  "actual_output_tokens": None, "actual_model_calls": None, "actual_seconds": None,
                  "observed_provider_requests": None}
        try:
            if key in supplied:
                value, ref = _reference(root, supplied[key]["receipt"])
                _check(value.get("role") == "source" and isinstance(value.get("profile"), str) and
                       value["profile"] in {"baseline", "recent", "retrieval"}
                       and value.get("knowledge") == "none", "source receipt must declare a separate unseeded source role")
                target = {**_object(value.get("task"), "source must record its actual frozen task"),
                          "instance_id": key[0], "role": "source"}
                source_arm = {"id": _label(value.get("arm")), "profile": value["profile"], "knowledge": "none"}
                actual = _receipt(value, (key[0], key[1], source_arm["id"]), target, source_arm, plan, root)
                record.update(status=actual["status"], submitted=actual["submitted"], delivery_success=actual["delivery_success"],
                              attempt_started=actual["attempt_started"], attempted_count=actual["attempted_count"],
                              **{field: actual[field] for field in ("usage_complete", "actual_input_tokens", "actual_output_tokens",
                                  "actual_model_calls", "actual_seconds", "observed_provider_requests")},
                              artifacts=actual["artifacts"], receipt=ref)
        except MissingArtifact:
            record["status"] = "missing_artifact"
        records.append(record)
    return {"scope": "source seed attempts only; excluded from every target denominator", "expected_attempts": len(plan_keys),
            "submitted_count": sum(row["submitted"] is True for row in records),
            "actual_model_attempt_count": sum(row.get("attempted_count") == 1 for row in records),
            "delivery_success_count": sum(row["delivery_success"] is True for row in records), "attempts": records}


def summarize(plan_path, observations_path, output=None):
    plan, plan_hash = _read(plan_path)
    arms, targets, expected, reference, sources = _plan(plan)
    observations, observations_hash = _read(observations_path)
    _check(observations.get("schema") == 1 and observations.get("kind") == "closure_cohort_observations" and
           observations.get("plan_sha256") == plan_hash, "observations must bind the exact frozen plan")
    supplied = observations.get("runs")
    _check(isinstance(supplied, list) and len(supplied) <= MAX_ROWS, "observations must be a bounded trial list")
    mapped = {}
    for row in supplied:
        _check(isinstance(row, dict) and set(row) == {"instance_id", "seed", "arm", "receipt"}, "observation fields differ from the contract")
        key = _key(row)
        _check(key in expected and key not in mapped, "trial must match a unique predeclared target/seed/arm")
        mapped[key] = row
    root, rows = Path(observations_path).resolve().parent, []
    source_summary = _sources(sources, observations.get("source_attempts", []), root, plan)
    for key, target in sorted(expected.items()):
        row = _missing(key, target)
        try:
            if key in mapped:
                value, ref = _reference(root, mapped[key]["receipt"])
                row = _receipt(value, key, target, arms[key[2]], plan, root,
                               plan_sha256=plan_hash, source_attempts=source_summary["attempts"])
                row["receipt"] = ref
        except MissingArtifact:
            row = _missing(key, target, reason="missing_artifact")
        rows.append(row)
    result = {"schema": 1, "kind": "fixed_closure_cohort_summary", "fixture": False, "suite_id": plan["suite_id"],
        "plan_sha256": plan_hash, "observations_sha256": observations_hash, "model": plan["model"], "common": plan["common"],
        "expected_target_attempts": len(expected), "complete": all(row["delivery_success"] is not None for row in rows),
        "by_arm": {name: {"profile": arm["profile"], "knowledge": arm["knowledge"], **_arm_stats([row for row in rows if row["arm"] == name])}
                   for name, arm in arms.items()},
        "by_role": {role: {name: _arm_stats([row for row in rows if row["role"] == role and row["arm"] == name]) for name in arms}
                    for role in sorted({target["role"] for target in targets.values()})},
        "paired": _paired(rows, reference, arms), "source_attempts": source_summary,
        "target_attempts": rows, "official_resolved": None, "global_improvement_claim": False,
        "knowledge_metric_scope": "Observed successful nonempty tool read, verified from the full audited command and hash-bound source body; no claim that the model used it or benefited. knowledge_observed_reuse fields are deprecated aliases for observed_read.",
        "limitations": ["The frozen cohort is small and predeclared; no significance test or general improvement claim.",
                        "Near-transfer and holdout roles are reported separately; source seed attempts are not target successes.",
                        "Actual provider usage only; any incomplete arm coverage makes its total token metric null.",
                        "Successful-pair efficiency and cost per delivery success across all attempts have different scopes.",
                        "Receipt hashes bind runner artifacts; this summary does not replace independent verification or prove freeze timing."]}
    if output is not None:
        path = Path(output)
        refs = [row["receipt"] for row in rows if "receipt" in row]
        refs.extend(ref for row in rows for ref in row["artifacts"].values() if ref is not None)
        refs.extend(ref for row in rows for ref in row["knowledge_read_evidence"].values())
        refs.extend(row["receipt"] for row in result["source_attempts"]["attempts"] if "receipt" in row)
        refs.extend(ref for row in result["source_attempts"]["attempts"] for ref in row.get("artifacts", {}).values() if ref is not None)
        protected = {Path(plan_path).resolve(), Path(observations_path).resolve(),
                     *(root.joinpath(*ref["path"].split("/")).resolve() for ref in refs)}
        _check(path.resolve() not in protected, "output may not overwrite cohort inputs or evidence")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = summarize(args.plan, args.observations, args.output)
    except (SummaryError, OSError, TypeError, KeyError):
        print(json.dumps({"error": "invalid_or_unavailable_cohort_evidence"}))
        return 2
    print(json.dumps({"expected_target_attempts": result["expected_target_attempts"], "complete": result["complete"],
                      "global_improvement_claim": False}))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
