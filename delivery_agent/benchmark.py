"""Answer-free SWE-bench ingestion and an independently graded submission bridge.

No model, dataset SDK, Docker client or harness is imported here. Dataset answers
stay in the caller's source file, outside the agent's public task manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HARNESS_COMMIT = "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e"
HARNESS_SOURCE = (
    "https://github.com/SWE-bench/SWE-bench/blob/"
    f"{HARNESS_COMMIT}/swebench/harness/run_evaluation.py"
)
PUBLIC_FIELDS = frozenset({"instance_id", "repo", "base_commit", "problem_statement", "version"})
ANSWER_FIELDS = frozenset({"patch", "test_patch", "hints_text", "FAIL_TO_PASS", "PASS_TO_PASS"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_COMMIT = re.compile(r"[a-fA-F0-9]{40}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")


class BenchmarkError(ValueError):
    """A dataset, prediction or report failed validation."""


def _text(value: Any, name: str, *, preserve_space: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BenchmarkError(f"{name} must be a nonempty string")
    if not preserve_space and value != value.strip():
        raise BenchmarkError(f"{name} must not contain surrounding whitespace")
    return value


def _identifier(value: Any) -> str:
    value = _text(value, "instance_id")
    if not _ID.fullmatch(value):
        raise BenchmarkError("instance_id contains unsafe characters")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON in {label}: line {exc.lineno}") from exc


def _read_json(path: Path) -> Any:
    try:
        return _parse_json(path.read_text(encoding="utf-8-sig"), path.name)
    except UnicodeError as exc:
        raise BenchmarkError(f"{path.name} must be UTF-8") from exc


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _public_task(record: Any, *, strict: bool = False) -> dict[str, str]:
    if not isinstance(record, dict):
        raise BenchmarkError("each task must be a JSON object")
    if strict and set(record) - PUBLIC_FIELDS:
        raise BenchmarkError("public manifest contains forbidden or unknown fields")
    instance_id = _identifier(record.get("instance_id"))
    repo = _text(record.get("repo"), "repo")
    if not _REPO.fullmatch(repo):
        raise BenchmarkError(f"{instance_id}: repo must be owner/name")
    commit = _text(record.get("base_commit"), "base_commit")
    if not _COMMIT.fullmatch(commit):
        raise BenchmarkError(f"{instance_id}: base_commit must be a full 40-character SHA")
    result = {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": commit.lower(),
        "problem_statement": _text(record.get("problem_statement"), "problem_statement", preserve_space=True),
    }
    if "version" in record:
        result["version"] = _text(record["version"], "version")
    return result


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = []
        for key, row in value.items():
            _identifier(key)
            if not isinstance(row, dict):
                raise BenchmarkError("keyed datasets must map IDs to task objects")
            if "instance_id" in row and row["instance_id"] != key:
                raise BenchmarkError(f"task map key disagrees with instance_id: {key}")
            rows.append({**row, "instance_id": key})
    else:
        raise BenchmarkError("dataset must be a list or an instance-ID map")
    if not rows:
        raise BenchmarkError("dataset contains no tasks")
    seen: set[str] = set()
    for row in rows:
        task = _public_task(row)
        if task["instance_id"] in seen:
            raise BenchmarkError(f"duplicate task: {task['instance_id']}")
        seen.add(task["instance_id"])
    return rows


def read_dataset(source: str | Path) -> list[dict[str, Any]]:
    """Read JSON arrays, ID maps or JSONL; callers must keep raw rows private."""
    source = Path(source)
    if source.suffix.lower() in {".jsonl", ".ndjson"}:
        try:
            lines = source.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeError as exc:
            raise BenchmarkError("dataset must be UTF-8") from exc
        return _records([_parse_json(line, f"{source.name}:{number}")
                         for number, line in enumerate(lines, 1) if line.strip()])
    return _records(_read_json(source))


def freeze_dataset(
    source: str | Path,
    output_dir: str | Path,
    dataset_name: str,
    dataset_revision: str,
    split: str = "test",
    instance_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Freeze allowlisted tasks and external pin metadata, never copying answers.

    Revision is a caller-declared immutable dataset SHA; the exact supplied bytes
    are independently hashed. This function does not verify a remote revision.
    """
    source, output_dir = Path(source), Path(output_dir)
    dataset_name = _text(dataset_name, "dataset_name")
    dataset_revision = _text(dataset_revision, "dataset_revision")
    if not _COMMIT.fullmatch(dataset_revision):
        raise BenchmarkError("dataset_revision must be an immutable 40-character SHA")
    split = _text(split, "split")
    rows = read_dataset(source)
    indexed = {row["instance_id"]: row for row in rows}
    if instance_ids is None:
        selected = list(indexed)
    else:
        selected = [_identifier(value) for value in instance_ids]
        if not selected or len(set(selected)) != len(selected):
            raise BenchmarkError("instance selection must be nonempty and unique")
        missing = set(selected) - set(indexed)
        if missing:
            raise BenchmarkError(f"requested tasks absent from dataset: {', '.join(sorted(missing))}")
    tasks = [_public_task(indexed[key]) for key in selected]
    manifest = {"schema_version": 1, "tasks": tasks}
    manifest_bytes = _json_bytes(manifest)
    metadata = {
        "schema_version": 1,
        "dataset_name": dataset_name,
        "dataset_revision": dataset_revision.lower(),
        "revision_verified_remotely": False,
        "split": split,
        "snapshot_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_record_count": len(rows),
        "instance_ids": selected,
        "harness_commit": HARNESS_COMMIT,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path, metadata_path = output_dir / "manifest.json", output_dir / "snapshot.json"
    if manifest_path.exists() or metadata_path.exists():
        raise BenchmarkError("frozen output already exists; choose a fresh snapshot directory")
    if source.resolve() in {manifest_path.resolve(), metadata_path.resolve()}:
        raise BenchmarkError("snapshot output would overwrite source dataset")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    _write_json(metadata_path, metadata)
    return {"manifest_path": str(manifest_path), "metadata_path": str(metadata_path), **metadata}


def load_manifest(path: str | Path) -> list[dict[str, str]]:
    """Reject answer-bearing or modified frozen manifests before execution."""
    path = Path(path)
    manifest = _read_json(path)
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "tasks"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1):
        raise BenchmarkError("invalid public manifest schema")
    if not isinstance(manifest["tasks"], list):
        raise BenchmarkError("public manifest tasks must be a list")
    tasks = _records(manifest["tasks"])
    public = [_public_task(task, strict=True) for task in tasks]
    metadata_path = path.with_name("snapshot.json")
    if not metadata_path.is_file():
        raise BenchmarkError("frozen manifest requires sibling snapshot.json metadata")
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict):
        raise BenchmarkError("invalid snapshot metadata")
    if metadata.get("manifest_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
        raise BenchmarkError("manifest hash disagrees with frozen snapshot")
    if metadata.get("instance_ids") != [task["instance_id"] for task in public]:
        raise BenchmarkError("manifest selection disagrees with frozen snapshot")
    if not _COMMIT.fullmatch(str(metadata.get("dataset_revision", ""))):
        raise BenchmarkError("snapshot is missing an immutable dataset revision")
    return public


def _model_name(value: Any) -> str:
    value = _text(value, "model_name_or_path")
    if not _MODEL.fullmatch(value) or value in {".", ".."}:
        raise BenchmarkError("model_name_or_path contains unsafe characters")
    return value


def _prediction(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BenchmarkError("prediction must be an object")
    patch = value.get("model_patch")
    if not isinstance(patch, str) or "\x00" in patch:
        raise BenchmarkError("model_patch must be a string, including an honest empty string")
    return {"instance_id": _identifier(value.get("instance_id")),
            "model_name_or_path": _model_name(value.get("model_name_or_path")), "model_patch": patch}


def export_predictions(
    result_paths: Iterable[str | Path], output_path: str | Path, model_name: str | None = None,
) -> dict[str, Any]:
    """Export official JSONL from real submitted result files, not agent claims.

    Failed/fixture runs are recorded as exclusions. Local patch validity and
    public checks do not imply official resolution and never filter submissions.
    """
    override = _model_name(model_name) if model_name is not None else None
    result_paths = [Path(path) for path in result_paths]
    predictions, excluded, submitted, attempts = [], [], [], []
    seen: set[str] = set()
    seen_attempts: set[tuple[str, str]] = set()
    for item in result_paths:
        path = Path(item)
        result = _read_json(path)
        if not isinstance(result, dict):
            raise BenchmarkError(f"{path.name}: result must be an object")
        reason = None
        if result.get("fixture") is not False or result.get("provider") != "litellm":
            reason = "fixture_or_unverified_provider"
        else:
            instance_id = _identifier(result.get("instance_id"))
            model = override or _model_name(result.get("model_name_or_path", result.get("model")))
            attempt_key = (model, instance_id)
            if attempt_key in seen_attempts:
                raise BenchmarkError(f"duplicate real attempt: {model}/{instance_id}")
            seen_attempts.add(attempt_key)
            is_submitted = result.get("status") == "submitted" and result.get("exit_status") == "Submitted"
            attempts.append({"instance_id": instance_id, "model_name_or_path": model,
                             "submitted": is_submitted, "status": result.get("status"),
                             "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            if not is_submitted:
                reason = "not_submitted"
        if reason:
            excluded.append({"result_path": str(path), "instance_id": result.get("instance_id"), "reason": reason})
            continue
        prediction = _prediction({**result, "model_name_or_path": override or result.get("model_name_or_path", result.get("model"))})
        instance_id = prediction["instance_id"]
        if instance_id in seen:
            raise BenchmarkError(f"duplicate submitted task: {instance_id}")
        seen.add(instance_id)
        patch_valid = result.get("patch_valid")
        if patch_valid is not None and type(patch_valid) is not bool:
            raise BenchmarkError("patch_valid must be a boolean or null")
        predictions.append(prediction)
        submitted.append({"instance_id": instance_id, "patch_valid": patch_valid,
                          "official_resolved": None, "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    output_path = Path(output_path)
    sources = {path.resolve() for path in result_paths}
    attempts_path = output_path.with_name(output_path.name + ".attempts.json")
    if output_path.resolve() in sources or attempts_path.resolve() in sources:
        raise BenchmarkError("prediction output would overwrite a result file")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(pred, ensure_ascii=False) + "\n" for pred in predictions), encoding="utf-8")
    _write_json(attempts_path, {"schema_version": 1,
                              "predictions_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
                              "attempts": attempts})
    return {"predictions_path": str(output_path), "submitted_count": len(predictions),
            "attempts_path": str(attempts_path), "attempted_count": len(attempts),
            "agent_failure_count": sum(not item["submitted"] for item in attempts),
            "empty_patch_count": sum(pred["model_patch"] == "" for pred in predictions),
            "submitted": submitted, "excluded": excluded, "official_resolved": None}


def grade_command(
    predictions_path: str | Path, dataset_name: str, run_id: str, *, python: str = "python",
    split: str = "test", max_workers: int = 4, timeout: int = 1800,
    instance_ids: Iterable[str] | None = None,
) -> list[str]:
    """Return argv for the pinned official module; execution is the caller's job.

    For reproducibility, dataset_name should be the original private JSON snapshot
    and the grading environment must install HARNESS_COMMIT. No shell is used.
    """
    for value, label in ((python, "python"), (dataset_name, "dataset_name"), (split, "split")):
        _text(value, label)
    _identifier(run_id)
    for value, label in ((max_workers, "max_workers"), (timeout, "timeout")):
        if type(value) is not int or value <= 0:
            raise BenchmarkError(f"{label} must be a positive integer")
    argv = [python, "-m", "swebench.harness.run_evaluation", "--dataset_name", dataset_name,
            "--split", split, "--predictions_path", str(predictions_path), "--run_id", run_id,
            "--max_workers", str(max_workers), "--timeout", str(timeout)]
    if instance_ids is not None:
        ids = [_identifier(value) for value in instance_ids]
        if not ids or len(set(ids)) != len(ids):
            raise BenchmarkError("grading selection must be nonempty and unique")
        argv.extend(["--instance_ids", *ids])
    return argv


def _load_predictions(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = [_parse_json(line, path.name) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        rows = _read_json(path)
    if isinstance(rows, dict):
        values = []
        for key, row in rows.items():
            if not isinstance(row, dict) or row.get("instance_id", key) != key:
                raise BenchmarkError("invalid keyed prediction")
            values.append({**row, "instance_id": key})
        rows = values
    if not isinstance(rows, list):
        raise BenchmarkError("predictions must be a JSON list, map or JSONL")
    predictions = [_prediction(row) for row in rows]
    if len({row["instance_id"] for row in predictions}) != len(predictions):
        raise BenchmarkError("duplicate prediction instance IDs")
    return predictions


def aggregate_official_reports(
    predictions_path: str | Path, reports_dir: str | Path, output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read canonical official reports for one run, retaining missing outcomes.

    reports_dir is logs/run_evaluation/<run_id>, not a parent containing other
    runs. Each report must have a matching sibling patch.diff; stale or malformed
    reports count as invalid. No local result's self-reported resolution is read.
    """
    predictions_path, reports_dir = Path(predictions_path), Path(reports_dir)
    predictions = _load_predictions(predictions_path)
    attempts_path = predictions_path.with_name(predictions_path.name + ".attempts.json")
    attempts = None
    if attempts_path.is_file():
        sidecar = _read_json(attempts_path)
        if (not isinstance(sidecar, dict) or sidecar.get("schema_version") != 1
                or sidecar.get("predictions_sha256") != hashlib.sha256(predictions_path.read_bytes()).hexdigest()
                or not isinstance(sidecar.get("attempts"), list)):
            raise BenchmarkError("attempt sidecar does not match predictions")
        attempts = sidecar["attempts"]
        attempt_keys = set()
        submitted_keys = set()
        for attempt in attempts:
            if not isinstance(attempt, dict) or type(attempt.get("submitted")) is not bool:
                raise BenchmarkError("invalid attempt sidecar record")
            key = (_model_name(attempt.get("model_name_or_path")), _identifier(attempt.get("instance_id")))
            if key in attempt_keys:
                raise BenchmarkError("duplicate attempt in sidecar")
            attempt_keys.add(key)
            if attempt["submitted"]:
                submitted_keys.add(key)
        expected = {(prediction["model_name_or_path"], prediction["instance_id"]) for prediction in predictions}
        if submitted_keys != expected:
            raise BenchmarkError("attempt sidecar submitted selection disagrees with predictions")
    outcomes, groups = [], {key: [] for key in ("resolved", "unresolved", "missing", "invalid")}
    for prediction in predictions:
        instance_id = prediction["instance_id"]
        directory = reports_dir / prediction["model_name_or_path"].replace("/", "__") / instance_id
        report_path = directory / "report.json"
        outcome: dict[str, Any] = {"instance_id": instance_id, "submitted": True,
                                   "patch_valid": None, "official_resolved": None,
                                   "report_path": str(report_path)}
        state = "missing"
        if report_path.is_file():
            try:
                report = _read_json(report_path)
                item = report.get(instance_id) if isinstance(report, dict) else None
                if not isinstance(item, dict) or type(item.get("resolved")) is not bool:
                    raise BenchmarkError("official resolved field must be a boolean")
                valid = item.get("patch_successfully_applied")
                if type(valid) is not bool:
                    raise BenchmarkError("official patch_successfully_applied must be a boolean")
                patch_path = directory / "patch.diff"
                expected_patch = prediction["model_patch"].encode("utf-8")
                if (not patch_path.is_file()
                        or patch_path.read_bytes().replace(b"\r\n", b"\n") != expected_patch.replace(b"\r\n", b"\n")):
                    raise BenchmarkError("official report lacks matching submitted patch evidence")
                if item["resolved"] and not valid:
                    raise BenchmarkError("resolved official report contradicts patch applicability")
                outcome["patch_valid"], outcome["official_resolved"] = valid, item["resolved"]
                outcome["submitted_patch_sha256"] = hashlib.sha256(expected_patch).hexdigest()
                outcome["logged_patch_sha256"] = hashlib.sha256(patch_path.read_bytes()).hexdigest()
                state = "resolved" if item["resolved"] else "unresolved"
            except (BenchmarkError, OSError, UnicodeError) as exc:
                state, outcome["error"] = "invalid", str(exc)
        outcome["evaluation_status"] = state
        groups[state].append(instance_id)
        outcomes.append(outcome)
    evaluated = len(groups["resolved"]) + len(groups["unresolved"])
    evaluation_complete = evaluated == len(predictions)
    complete = evaluation_complete and bool(predictions)
    attempted_count = len(attempts) if attempts is not None else None
    failures = [{"instance_id": attempt["instance_id"], "model_name_or_path": attempt["model_name_or_path"],
                 "status": attempt.get("status")}
                for attempt in (attempts or []) if not attempt["submitted"]]
    summary = {"schema_version": 1, "harness_commit": HARNESS_COMMIT,
               "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
               "requested_count": len(predictions), "submitted_count": len(predictions),
               "attempted_count": attempted_count, "attempted_denominator": attempted_count,
               "evaluation_complete_for_submitted": evaluation_complete,
               "unresolved_agent_failures": failures, "agent_failure_count": len(failures) if attempts is not None else None,
               "evaluated_count": evaluated, "complete": complete,
               "resolved_count": len(groups["resolved"]), "unresolved_count": len(groups["unresolved"]),
               "missing_count": len(groups["missing"]), "invalid_count": len(groups["invalid"]),
               "submitted_only_resolved_rate": len(groups["resolved"]) / len(predictions) if complete else None,
               "all_attempt_resolved_rate": len(groups["resolved"]) / attempted_count
               if evaluation_complete and attempted_count else None,
               "patch_valid_count": sum(item["patch_valid"] is True for item in outcomes),
               "patch_invalid_count": sum(item["patch_valid"] is False for item in outcomes),
               "patch_valid_unknown_count": sum(item["patch_valid"] is None for item in outcomes),
               **{f"{key}_ids": ids for key, ids in groups.items()}, "outcomes": outcomes}
    if output_path is not None:
        if Path(output_path).resolve() == predictions_path.resolve():
            raise BenchmarkError("report summary would overwrite predictions")
        _write_json(Path(output_path), summary)
    return summary
