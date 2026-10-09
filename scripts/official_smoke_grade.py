"""Grade real predictions with pinned official SWE-bench v5, without exporting gold.

Run on a dedicated Linux Docker worker. Missing predictions are waiting input,
not a successful empty benchmark. Public artifacts are an explicit allowlist;
private dataset rows, eval scripts and raw harness output never leave the job.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.benchmark import HARNESS_COMMIT, aggregate_official_reports, grade_command, _load_predictions
from delivery_agent.repository import atomic_json

DATASET = "SWE-bench/SWE-bench_Verified"
REVISION = "03e151cf5560b1af6a4363c6a9d766deaaea6b56"
PARQUET_SHA = "bb5b123d29ce70107cc0951cf444894241c570a11d76aec452332c65b01e06d8"
MAX_PARQUET_BYTES = 128 * 1024 * 1024
PUBLIC_REPORT_FIELDS = {"patch_is_None", "patch_exists", "patch_successfully_applied", "resolved", "infra_failure"}


def read_inputs(source_path, predictions_path):
    source = json.loads(Path(source_path).read_text(encoding="utf-8"))
    expected_url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data/test-00000-of-00001.parquet"
    if (source.get("dataset") != DATASET or source.get("revision") != REVISION or
            source.get("parquet_sha256") != PARQUET_SHA or source.get("parquet_url") != expected_url):
        raise ValueError("official smoke source does not match the independently fixed dataset pin")
    selected = source.get("instance_ids")
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 3 or len(set(selected)) != len(selected)
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", item) for item in selected)):
        raise ValueError("invalid bounded source selection")
    predictions_path = Path(predictions_path)
    attempts_path = predictions_path.with_name(predictions_path.name + ".attempts.json")
    if not predictions_path.exists() or not attempts_path.exists():
        return source, [], [], "waiting_for_predictions_and_attempts"
    predictions = _load_predictions(predictions_path)
    # Reuse the strict hash/uniqueness/submitted-selection contract, with no
    # reports present. This initial missing result is not published as a grade.
    aggregate_official_reports(predictions_path, predictions_path.parent / "__not_a_grading_run__")
    sidecar = json.loads(attempts_path.read_text(encoding="utf-8"))
    attempts = sidecar["attempts"]
    if (len(attempts) > 3 or len({row["model_name_or_path"] for row in [*predictions, *attempts]}) > 1
            or {row["instance_id"] for row in [*predictions, *attempts]} - set(selected)):
        raise ValueError("predictions/attempts contain mixed models or are outside the fixed official smoke selection")
    if not attempts:
        return source, predictions, attempts, "waiting_for_real_attempts"
    if not predictions:
        return source, predictions, attempts, "no_submissions"
    return source, predictions, attempts, "ready"


def download_verified_parquet(source, destination):
    """Stream a bounded HTTPS download and verify exact immutable bytes."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("refuse to overwrite an existing dataset download")
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(source["parquet_url"], timeout=120) as response, destination.open("xb") as target:
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > MAX_PARQUET_BYTES:
                    raise ValueError("official Parquet exceeds the bounded download limit")
                digest.update(block)
                target.write(block)
        if digest.hexdigest() != PARQUET_SHA:
            raise ValueError("official dataset Parquet SHA256 mismatch")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return total


def select_grading_rows(source, parquet, instance_ids):
    import pyarrow.parquet as pq
    rows = pq.read_table(parquet).to_pylist()
    return validate_grading_rows(source, rows, instance_ids)


def validate_grading_rows(source, rows, instance_ids):
    if len(rows) != source["record_count"] or len(rows) != 500:
        raise ValueError("official Parquet record count differs from the fixed snapshot")
    indexed = {row["instance_id"]: row for row in rows}
    if len(indexed) != len(rows) or set(instance_ids) - set(indexed):
        raise ValueError("official dataset contains duplicate or missing task identities")
    metadata = {item["instance_id"]: item for item in source["grading_metadata"]}
    required = {"image", "eval_script", "log_parser", "eval_type", "FAIL_TO_PASS", "PASS_TO_PASS", "repo", "version"}
    selected = []
    for identity in instance_ids:
        row = indexed[identity]
        if not required <= row.keys() or any(row[key] is None for key in required):
            raise ValueError("selected official record is missing v5 grading fields")
        reference = metadata[identity]
        if (row["image"] != reference["image"] or not row["image"].startswith("swebench/sweb.eval.x86_64.") or
                hashlib.sha256(row["eval_script"].encode()).hexdigest() != reference["eval_script_sha256"]):
            raise ValueError("official image/eval-script pin differs from the frozen selection")
        selected.append(row)
    return selected


def verify_installed_harness():
    distribution = importlib.metadata.distribution("swebench")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if (direct.get("vcs_info", {}).get("commit_id") != HARNESS_COMMIT or
            direct.get("url", "").rstrip("/") not in {"https://github.com/SWE-bench/SWE-bench.git", "https://github.com/SWE-bench/SWE-bench"}):
        raise ValueError("install the official harness from the exact pinned Git commit")
    from swebench.harness import run_evaluation
    from swebench.harness.constants import RUN_EVALUATION_LOG_DIR
    if RUN_EVALUATION_LOG_DIR != Path("logs/evaluation"):
        raise ValueError("installed harness log layout differs from the inspected v5 source")
    return {"commit": HARNESS_COMMIT, "distribution_version": distribution.version,
            "run_evaluation_sha256": hashlib.sha256(Path(run_evaluation.__file__).read_bytes()).hexdigest(),
            "python_version": sys.version.split()[0], "pyarrow_version": importlib.metadata.version("pyarrow"),
            "run_log_root": str(RUN_EVALUATION_LOG_DIR)}


@contextmanager
def working_directory(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def export_public_reports(private_run, public_run, predictions):
    """Copy only official booleans and matching submitted patch evidence.

    tests_status, eval.sh, test_output and raw logs can contain hidden evaluation
    details. Original report hashes record provenance for the selected fields.
    """
    private_run, public_run = Path(private_run), Path(public_run)
    public_run.mkdir(parents=True, exist_ok=True)
    provenance = []
    for prediction in predictions:
        relative = Path(prediction["model_name_or_path"].replace("/", "__")) / prediction["instance_id"]
        original = private_run / relative / "report.json"
        patch = private_run / relative / "patch.diff"
        if not original.is_file():
            continue
        value = json.loads(original.read_text())
        item = value.get(prediction["instance_id"])
        if not isinstance(item, dict):
            continue  # Aggregator records missing/invalid rather than inventing.
        selected = {key: value for key, value in item.items() if key in PUBLIC_REPORT_FIELDS and type(value) is bool}
        target = public_run / relative
        target.mkdir(parents=True, exist_ok=True)
        atomic_json(target / "report.json", {prediction["instance_id"]: selected})
        if patch.is_file():
            shutil.copyfile(patch, target / "patch.diff")
        provenance.append({"instance_id": prediction["instance_id"], "original_report_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                           "exported_fields": sorted(selected), "gold_and_test_status_exported": False})
    final = private_run / "results.json"
    if final.is_file():
        # The fixed official reporting.py emits aggregate counts and IDs only.
        value = json.loads(final.read_text())
        allowed = {"total_instances", "submitted_instances", "completed_instances", "resolved_instances", "unresolved_instances",
                   "infra_failure_instances", "ambiguous_failure_instances", "empty_patch_instances", "error_instances", "schema_version",
                   "completed_ids", "incomplete_ids", "empty_patch_ids", "submitted_ids", "resolved_ids", "unresolved_ids", "infra_failure_ids",
                   "ambiguous_failure_ids", "error_ids", "unstopped_instances", "unstopped_containers", "unremoved_images"}
        atomic_json(public_run / "results.json", {key: value[key] for key in allowed if key in value})
    atomic_json(public_run / "report-provenance.json", provenance)


def run_bounded(argv, output_log, *, timeout, private_root, min_free_bytes=2 * 1024 ** 3):
    """Bound grader wall time, private output volume and disk pressure."""
    environment = dict(os.environ, SWEBENCH_DOCKER_TIMEOUT="600", SWEBENCH_DOCKER_POOL_SIZE="4")
    started = time.monotonic()
    with Path(output_log).open("wb") as stream:
        process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        reason = None
        while process.poll() is None:
            if time.monotonic() - started >= timeout:
                reason = "wall_time_limit"
            elif stream.tell() > 32 * 1024 * 1024:
                reason = "private_log_limit"
            elif shutil.disk_usage(private_root).free < min_free_bytes:
                reason = "disk_pressure"
            if reason:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=15)
                break
            time.sleep(1)
    return {"exit_code": process.returncode, "stop_reason": reason, "elapsed_seconds": time.monotonic() - started}


def cleanup_task_container(instance_id, run_id):
    expected = f"sweb.eval.{instance_id.lower()}.{run_id}"
    result = subprocess.run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("could not confirm grading container cleanup")
    for line in result.stdout.decode().splitlines():
        identifier, name = line.split("\t", 1)
        if name == expected or re.fullmatch(re.escape(expected) + r"\.[0-9]+", name):
            if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                raise RuntimeError("Docker cleanup returned an invalid container identity")
            subprocess.run(["docker", "rm", "-f", identifier], check=True, capture_output=True, timeout=30)


def grade(source_path, predictions_path, output_dir, run_id, test_timeout=900, operation_timeout=1500):
    source, predictions, attempts, status = read_inputs(source_path, predictions_path)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "summary.json").exists():
        raise ValueError("use a fresh grading output directory")
    base = {"schema_version": 1, "dataset": DATASET, "revision": REVISION, "parquet_sha256": PARQUET_SHA,
            "harness_commit": HARNESS_COMMIT, "run_id": run_id, "model": attempts[0]["model_name_or_path"] if attempts else None,
            "attempted_count": len(attempts), "attempted_denominator": len(attempts), "submitted_count": len(predictions),
            "agent_failure_count": sum(not item["submitted"] for item in attempts),
            "unattempted_ids": sorted(set(source["instance_ids"]) - {row["instance_id"] for row in attempts}),
            "official_resolved_rate": None, "all_attempt_resolved_rate": None, "complete": False}
    base["metric_scope"] = "fixed selected-instance smoke; not the full 500-instance Verified benchmark"
    if status != "ready":
        atomic_json(output / "summary.json", {**base, "grading_status": status})
        return {**base, "grading_status": status}
    if os.name != "posix" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", run_id):
        raise ValueError("official grading requires a dedicated Linux worker and a safe run ID")
    harness = verify_installed_harness()
    predictions_path = Path(predictions_path).resolve()
    attempt_ids = [row["instance_id"] for row in attempts]
    operations = []
    def event(kind, **fields):
        record = {"event": kind, "time": time.time(), **fields}
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
    event("validated_inputs", attempts=len(attempts), submissions=len(predictions), predictions_sha256=hashlib.sha256(predictions_path.read_bytes()).hexdigest())
    with tempfile.TemporaryDirectory(prefix="repo-delivery-official-") as temporary:
        private = Path(temporary)
        parquet = private / "official.parquet"
        size = download_verified_parquet(source, parquet)
        rows = select_grading_rows(source, parquet, attempt_ids)
        grading_source = private / "grading-source.json"
        atomic_json(grading_source, rows)
        event("dataset_verified", bytes=size, selected_count=len(rows), gold_exported=False)
        with working_directory(private):
            for prediction in predictions:
                instance_id = prediction["instance_id"]
                if not prediction["model_patch"]:
                    event("empty_patch", instance_id=instance_id)
                    continue
                row = next(row for row in rows if row["instance_id"] == instance_id)
                image = row["image"]
                existed = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0
                if shutil.disk_usage(private).free < 8 * 1024 ** 3:
                    operations.append({"instance_id": instance_id, "exit_code": None, "stop_reason": "insufficient_starting_disk"})
                    event("grading_skipped", instance_id=instance_id, reason="insufficient_starting_disk")
                    continue
                argv = grade_command(predictions_path, str(grading_source), run_id, python=sys.executable,
                                     max_workers=1, timeout=test_timeout, instance_ids=[instance_id])
                event("grading_started", instance_id=instance_id)
                try:
                    result = run_bounded(argv, private / (instance_id + ".private.log"), timeout=operation_timeout, private_root=private)
                    operations.append({"instance_id": instance_id, **result})
                    event("grading_finished", instance_id=instance_id, **result)
                except Exception as exc:
                    operations.append({"instance_id": instance_id, "exit_code": None, "stop_reason": "orchestrator_error", "exception_type": type(exc).__name__})
                    event("grading_error", instance_id=instance_id, exception_type=type(exc).__name__)
                    raise
                finally:
                    cleanup_task_container(instance_id, run_id)
                    inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", image], capture_output=True, timeout=30)
                    if inspected.returncode == 0:
                        image_info = json.loads(inspected.stdout)
                        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_info.get("Id", "")):
                            operations[-1]["image_id"] = image_info["Id"]
                            operations[-1]["repo_digests"] = image_info.get("RepoDigests", [])
                    if not existed:
                        # Remove only this job's newly pulled task image. Shared
                        # layers still referenced by other images remain intact.
                        removed = subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=60)
                        event("new_image_cleanup", instance_id=instance_id, exit_code=removed.returncode)
            from swebench.harness.reporting import make_run_report
            make_run_report({row["instance_id"]: row for row in predictions}, rows, run_id, client=None)
            export_public_reports(private / harness["run_log_root"] / run_id, output / "reports", predictions)
        summary = aggregate_official_reports(predictions_path, output / "reports")
        official = json.loads((output / "reports" / "results.json").read_text())
        known_empty = sorted(set(official.get("empty_patch_ids", [])) & {row["instance_id"] for row in predictions if not row["model_patch"]})
        unknown_nonempty = set(summary["missing_ids"]) - set(known_empty)
        grade_complete = not unknown_nonempty and not summary["invalid_ids"]
        nonempty = sum(bool(row["model_patch"]) for row in predictions)
        resolved_fraction = summary["resolved_count"] / len(attempts) if grade_complete and attempts else None
        submitted_fraction = summary["resolved_count"] / len(predictions) if grade_complete and predictions else None
        summary.update(base)
        summary.update(grading_status=("empty_submissions" if nonempty == 0 else "completed" if grade_complete else "incomplete"), complete=grade_complete and nonempty > 0,
                       evaluation_complete_for_submitted=grade_complete, official_empty_patch_ids=known_empty,
                       submitted_only_resolved_rate=submitted_fraction,
                       all_attempt_resolved_rate=resolved_fraction, official_resolved_rate=resolved_fraction,
                       operations=operations, harness_provenance=harness,
                       official_grader_attempt_count=len(operations), nonempty_submission_count=nonempty,
                       official_infra_failure_count=official.get("infra_failure_instances"),
                       official_ambiguous_failure_count=official.get("ambiguous_failure_instances"),
                       artifact_policy="official boolean reports + submitted patches + safe orchestration events; gold/raw eval output excluded")
        atomic_json(output / "summary.json", summary)
        event("aggregated", status=summary["grading_status"], resolved=summary["resolved_count"], denominator=len(attempts), unknown_count=len(unknown_nonempty))
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="examples/official-smoke/source.json")
    parser.add_argument("--predictions", default="experiments/official-predictions.jsonl")
    parser.add_argument("--output", default="runs/official-smoke")
    parser.add_argument("--run-id", default="official-smoke-" + os.environ.get("GITHUB_RUN_ID", "local") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    parser.add_argument("--test-timeout", type=int, default=900)
    parser.add_argument("--operation-timeout", type=int, default=1500)
    parser.add_argument("--check-inputs", action="store_true")
    args = parser.parse_args()
    if not 30 <= args.test_timeout <= 1800 or not args.test_timeout <= args.operation_timeout <= 2400:
        parser.error("timeouts must be positive, ordered and bounded")
    if args.check_inputs:
        _, _, _, status = read_inputs(args.source, args.predictions)
        print(json.dumps({"grading_status": status, "ready": status == "ready", "official_grade_performed": False}))
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("ready=" + str(status == "ready").lower() + "\n")
        if status != "ready":
            grade(args.source, args.predictions, args.output, args.run_id, args.test_timeout, args.operation_timeout)
        return
    try:
        result = grade(args.source, args.predictions, args.output, args.run_id, args.test_timeout, args.operation_timeout)
    except Exception as exc:
        # Do not echo exception messages containing downloaded grading rows.
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "pipeline-error.json", {"grading_status": "pipeline_error", "exception_type": type(exc).__name__,
                                                    "official_resolved_rate": None, "complete": False})
        print("Official grading pipeline failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({key: result.get(key) for key in ("grading_status", "attempted_denominator", "submitted_count", "resolved_count", "all_attempt_resolved_rate")}))
    if result["grading_status"] == "incomplete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
