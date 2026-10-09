"""Run one official container contract fixture; never claim an LLM score.

The fixed patch only adds a text file and does not solve the public issue. Gold
rows are passed privately to the pinned official harness, never used to choose
the fixture patch or exported. A successful contract requires an applied patch,
parseable hidden test output, an unresolved issue and no infrastructure failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.benchmark import HARNESS_COMMIT, grade_command
from delivery_agent.repository import atomic_json
from scripts.official_smoke_grade import (
    DATASET, PARQUET_SHA, REVISION, cleanup_task_container,
    download_verified_parquet, export_public_reports, read_inputs, run_bounded,
    select_grading_rows, verify_installed_harness, working_directory,
)

INSTANCE_ID = "psf__requests-2931"
FIXTURE_MODEL = "harness-contract-fixture"
FIXTURE_TEXT = "Functional harness fixture only: no LLM calls; not an issue solution.\n"
FIXTURE_PATCH = (
    "diff --git a/delivery_harness_smoke.txt b/delivery_harness_smoke.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/delivery_harness_smoke.txt\n"
    "@@ -0,0 +1 @@\n"
    "+" + FIXTURE_TEXT
)


def fixture_prediction():
    return {"instance_id": INSTANCE_ID, "model_name_or_path": FIXTURE_MODEL,
            "model_patch": FIXTURE_PATCH}


def verify_contract_report(reports):
    """Require canonical official booleans and exact fixture patch evidence."""
    directory = Path(reports) / FIXTURE_MODEL / INSTANCE_ID
    report_path, patch_path = directory / "report.json", directory / "patch.diff"
    if not report_path.is_file() or not patch_path.is_file():
        raise ValueError("official fixture report or patch evidence is missing")
    value = json.loads(report_path.read_text(encoding="utf-8"))
    item = value.get(INSTANCE_ID) if isinstance(value, dict) else None
    expected = {"patch_successfully_applied": True, "resolved": False, "infra_failure": False}
    if (not isinstance(item, dict) or any(type(item.get(key)) is not bool or item[key] is not wanted
                                        for key, wanted in expected.items())):
        raise ValueError("official fixture did not satisfy the container/test contract")
    if patch_path.read_bytes().replace(b"\r\n", b"\n") != FIXTURE_PATCH.encode():
        raise ValueError("official fixture report does not match the controlled patch")
    return {"patch_applied": True, "resolved": False, "infra_failure": False}


def run_smoke(source_path, output_dir, run_id, *, test_timeout=900, operation_timeout=1500):
    if (os.name != "posix" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", run_id)
            or not 30 <= test_timeout <= 900 or not test_timeout <= operation_timeout <= 1500):
        raise ValueError("fixture requires a Linux worker, a safe run ID and bounded timeouts")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("use a fresh fixture output directory")
    summary = {"schema_version": 1, "fixture": True, "llm_calls": 0, "scope": "functional_contract",
               "dataset": DATASET, "revision": REVISION, "parquet_sha256": PARQUET_SHA,
               "harness_commit": HARNESS_COMMIT, "run_id": run_id, "instance_id": INSTANCE_ID,
               "model_name_or_path": FIXTURE_MODEL, "model_score_eligible": False,
               "official_resolved_rate": None, "all_attempt_resolved_rate": None,
               "contract_status": "running", "contract_passed": False,
               "fixture_patch_sha256": hashlib.sha256(FIXTURE_PATCH.encode()).hexdigest(),
               "artifact_policy": "boolean reports + fixture patch + contract metadata; no gold/raw logs"}
    atomic_json(output / "summary.json", summary)
    (output / "fixture.patch").write_bytes(FIXTURE_PATCH.encode())
    try:
        harness = verify_installed_harness()
        summary["harness_provenance"] = harness
        with tempfile.TemporaryDirectory(prefix="repo-delivery-harness-fixture-") as temporary:
            private = Path(temporary)
            # Reuse source pin validation with intentionally absent real-model
            # inputs. This fixture never fabricates Submitted/attempts evidence.
            source, predictions, attempts, status = read_inputs(source_path, private / "absent-real-predictions.jsonl")
            if status != "waiting_for_predictions_and_attempts" or predictions or attempts or INSTANCE_ID not in source["instance_ids"]:
                raise ValueError("fixture source does not contain the fixed public task")
            parquet = private / "official.parquet"
            summary["verified_parquet_bytes"] = download_verified_parquet(source, parquet)
            rows = select_grading_rows(source, parquet, [INSTANCE_ID])
            if len(rows) != 1 or rows[0]["instance_id"] != INSTANCE_ID:
                raise ValueError("fixture grading selection must contain exactly the fixed task")
            grading_source = private / "grading-source.json"
            atomic_json(grading_source, rows)
            prediction = fixture_prediction()
            predictions_path = private / "fixture-predictions.jsonl"
            predictions_path.write_text(json.dumps(prediction) + "\n", encoding="utf-8")
            image = rows[0]["image"]
            with working_directory(private):
                existed = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0
                if shutil.disk_usage(private).free < 8 * 1024 ** 3:
                    raise RuntimeError("insufficient starting disk for the official fixture")
                argv = grade_command(predictions_path, str(grading_source), run_id, python=sys.executable,
                                     max_workers=1, timeout=test_timeout, instance_ids=[INSTANCE_ID])
                try:
                    summary["operation"] = run_bounded(argv, private / "harness.private.log",
                                                       timeout=operation_timeout, private_root=private)
                finally:
                    try:
                        cleanup_task_container(INSTANCE_ID, run_id)
                    finally:
                        inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", image],
                                                   capture_output=True, timeout=30)
                        if inspected.returncode == 0:
                            info = json.loads(inspected.stdout)
                            if not re.fullmatch(r"sha256:[0-9a-f]{64}", info.get("Id", "")):
                                raise ValueError("official image returned an invalid immutable identity")
                            summary["image_id"] = info["Id"]
                            summary["repo_digests"] = info.get("RepoDigests", [])
                            if not existed:
                                removed = subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=60)
                                summary["new_image_cleanup_exit_code"] = removed.returncode
                                if removed.returncode:
                                    raise RuntimeError("could not remove the fixture's newly pulled image")
                export_public_reports(private / harness["run_log_root"] / run_id, output / "reports", [prediction])
                summary.update(verify_contract_report(output / "reports"))
                if summary["operation"]["exit_code"] != 0 or summary["operation"]["stop_reason"] is not None:
                    raise RuntimeError("official fixture CLI did not complete normally")
        summary.update(contract_status="passed", contract_passed=True)
        atomic_json(output / "summary.json", summary)
        return summary
    except Exception as exc:
        # Only the exception type is public: harness errors can include gold.
        summary.update(contract_status="failed", contract_passed=False, exception_type=type(exc).__name__)
        atomic_json(output / "summary.json", summary)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="examples/official-smoke/source.json")
    parser.add_argument("--output", default="runs/official-harness-fixture")
    parser.add_argument("--run-id", default="harness-fixture-" + os.environ.get("GITHUB_RUN_ID", "local") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    parser.add_argument("--test-timeout", type=int, default=900)
    parser.add_argument("--operation-timeout", type=int, default=1500)
    args = parser.parse_args()
    try:
        result = run_smoke(args.source, args.output, args.run_id, test_timeout=args.test_timeout, operation_timeout=args.operation_timeout)
    except Exception as exc:
        print("Official harness fixture failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({key: result[key] for key in ("fixture", "llm_calls", "scope", "contract_status", "patch_applied", "resolved", "infra_failure")}))


if __name__ == "__main__":
    main()
