"""Reproduce the fixed original-ten and separate supplemental-two summaries.

No inference, test execution, artifact download or modification of source
evidence. Download provenance attests archive digests; every local report and
historical request is independently bound before byte-preserving merging.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from delivery_agent.repository import atomic_json
from scripts import summarize_codex_transfer as consumer
from scripts.ingest_verified_delivery import canonical, digest, require

BATCHES = {
    "last": {"directory": "verification-last", "request": "requests/last.json", "stage": "targets-last",
        "source_commit": "8dff3533cc152f00e09cce8192737ae7e10d961f", "run_id": 38030559481,
        "artifact_id": 11660959712, "sha256": "066fbef3dc139372117990292963b1762887927a9a971915c4883ed270556c36",
        "archive_bytes": 20674},
    "split": {"directory": "verification-split", "request": "requests/split-and-supplement.json",
        "stage": "targets-split-and-supplement", "source_commit": "620980cbb2f306ec05f66e4693dcd2389aae94bc",
        "run_id": 38032072691, "artifact_id": 11663175385,
        "sha256": "79c1779a8851c7e48832bd38dde6400e50e328de761e43472a5040b8fb8eeea5", "archive_bytes": 20596},
}


def same(left, right, reason):
    require(canonical(left) == canonical(right), reason)


def supplemental_plan(root, original, planned):
    value, raw = consumer._json(root / "supplement-plan.json")
    split = next(row for row in planned if row["task_group"] == "split")
    trials = [{"label": "split-baseline-empty-r3", "profile": "baseline", "private_output": "runs/ct4/t1"},
              {"label": "split-retrieval-empty-r3", "profile": "retrieval", "private_output": "runs/ct4/t2"}]
    for key, expected in {"schema": 1, "kind": "predeclared_additional_provider_recovery_trials", "fixture": False,
            "original_target_denominator": 10, "additional_trials": 2, "included_in_original_comparison": False,
            "model": original["model"], "provider": original["provider"], "common": original["common"],
            "task": "examples/delivery-suite-v1/more-itertools-split-after.json",
            "task_sha256": split["frozen_task_sha256"], "knowledge_namespace": "codex-transfer-v1",
            "knowledge_mode": "empty", "max_parallel_trials": 2, "trials": trials,
            "availability_probe": "provider-availability-probe.json"}.items():
        same(value.get(key), expected, "supplement changes the declared separate two-trial plan")
    rows = [{**split, "label": row["label"], "profile": row["profile"], "arm": row["profile"] + "-empty",
             "repetition": 3, "included_in_original_comparison": False} for row in trials]
    probe, probe_raw = consumer._json(root / value["availability_probe"])
    require(probe.get("kind") == "response_only_provider_diagnostic"
            and probe.get("excluded_from_task_results") is True and type(probe.get("model_calls")) is int
            and probe["model_calls"] == 1 and probe.get("status") == "completed",
            "supplement provider diagnostic receipt differs")
    return value, rows, {"path": "supplement-plan.json", "sha256": digest(raw)}, {
        "path": value["availability_probe"], "sha256": digest(probe_raw), "model_calls": probe["model_calls"],
        "usage": probe.get("usage"), "seconds": probe.get("seconds"), "excluded_from_task_results": True}


def batch(root, key, labels):
    pin = BATCHES[key]
    directory = root / pin["directory"]
    provenance, provenance_raw = consumer._json(directory / "artifact-provenance.json")
    for name in ("source_commit", "run_id", "artifact_id", "sha256", "archive_bytes"):
        same(provenance.get(name), pin[name], "downloaded verifier artifact differs from pinned provenance")
    require(provenance.get("schema") == 1 and provenance.get("github_digest_verified") is True
            and provenance.get("artifact_name") == f"codex-transfer-verification-{pin['run_id']}-1"
            and provenance.get("run_url") == f"https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/{pin['run_id']}",
            "verifier download provenance is incomplete")
    request, request_raw = consumer._json(root / pin["request"])
    same(request, {"schema": 1, "stage": pin["stage"], "manifests": [
        "experiments/codex-transfer-v1/targets/" + label + "/repository-ablation.json" for label in labels]},
        "historical verifier request changes the complete planned inventory")
    index, index_raw = consumer._json(directory / "summary.json")
    require(index.get("schema") == 1 and index.get("request_sha256") == digest(request_raw),
            "verifier index is unbound to its historical request")
    rows = index.get("trials")
    require(isinstance(rows, list) and len(rows) == len(labels) and all(isinstance(row, dict) for row in rows)
            and [row.get("label") for row in rows] == labels, "verifier index omits, repeats or adds a trial")
    expected = {"artifact-provenance.json", "summary.json", *[label + "/verification.json" for label in labels]}
    actual = set()
    for number, path in enumerate(directory.rglob("*")):
        require(number < 64 and not path.is_symlink(), "verifier artifact contains links or excessive files")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    require(actual == expected, "verifier artifact has missing or unexpected files")
    reports = {}
    for row in rows:
        label = row["label"]
        report, raw = consumer._json(directory / label / "verification.json")
        _, manifest_raw = consumer._json(root / "targets" / label / "repository-ablation.json")
        require(row.get("manifest_sha256") == digest(manifest_raw) == report.get("manifest_sha256"),
                "verifier report is not bound to the actual target manifest")
        require(type(row.get("delivery_successes")) is int and type(row.get("infrastructure_failures")) is int,
                "verifier index counters are malformed")
        same(row["delivery_successes"], report.get("delivery_successes"), "verifier index success count differs")
        same(row["infrastructure_failures"], len(report.get("infrastructure_failures", [])), "verifier index infrastructure count differs")
        reports[label] = {"raw": raw, "report": report, "path": pin["directory"] + "/" + label + "/verification.json"}
    return {"batch": key, **provenance, "download_provenance_sha256": digest(provenance_raw),
        "request": {"path": pin["request"], "sha256": digest(request_raw)},
        "index": {"path": pin["directory"] + "/summary.json", "sha256": digest(index_raw)},
        "labels": labels}, reports


def trial(root, planned, original_plan, source, reports):
    label = planned["label"]
    public = root / "targets" / label
    run, value, entry, refs, measured = consumer._trial(public, planned["profile"], planned["task"],
        planned["frozen_task_sha256"], original_plan, label)
    knowledge = consumer._knowledge(public, run, value, entry, planned, source)
    report = reports[label]
    outcome, report_sha, reason, image = consumer._external(root / report["path"], run, refs["manifest"]["sha256"])
    raw = report["report"]
    success_count = sum(row.get("delivery_success") is True for row in raw["runs"])
    same(raw.get("delivery_successes"), success_count, "report aggregate contradicts verified run outcome")
    public_pass = (len(raw["runs"]) == 1 and raw["runs"][0].get("verification_status") == "passed")
    row = {key: val for key, val in planned.items() if key != "task"}
    row.update(measured, evidence_status="validated", verification_status="validated" if reason is None else reason,
        delivery_success=outcome, knowledge=knowledge, knowledge_observed_read=knowledge["knowledge_observed_read"],
        verification_sha256=report_sha, verification_image=image, public_checks_passed=public_pass,
        artifact_root="targets/" + label, artifacts=refs,
        source_commit=value.get("source_commit"), source_tracked_tree_clean=value.get("source_tracked_tree_clean"),
        verification_artifact={"path": report["path"], "sha256": report_sha})
    return row


def cost_scope(rows):
    complete = all(row.get("usage_complete") is True for row in rows)
    known = sum(row.get("input_tokens") or 0 for row in rows) + sum(row.get("output_tokens") or 0 for row in rows)
    return {"token_total_complete": complete, "reported_tokens_known_subtotal": known,
        "true_total_tokens": known if complete else None,
        "unknown_usage_requests": sum(row.get("unknown_usage_requests") or 0 for row in rows),
        "unknown_usage_trial_count": sum(row.get("usage_complete") is not True for row in rows),
        "monetary_cost": None, "monetary_cost_source": "chatgpt_plan_unpriced",
        "interpretation": "Incomplete usage is a reported lower bound, never a complete zero-cost request. Source and diagnostic costs are separate."}


def check_merged(merged, merge, reports):
    """Reject extra samples and require byte-identical copies of original reports."""
    expected = {"merge-provenance.json", *[item["path"] for item in merge["reports"]]}
    actual = set()
    require(merged.is_dir() and not merged.is_symlink(), "merged verification directory is absent or linked")
    for number, path in enumerate(merged.rglob("*")):
        require(number < 64 and not path.is_symlink(), "merged verification contains links or excessive files")
        if path.is_file():
            actual.add(path.relative_to(merged).as_posix())
    require(len(expected) == 11 and actual == expected, "merged verification must contain exactly ten reports and provenance")
    for item in merge["reports"]:
        require((merged / item["path"]).read_bytes() == reports[item["label"]]["raw"],
                "merged report differs from its original artifact bytes")
    recorded, _ = consumer._json(merged / "merge-provenance.json")
    same(recorded, merge, "merge provenance differs from recomputed inputs")


def finalize(root, *, check=False):
    root = consumer.extended_path(Path(root).resolve())
    merged, output, extra_output = root / "verification-merged", root / "summary.json", root / "supplement-summary.json"
    if not check:
        require(not any(path.exists() for path in (merged, output, extra_output)), "finalization outputs must be fresh")
    plan, plan_sha, planned = consumer._plan(root)
    source, source_cost = consumer._source(root, plan)
    supplement, additional, supplement_ref, probe = supplemental_plan(root, plan, planned)
    last = [row["label"] for row in sorted(planned, key=lambda item: item["repetition"]) if row["task_group"] == "last"]
    split = [row["label"] for row in sorted(planned, key=lambda item: item["repetition"]) if row["task_group"] == "split"]
    last_origin, last_reports = batch(root, "last", last)
    split_origin, split_reports = batch(root, "split", split + [row["label"] for row in additional])
    reports = {**last_reports, **split_reports}
    require(len(reports) == 12 and len(set(last_reports) & set(split_reports)) == 0, "verifier batches overlap")
    original_rows = [trial(root, row, plan, source, reports) for row in planned]
    additional_rows = [trial(root, row, plan, None, reports) for row in additional]
    # Validate preserved setup history before producing any output as well.
    preflight = consumer.summarize(root, root / "verification-does-not-exist")
    require(all(row["evidence_status"] == "validated" for row in preflight["rows"]), "a planned original trial lacks valid evidence")
    copies = []
    for row in planned:
        label = row["label"]
        item = reports[label]
        copies.append({"label": label, "path": label + "/verification.json", "sha256": digest(item["raw"]),
            "bytes": len(item["raw"]), "source_path": item["path"], "source_batch": "last" if label in last else "split"})
    merge = {"schema": 1, "kind": "immutable_original_ten_verifier_merge", "plan_sha256": plan_sha,
        "planned_original_targets": 10, "additional_trials_merged": 0, "source_batches": [last_origin, split_origin],
        "reports": copies, "excluded_supplemental_labels": [row["label"] for row in additional],
        "trust_boundary": "Archive digests are pinned to the recorded verified download provenance; this offline script rechecks extracted report bytes, historical requests and trial bindings, not GitHub authentication."}
    if not check:
        merged.mkdir()
        for item in copies:
            path = merged / item["path"]
            path.parent.mkdir()
            with path.open("xb") as handle:
                handle.write(reports[item["label"]]["raw"])
        atomic_json(merged / "merge-provenance.json", merge)
    check_merged(merged, merge, reports)
    original = consumer.summarize(root, merged)
    for row, independently_checked in zip(original["rows"], original_rows):
        same(row["delivery_success"], independently_checked["delivery_success"], "merged verdict changed")
        for key in ("public_checks_passed", "source_commit", "source_tracked_tree_clean"):
            row[key] = independently_checked[key]
    original["merge_provenance"] = {"path": "verification-merged/merge-provenance.json",
                                     "sha256": digest((merged / "merge-provenance.json").read_bytes())}
    original["cost_scope"] = cost_scope(original["rows"])
    original["supplemental_trials_in_original_denominator"] = 0
    extra = {"schema": 1, "kind": "independently_checked_separate_codex_supplement", "fixture": False,
        "included_in_original_comparison": False, "original_target_denominator": 10, "planned_trials": 2,
        "supplement_plan": supplement_ref, "original_plan_sha256": plan_sha, "verification_batch": split_origin,
        "rows": additional_rows, "targets": consumer._aggregate(additional_rows), "cost_scope": cost_scope(additional_rows),
        "provider_diagnostic": probe, "complete": all(row["delivery_success"] is not None for row in additional_rows),
        "limits": ["Selected engineering follow-up after provider availability returned; not randomized replication.",
                   "Does not replace or erase the four original one-call protocol failures.",
                   "Both knowledge databases are empty; these trials make no knowledge-benefit comparison.",
                   "Provider protocol-error root cause is unconfirmed; dollar cost and official score remain unknown."]}
    if check:
        for path, expected in ((output, original), (extra_output, extra)):
            require(not path.is_symlink(), "summary output cannot be a link")
            actual, _ = consumer._json(path)
            same(actual, expected, "saved summary differs from independently recomputed payload")
    else:
        atomic_json(output, original)
        atomic_json(extra_output, extra)
    return {"original": original["targets"], "original_cost": original["cost_scope"],
        "source": source_cost, "supplement": extra["targets"], "supplement_cost": extra["cost_scope"],
        "provider_diagnostic": probe, "preserved_zero_call_launcher_failures": original["preserved_zero_call_launcher_failures"],
        "knowledge_pairs": original["knowledge_only_pairs"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default="experiments/codex-transfer-v1")
    parser.add_argument("--check", action="store_true", help="read-only: recheck all inputs, the exact merged inventory and both saved summaries")
    args = parser.parse_args(argv)
    print(json.dumps(finalize(args.root, check=args.check), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
