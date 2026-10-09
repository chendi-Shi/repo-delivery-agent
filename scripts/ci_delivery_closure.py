"""One genuine Docker coding-agent trial with independent public verification.

Raw trajectories, model responses, spec/runtime files and operator build logs
remain private. The public export contains frozen task/patch provenance, bounded
action hashes and independently measured outcomes; it never injects actions or
an issue solution into the model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.cli import run_repository
from delivery_agent.repository import RepoTask, atomic_json, git, checkout
from delivery_agent.runtime import DockerSession
from scripts.grade_experiments import grade

OLLAMA_VERSION = "0.40.2"
OLLAMA_ARCHIVE_SHA256 = "726bee78706c281b0eeef00746efe51a044d71c592c3f0b195820707f31fdf04"
MODELS = ("qwen2.5-coder:7b", "qwen3:4b-instruct")
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def sha256(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def load_suite_task(task_path):
    """Only the byte-frozen, answer-free operator-approved suite is executable."""
    suite = ROOT / "examples" / "delivery-suite-v1"
    candidate = Path(task_path).absolute()
    if candidate.is_symlink() or candidate.parent.resolve() != suite.resolve():
        raise ValueError("task must be a frozen delivery-suite-v1 member")
    manifest_path = suite / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = [row for row in manifest["tasks"] if row["path"] == candidate.name]
    if len(rows) != 1 or candidate.stat().st_size > 256 * 1024:
        raise ValueError("task is absent from the frozen suite")
    raw = candidate.read_bytes()
    task = RepoTask.parse(json.loads(raw))
    if (sha256(raw) != rows[0]["sha256"] or task.fingerprint() != rows[0]["fingerprint"]
            or task.instance_id != rows[0]["instance_id"]):
        raise ValueError("task bytes or identity differ from the frozen suite")
    approved = {("more-itertools/more-itertools", "81c21a8db1c922260cd673841257fb9b112c1318", "python:3.12-slim"),
                ("psf/requests", "5f7a3a74aab1625c2bb65f643197ee885e3da576", "python:3.8-slim")}
    if (task.repo, task.base_commit, task.image) not in approved or not task.public_checks:
        raise ValueError("task needs an approved public Python base and public checks")
    return task, raw


def hardware_metadata():
    memory = None
    if Path("/proc/meminfo").is_file():
        match = re.search(r"^MemTotal:\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.M)
        if match:
            memory = int(match[1]) * 1024
    return {"system": platform.system(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
            "total_memory_bytes": memory, "agent_container_cpus": 2, "agent_container_memory": "1g"}


class OwnedOllama:
    """Own an isolated daemon; never borrow or stop another local service."""
    def __init__(self, binary, private_root, *, model, context_tokens):
        self.binary, self.private = Path(binary).resolve(), Path(private_root).resolve()
        self.model, self.context_tokens = model, context_tokens
        self.process, self.log, self.identity = None, None, None
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, body=None, *, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        request = Request("http://127.0.0.1:11434" + path, data=data, headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("bounded Ollama setup response exceeded")
        return json.loads(raw)

    def current_identity(self):
        rows = [row for row in self.request("/api/tags").get("models", []) if row.get("name") == self.model]
        if len(rows) != 1 or not SHA.fullmatch(str(rows[0].get("digest", ""))):
            raise ValueError("Ollama model identity is unresolved")
        row = rows[0]
        return {key: row.get(key) for key in ("name", "digest", "size", "details")}

    def __enter__(self):
        if os.name != "posix":
            raise ValueError("this CI runner requires a dedicated Linux Docker worker")
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 11434))
        finally:
            probe.close()
        environment = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_MODELS": str(self.private / "ollama-models"), "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_KEEP_ALIVE": "30m",
            "OLLAMA_CONTEXT_LENGTH": str(self.context_tokens), "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost"}
        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            environment.pop(key, None)
        self.log = (self.private / "ollama.private.log").open("wb")
        try:
            self.process = subprocess.Popen([str(self.binary), "serve"], stdout=self.log, stderr=subprocess.STDOUT,
                                            env=environment, start_new_session=True)
            deadline = time.monotonic() + 120
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError("owned Ollama daemon exited during startup")
                try:
                    if self.request("/api/version", timeout=2).get("version") != OLLAMA_VERSION:
                        raise ValueError("Ollama server version differs from the fixed release")
                    break
                except (OSError, URLError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("owned Ollama startup deadline exceeded")
                    time.sleep(0.2)
            subprocess.run([str(self.binary), "pull", self.model], stdout=self.log, stderr=subprocess.STDOUT,
                           check=True, timeout=1200, env=environment)
            self.identity = self.current_identity()
            self.request("/api/generate", {"model": self.model, "stream": False, "keep_alive": "30m"}, timeout=300)
            self.assert_identity()
            return self
        except BaseException:
            self.close()
            raise

    def assert_identity(self):
        if self.current_identity()["digest"] != self.identity["digest"]:
            raise ValueError("Ollama weights changed during the experiment")

    def close(self):
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=5)
        finally:
            if self.log is not None:
                self.log.close()

    def __exit__(self, *args):
        self.close()


def inspect_image(image):
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", image],
                            check=True, capture_output=True, timeout=60)
    info = json.loads(result.stdout)
    if not IMAGE_ID.fullmatch(info.get("Id", "")):
        raise ValueError("Docker returned an invalid immutable image identity")
    return info


def prepare_task_image(task, private_root):
    """Operator-only image build. Model containers retain network=none."""
    subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True, capture_output=True, timeout=60)
    subprocess.run(["docker", "pull", task.image], check=True, capture_output=True, timeout=600)
    base = inspect_image(task.image)
    digests = base.get("RepoDigests", [])
    candidates = [value for value in digests if isinstance(value, str) and
                  re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*@sha256:[0-9a-f]{64}", value)]
    if not candidates:
        raise ValueError("public base image has no portable repository digest")
    base_digest = sorted(candidates)[0]
    # No task source, solutions or tests enter the build context.
    dockerfile = ("FROM " + base_digest + "\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep "
        "&& rm -rf /var/lib/apt/lists/*\n")
    context = Path(private_root) / "operator-image-context"
    context.mkdir()
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    tag = "repo-delivery-closure:" + uuid.uuid4().hex
    with (Path(private_root) / "image-build.private.log").open("wb") as output:
        subprocess.run(["docker", "build", "--pull=false", "--tag", tag, str(context)], check=True,
                       stdout=output, stderr=subprocess.STDOUT, timeout=900)
    derived = inspect_image(tag)
    effective, public = replace(task, image=derived["Id"]), replace(task, image=base_digest)
    for candidate in (effective, public):
        assert {k: v for k, v in asdict(candidate).items() if k != "image"} == {
            k: v for k, v in asdict(task).items() if k != "image"}
    return effective, public, {"public_base_reference": task.image, "public_base_digest": base_digest,
        "public_base_image_id": base["Id"], "inference_image_id": derived["Id"],
        "operator_dockerfile_sha256": sha256(dockerfile), "operator_image_tag": tag,
        "image_policy": "tools added outside the agent; inference image differs from portable public verifier base"}


def run_baseline_checks(task, private_root):
    """Operator checks before inference; never add this receipt to model trials."""
    work = checkout(task, Path(private_root) / "baseline-workspace")
    records = []
    with DockerSession(work, task.image, timeout=90) as environment:
        resolved = environment.image_id
        for ordinal, command in enumerate(task.public_checks, 1):
            result = environment.execute({"command": command})
            output = result.get("output", "")
            errors = re.findall(r"^([A-Za-z][A-Za-z0-9]*(?:Error|Exception))(?::|$)", output, re.M)
            records.append({"ordinal": ordinal, "command_sha256": sha256(command),
                "returncode": result["returncode"], "passed": result["returncode"] == 0,
                "output_sha256": sha256(output), "output_preview": output[:4000],
                "observed_exception_types": sorted(set(errors))})
    # Suite tasks register existing regression coverage first and the new public
    # contract afterwards. Import/dependency faults do not prove a feature gap.
    feature_errors = {"AssertionError", "TypeError", "UnicodeDecodeError", "AttributeError", "KeyError"}
    first_passed = bool(records) and records[0]["passed"]
    defect = any(not row["passed"] and set(row["observed_exception_types"]) & feature_errors for row in records[1:])
    unknown_failure = any(not row["passed"] and not set(row["observed_exception_types"]) & feature_errors for row in records)
    return {"schema": 1, "kind": "operator_public_baseline", "model_free": True, "llm_calls": 0,
            "task_fingerprint": task.fingerprint(), "public_base_reference": task.image,
            "resolved_image_id": resolved, "checks": records, "initial_check_passed": first_passed,
            "feature_gap_confirmed": first_passed and defect and not unknown_failure,
            "unknown_failure": unknown_failure,
            "scope": "registered public checks before model inference; excluded from model trial denominator"}


def preflight_context(task, args, workspace):
    """Render the actual pinned initial templates without any model/tool call."""
    from delivery_agent.cli import SYSTEM
    from delivery_agent.file_environment import FileEnvironment
    from delivery_agent.ollama_model import OllamaModel
    from delivery_agent.skills import SkillRegistry, BUILTIN_SKILLS
    from delivery_agent.toolbox import ToolboxEnvironment
    from delivery_agent.upstream import (require_upstream, HISTORY_GUIDE, ContextConfig, ContextPolicy,
                                         ContextBudgetExceeded, estimate_tokens, UPSTREAM_VERSION)
    actual_default = require_upstream()
    class TemplateEnvironment:
        def get_template_vars(self):
            return {"cwd": "/workspace", "system": "Linux", "image": task.image}
        def execute(self, *args, **kwargs):
            raise AssertionError("budget preflight must not execute commands")
    model = OllamaModel(model_name="ollama_chat/" + args.model, model_kwargs={"api_base": "http://127.0.0.1:11434",
        "temperature": 0, "seed": args.seed, "max_tokens": args.max_output_tokens, "timeout": args.request_timeout,
        "num_ctx": args.context_tokens, "mode": args.ollama_mode, "think": False})
    environment = TemplateEnvironment()
    skills = SkillRegistry([BUILTIN_SKILLS])
    toolbox = ToolboxEnvironment(environment, skills, None, task.instance_id)
    inventory = FileEnvironment(workspace).inventory(task.repo.rsplit("/", 1)[-1])
    prompt = task.problem_statement + "\n\n" + inventory + \
        "\n\nPublic checks (independent final benchmark tests are unavailable):\n" + "\n".join(task.public_checks)
    instance = "{{task}}" + HISTORY_GUIDE + toolbox.guide()
    agent = actual_default(model, environment, system_template=SYSTEM, instance_template=instance,
        step_limit=args.max_steps, cost_limit=args.cost_limit, wall_time_limit_seconds=args.wall_seconds,
        max_consecutive_format_errors=3)
    agent.extra_template_vars["task"] = prompt
    messages = [model.format_message(role="system", content=agent._render_template(agent.config.system_template)),
                model.format_message(role="user", content=agent._render_template(agent.config.instance_template))]
    policy = ContextPolicy(ContextConfig(args.profile, args.context_tokens, args.max_output_tokens + 1000))
    receipt = {"schema": 1, "kind": "static_context_budget", "model_free": True, "llm_calls": 0,
        "upstream_version": UPSTREAM_VERSION, "default_agent_module": actual_default.__module__,
        "estimate_method": "utf8_bytes_plus_message_framing", "provider_usage": None,
        "context_parameter": args.context_tokens, "reserved_budget": args.max_output_tokens + 1000,
        "available_input_budget": args.context_tokens - args.max_output_tokens - 1000,
        "initial_estimated_input_budget": estimate_tokens(messages), "system_template_sha256": sha256(SYSTEM),
        "user_payload_sha256": sha256(prompt), "inventory_sha256": sha256(inventory),
        "initial_fits": True, "synthetic_ascii_tool_view_fits": True,
        "scope": "policy byte estimate, not measured model tokens; synthetic view is never executed or sent to a provider"}
    try:
        policy.select(messages)
    except ContextBudgetExceeded:
        receipt["initial_fits"] = False
    probe = [*messages, {"role": "assistant", "content": "", "tool_calls": [{"id": "budget-only", "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"budget-only: no execution"}'}}]},
             {"role": "tool", "tool_call_id": "budget-only", "content": "x" * 8000}]
    try:
        _, selected = policy.select(probe)
        receipt["synthetic_ascii_tool_view_estimated_budget"] = selected["estimated_input_tokens"]
    except ContextBudgetExceeded:
        receipt["synthetic_ascii_tool_view_fits"] = False
    return receipt


def public_preview(value):
    """Keep bounded public diagnostics; redact entire secret/private fields."""
    if not isinstance(value, str):
        raise ValueError("action preview must be text")
    sensitive = re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}"
        r"|Bearer\s+[A-Za-z0-9._~+/-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        r"|(?:AKIA|ASIA)[A-Z0-9]{16}", re.I)
    normalized = value.replace("\\", "/").casefold()
    private = (str(ROOT).replace("\\", "/").casefold() in normalized or "/home/runner/" in normalized
               or bool(re.search(r"[a-z]:/users/", normalized)))
    if private or sensitive.search(value):
        return "[REDACTED]", True
    return value[:500], False


def export_action_audit(private_path, public_path):
    """Export typed metadata and redacted, bounded public diagnostic previews."""
    private_path, public_path = Path(private_path), Path(public_path)
    rows, complete = [], True
    if not private_path.is_file():
        public_path.write_text("", encoding="utf-8")
        return {"available": False, "complete": False, "actions": 0, "kind_counts": {}}
    if private_path.is_symlink() or private_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("action audit violates private artifact bounds")
    allowed = {"schema", "schema_version", "event", "call", "ordinal", "tool_call_id", "action_kind",
               "command_sha256", "command_chars", "outcome", "returncode", "output_sha256", "output_chars",
               "exception_type", "elapsed_seconds", "assistant_source_id", "knowledge_source_ids", "knowledge_namespace_sha256",
               "knowledge_operation", "knowledge_records"}
    counts = {}
    for line in private_path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("action audit row must be an object")
        if item.get("event") == "audit_limit":
            complete = False
            continue
        if item.get("event") != "action":
            raise ValueError("action audit contains an unknown event")
        record = {key: item[key] for key in allowed if key in item}
        redacted = []
        for key in ("command_preview", "output_preview"):
            if key in item:
                record[key], removed = public_preview(item[key])
                if removed:
                    redacted.append(key)
        record["preview_redacted"] = bool(redacted)
        if redacted:
            record["redacted_preview_fields"] = redacted
        for key in ("command_sha256", "output_sha256", "knowledge_namespace_sha256"):
            if key in record and record[key] is not None and not SHA.fullmatch(str(record[key])):
                raise ValueError("action audit hash must be SHA256")
        for key in ("call", "ordinal", "command_chars", "output_chars"):
            if key in record and (type(record[key]) is not int or record[key] < 0):
                raise ValueError("action audit counters must be nonnegative integers")
        if "returncode" in record and record["returncode"] is not None and type(record["returncode"]) is not int:
            raise ValueError("action audit returncode must be an integer or null")
        if "elapsed_seconds" in record and (type(record["elapsed_seconds"]) not in (int, float) or
                not math.isfinite(record["elapsed_seconds"]) or record["elapsed_seconds"] < 0):
            raise ValueError("action audit elapsed time must be finite")
        if "action_kind" in record and (not isinstance(record["action_kind"], str) or
                not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}(?:/[A-Za-z0-9_.:-]{1,80})?", record["action_kind"])):
            raise ValueError("action kind must be a bounded tool/operation identifier")
        if "tool_call_id" in record and (not isinstance(record["tool_call_id"], str) or
                not re.fullmatch(r"[A-Za-z0-9_.:-]{0,200}", record["tool_call_id"])):
            raise ValueError("tool call ID must be a bounded identifier")
        for key in ("assistant_source_id", "outcome", "exception_type", "knowledge_operation"):
            if key in record and record[key] is not None and (not isinstance(record[key], str) or
                    not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", record[key])):
                raise ValueError("action audit labels must be bounded identifiers")
        if "knowledge_records" in record:
            # Later transfer runs bind these actual wrapper outputs to seed origins.
            records = record["knowledge_records"]
            if not isinstance(records, list) or len(records) > 8:
                raise ValueError("knowledge audit records must be bounded")
            keys = {"source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref", "verification_sha256"}
            clean = []
            for row in records:
                if (not isinstance(row, dict) or not re.fullmatch(r"k[0-9a-f]{32}", str(row.get("source_id", "")))
                        or type(row.get("verified")) is not bool
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", str(row.get("task_id", "")))
                        or not re.fullmatch(r"run:[0-9a-f]{64}", str(row.get("source_ref", "")))
                        or any(not SHA.fullmatch(str(row.get(key, ""))) for key in
                               ("content_sha256", "namespace_sha256", "verification_sha256"))):
                    raise ValueError("knowledge audit metadata must have source-backed identifiers")
                clean.append({k: row[k] for k in keys})
            record["knowledge_records"] = clean
        if "knowledge_source_ids" in record and (not isinstance(record["knowledge_source_ids"], list)
                or len(record["knowledge_source_ids"]) > 8 or any(not re.fullmatch(r"k[0-9a-f]{32}", str(value))
                                                             for value in record["knowledge_source_ids"])):
            raise ValueError("knowledge source IDs must be bounded identifiers")
        rows.append(record)
        kind = record.get("action_kind", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    public_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return {"available": True, "complete": complete, "actions": len(rows), "kind_counts": counts,
            "private_audit_sha256": sha256(private_path.read_bytes()), "public_audit_sha256": sha256(public_path.read_bytes())}


def export_trial(result, public_task, target, *, profile, budgets, bindings, private_run):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    task_value = asdict(public_task)
    task_value["public_checks"] = list(public_task.public_checks)
    atomic_json(target / "task.json", task_value)
    patch = result.get("model_patch", "")
    if not isinstance(patch, str) or len(patch.encode()) > 8 * 1024 * 1024:
        raise ValueError("model patch exceeds the public artifact budget")
    (target / "model.patch").write_bytes(patch.encode())
    audit = export_action_audit(Path(private_run) / "agent" / "action-audit.jsonl", target / "action-audit.jsonl")
    provenance = result.get("provenance", {})
    requests = Path(private_run) / "agent" / "request-views.jsonl"
    context_requests = 0
    if requests.is_file():
        if requests.is_symlink() or requests.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("private context audit exceeds artifact bounds")
        context_requests = sum(json.loads(line).get("event") == "request"
                               for line in requests.read_text(encoding="utf-8").splitlines())
    internal = result.get("public_checks", [])
    internal_summary = [{key: row.get(key) for key in ("command", "returncode", "passed")}
                        for row in internal if isinstance(row, dict)]
    evidence = {key: result.get(key) for key in ("fixture", "provider", "status", "exit_status", "seconds", "error_type", "patch_valid")}
    evidence.update(profile=profile, model_name=provenance.get("config", {}).get("model"),
        model_digest=provenance.get("model_digest"), patch_sha256=sha256(patch), task_hash=public_task.fingerprint(),
        effective_task_fingerprint=provenance.get("task_hash"), usage=(result.get("delivery") or {}).get("usage") or {},
        context_requests=context_requests,
        verification_status="deferred", internal_verification_status=result.get("verification_status"),
        internal_public_checks=internal_summary, inference_backend="docker", cost_source="unpriced_local",
        implementation_sha256=provenance.get("implementation_sha256"),
        configuration_sha256=sha256(json.dumps(provenance.get("config", {}), sort_keys=True)),
        budgets=budgets, action_audit=audit, bindings=bindings)
    atomic_json(target / "evidence.json", evidence)
    return evidence


def export_official_prediction(source_task, private_run, public_root, model, *, model_digest, public_patch):
    """Export only real results for the byte-matching original public issue."""
    from delivery_agent.benchmark import export_predictions, load_manifest
    original_rows = load_manifest(ROOT / "examples" / "official-smoke" / "manifest.json")
    original_path = ROOT / "examples" / "official-smoke" / "psf__requests-2931.json"
    original_raw = original_path.read_bytes()
    original = RepoTask.parse(json.loads(original_raw))
    matched = all(getattr(source_task, key) == getattr(original, key) for key in
                  ("instance_id", "repo", "base_commit", "problem_statement"))
    if not matched:
        return {"status": "not_original_official_issue", "official_resolved": None}
    original_row = next(row for row in original_rows if row["instance_id"] == original.instance_id)
    keys = ("instance_id", "repo", "base_commit", "problem_statement")
    if any(original_row[key] != getattr(original, key) for key in keys):
        raise ValueError("original task differs from the frozen official snapshot")
    result_path = Path(private_run) / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    provenance = result.get("provenance", {})
    actual = provenance.get("task", {})
    if (any(actual.get(key) != getattr(original, key) for key in keys)
            or provenance.get("model_digest") != model_digest
            or not SHA.fullmatch(str(model_digest))
            or sha256(result["model_patch"]) != sha256(Path(public_patch).read_bytes())):
        raise ValueError("official export result does not bind the actual task, model and public patch")
    model_id = "repo-delivery-" + model.replace(".", "_").replace(":", "-")
    exported = export_predictions([result_path], Path(public_root) / "official-predictions.jsonl", model_id)
    safe = {key: exported[key] for key in ("submitted_count", "attempted_count", "agent_failure_count", "empty_patch_count")}
    safe.update(status="exported_real_attempt", original_public_task_sha256=sha256(original_raw), model_id=model_id,
                task_image_and_checks_differ_from_official=True,
                submitted=exported["submitted"], excluded=[{key: row.get(key) for key in ("instance_id", "reason")}
                                                          for row in exported["excluded"]], official_resolved=None)
    atomic_json(Path(public_root) / "official-export.json", safe)
    return safe


def run_proof(args):
    task, source_raw = load_suite_task(args.task)
    private, public = Path(args.output).resolve(), Path(args.evidence).resolve()
    if private == public or private.is_relative_to(public) or public.is_relative_to(private):
        raise ValueError("private and public roots must be separate")
    private.mkdir(parents=True, exist_ok=False)
    public.mkdir(parents=True, exist_ok=False)
    budgets = {key: getattr(args, key) for key in ("max_steps", "context_tokens", "max_output_tokens", "wall_seconds",
                                                "request_timeout", "command_timeout", "seed", "cost_limit")}
    source_commit = git(ROOT, "rev-parse", "HEAD").stdout.decode().strip()
    summary = {"schema": 1, "kind": "real_docker_delivery_proof", "fixture": False, "mode": "proof",
        "delivery_success": None, "complete": False, "model": args.model, "ollama_mode": args.ollama_mode,
        "source_task_sha256": sha256(source_raw), "source_task_fingerprint": task.fingerprint(),
        "budgets": budgets, "hardware": hardware_metadata(), "official_resolved": None,
        "source_commit": source_commit, "runner_sha256": sha256(Path(__file__).read_bytes()),
        "label": args.label,
        "attempt_started": False, "attempted_count": 0, "attempt_id": None,
        "scope": "one public task; no general effect estimate or official benchmark score"}
    atomic_json(public / "summary.json", summary)
    image = None
    try:
        effective, public_task, image = prepare_task_image(task, private)
        baseline = run_baseline_checks(public_task, private)
        atomic_json(public / "baseline-receipt.json", baseline)
        summary["baseline_receipt_sha256"] = sha256((public / "baseline-receipt.json").read_bytes())
        summary["baseline_feature_gap_confirmed"] = baseline["feature_gap_confirmed"]
        if baseline["resolved_image_id"] != image["public_base_image_id"] or not baseline["feature_gap_confirmed"]:
            raise ValueError("baseline did not confirm a functional gap on the fixed public base")
        preflight = preflight_context(effective, args, private / "baseline-workspace")
        atomic_json(public / "context-preflight.json", preflight)
        summary["context_preflight_sha256"] = sha256((public / "context-preflight.json").read_bytes())
        if not preflight["initial_fits"]:
            raise ValueError("protected initial template exceeds the configured policy byte budget")
        with OwnedOllama(args.ollama, private, model=args.model, context_tokens=args.context_tokens) as daemon:
            environment = {"ollama_version": OLLAMA_VERSION, "ollama_archive_sha256": OLLAMA_ARCHIVE_SHA256,
                "model": daemon.identity, "hardware": summary["hardware"],
                "preload": "empty generate; no task prompt or model-selected action"}
            atomic_json(public / "environment.json", environment)
            bindings = {"source_task_sha256": summary["source_task_sha256"], "source_task_fingerprint": task.fingerprint(),
                        "effective_task_fingerprint": effective.fingerprint(), "public_task_fingerprint": public_task.fingerprint(),
                        **{key: value for key, value in image.items() if key != "operator_image_tag"}}
            trial_args = argparse.Namespace(**vars(args))
            trial_args.model, trial_args.provider, trial_args.backend = "ollama_chat/" + args.model, "ollama_native", "docker"
            trial_args.api_base = "http://127.0.0.1:11434"
            trial_args.skills, trial_args.knowledge_db = [], None
            run_id = task.instance_id + "-" + args.profile + "-" + args.label
            trial = private / run_id
            summary.update(attempt_started=True, attempted_count=1, attempt_id="a" + uuid.uuid4().hex,
                instance_id=task.instance_id, model_digest=daemon.identity["digest"], bindings=bindings,
                model_status="unknown_started", model_calls=None, usage=None)
            atomic_json(public / "summary.json", summary)
            result = run_repository(effective, trial_args, trial)
            summary.update(model_status=result.get("status"), exit_status=result.get("exit_status"),
                           seconds=result.get("seconds"))
            atomic_json(public / "summary.json", summary)
            daemon.assert_identity()
            if result.get("provenance", {}).get("model_digest") != daemon.identity["digest"]:
                raise ValueError("trial did not bind the frozen model weights")
            target = public / "model-runs" / run_id
            evidence = export_trial(result, public_task, target, profile=args.profile, budgets=budgets,
                                    bindings=bindings, private_run=trial)
            summary["official_export"] = export_official_prediction(task, trial, public, args.model,
                model_digest=daemon.identity["digest"], public_patch=target / "model.patch")
            relative = target.relative_to(public).as_posix()
            atomic_json(public / "repository-ablation.json", {"schema": 1, "kind": "real_model_repository_ablation",
                "runs": [{"id": run_id, "task": relative + "/task.json", "patch": relative + "/model.patch",
                          "evidence": relative + "/evidence.json"}],
                "limitations": ["One live Docker trial; no fixed actions or answer patch.",
                                "Inference tools image and independently pulled public verifier base differ."]})
            verified = grade(public / "repository-ablation.json", public / "verification.json", pull_images=True,
                             command_timeout=args.command_timeout)
            core_passed = (result.get("status") == "submitted" and result.get("exit_status") == "Submitted"
                and result.get("patch_valid") is True and len(result.get("public_checks", [])) == len(task.public_checks)
                and all(row.get("passed") is True and row.get("returncode") == 0 for row in result.get("public_checks", [])))
            external_known = not verified["infrastructure_failures"] and len(verified["runs"]) == 1
            external_passed = external_known and verified["runs"][0]["delivery_success"] is True
            summary.update(model_digest=daemon.identity["digest"], bindings=bindings, model_status=result.get("status"),
                exit_status=result.get("exit_status"), internal_independent_checks_passed=core_passed,
                external_independent_checks_passed=external_passed if external_known else None,
                delivery_success=(core_passed and external_passed) if external_known else None,
                complete=external_known, action_audit=evidence["action_audit"],
                public_verification_sha256=sha256((public / "verification.json").read_bytes()))
        atomic_json(public / "summary.json", summary)
        return summary
    except Exception as exc:
        summary.update(complete=False, infrastructure_error_type=type(exc).__name__)
        atomic_json(public / "summary.json", summary)
        raise
    finally:
        if image is not None:
            removed = subprocess.run(["docker", "image", "rm", image["operator_image_tag"]], capture_output=True, timeout=60)
            summary["operator_image_cleanup_exit_code"] = removed.returncode
            atomic_json(public / "summary.json", summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama", required=True)
    parser.add_argument("--task", default="examples/delivery-suite-v1/requests-2931-public.json")
    parser.add_argument("--mode", choices=["proof"], default="proof")
    parser.add_argument("--model", choices=MODELS, default=MODELS[0])
    parser.add_argument("--ollama-mode", choices=["json", "tools"], default="json")
    parser.add_argument("--profile", choices=["baseline", "recent", "retrieval"], default="recent")
    parser.add_argument("--label", default="closure-v1")
    parser.add_argument("--output", default="runs/delivery-closure-private")
    parser.add_argument("--evidence", default="runs/public-delivery-closure")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--context-tokens", type=int, default=16000)
    parser.add_argument("--max-output-tokens", type=int, default=1500)
    parser.add_argument("--wall-seconds", type=int, default=1800)
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--command-timeout", type=int, default=90)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cost-limit", type=float, default=3.0)
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", args.label) or not 1 <= args.max_steps <= 100
            or not 4096 <= args.context_tokens <= 32768 or not 128 <= args.max_output_tokens <= 4000
            or args.max_output_tokens + 1000 >= args.context_tokens or not 60 <= args.wall_seconds <= 3600
            or not 10 <= args.request_timeout <= 300 or not 1 <= args.command_timeout <= 300
            or not math.isfinite(args.cost_limit) or args.cost_limit <= 0):
        parser.error("invalid bounded delivery trial parameters")
    try:
        result = run_proof(args)
    except Exception as exc:
        print("Delivery proof infrastructure failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({key: result.get(key) for key in ("mode", "model_status", "exit_status", "complete", "delivery_success")}))
    return 0 if result["delivery_success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
