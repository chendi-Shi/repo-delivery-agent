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


def host_memory_telemetry(path=Path("/proc/meminfo")):
    """Read only two bounded numeric Linux resource counters."""
    result = {"status": "unknown", "mem_available_bytes": None, "swap_free_bytes": None}
    try:
        with Path(path).open("r", encoding="ascii") as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError("host memory telemetry exceeds bounds")
        for source, target in (("MemAvailable", "mem_available_bytes"), ("SwapFree", "swap_free_bytes")):
            values = re.findall(r"^" + source + r":\s+(\d+) kB$", content, re.M)
            if len(values) == 1 and int(values[0]) <= 2 ** 53 // 1024:
                result[target] = int(values[0]) * 1024
        result["status"] = "observed" if all(result[key] is not None for key in
            ("mem_available_bytes", "swap_free_bytes")) else "unknown"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    return result


def runtime_telemetry(daemon, phase):
    """Best-effort public allowlist: no other model details, prompts or logs."""
    value = {"schema": 1, "kind": "owned_ollama_runtime_sample", "phase": phase, "status": "unknown",
             "observed_at_unix_seconds": time.time(), "host_memory": host_memory_telemetry(),
             "configured_context_tokens": getattr(daemon, "context_tokens", None), "model_runtime": None}
    try:
        expected = daemon.identity["digest"]
        if not SHA.fullmatch(str(expected)) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,160}", daemon.model):
            raise ValueError("runtime telemetry requires a resolved public model identity")
        response = daemon.request("/api/ps", timeout=5)
        rows = response.get("models")
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError("runtime telemetry model list violates bounds")
        matches = [row for row in rows if isinstance(row, dict) and row.get("name", row.get("model")) == daemon.model
                   and row.get("digest") == expected]
        if len(matches) != 1:
            value["reason"] = "expected_model_not_uniquely_observed"
            return value
        row = matches[0]
        observed = {"name": daemon.model, "digest": expected}
        for key in ("context_length", "size", "size_vram"):
            item = row.get(key)
            observed[key] = item if type(item) is int and 0 <= item <= 2 ** 53 else None
        value.update(model_runtime=observed, status="observed" if all(observed[key] is not None
            for key in ("context_length", "size", "size_vram")) else "partial")
    except Exception as exc:
        value["error_type"] = type(exc).__name__
    return value


class OwnedOllama:
    """Own an isolated daemon; never borrow or stop another local service."""
    def __init__(self, binary, private_root, *, model, context_tokens):
        self.binary, self.private = Path(binary).resolve(), Path(private_root).resolve()
        self.model, self.context_tokens = model, context_tokens
        self.process, self.log, self.identity = None, None, None
        self.prewarm_telemetry = None
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
            self.prewarm()
            self.assert_identity()
            return self
        except BaseException:
            self.close()
            raise

    def assert_identity(self):
        if self.current_identity()["digest"] != self.identity["digest"]:
            raise ValueError("Ollama weights changed during the experiment")

    def prewarm(self):
        try:
            self.request("/api/generate", {"model": self.model, "stream": False, "keep_alive": "30m",
                                          "options": {"num_ctx": self.context_tokens}}, timeout=300)
        finally:
            self.prewarm_telemetry = runtime_telemetry(self, "after_prewarm")

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


def preflight_context(task, args, workspace, knowledge=None):
    """Render the actual pinned initial templates without any model/tool call."""
    from delivery_agent.cli import SYSTEM
    from delivery_agent.file_environment import FileEnvironment
    from delivery_agent.ollama_model import OllamaModel
    from delivery_agent.skills import SkillRegistry, BUILTIN_SKILLS
    from delivery_agent.toolbox import ToolboxEnvironment
    from delivery_agent.upstream import (require_upstream, compose_instance_template, ContextConfig, ContextPolicy,
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
    toolbox = ToolboxEnvironment(environment, skills, knowledge, task.instance_id)
    inventory = FileEnvironment(workspace).inventory(task.repo.rsplit("/", 1)[-1])
    prompt = task.problem_statement + "\n\n" + inventory + \
        "\n\nPublic checks (independent final benchmark tests are unavailable):\n" + "\n".join(task.public_checks)
    instance = compose_instance_template("{{task}}", toolbox=toolbox)
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
    receipt["synthetic_ascii_unselected_estimated_budget"] = estimate_tokens(probe)
    try:
        _, selected = policy.select(probe)
        receipt["synthetic_ascii_tool_view_estimated_budget"] = selected["estimated_input_tokens"]
    except ContextBudgetExceeded as exc:
        receipt["synthetic_ascii_tool_view_fits"] = False
        receipt["synthetic_ascii_budget_error"] = str(exc)
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
    measured_usage, measured_calls = normalized_usage(result, private_run)
    evidence = {key: result.get(key) for key in ("fixture", "provider", "status", "exit_status", "seconds", "error_type", "patch_valid",
                                              "delivery_metadata_source", "delivery_metadata_error_type")}
    evidence.update(profile=profile, model_name=provenance.get("config", {}).get("model"),
        model_digest=provenance.get("model_digest"), patch_sha256=sha256(patch), task_hash=public_task.fingerprint(),
        effective_task_fingerprint=provenance.get("task_hash"), usage=measured_usage,
        context_requests=context_requests,
        model_calls=measured_calls,
        verification_status="deferred", internal_verification_status=result.get("verification_status"),
        internal_public_checks=internal_summary, inference_backend="docker", cost_source="unpriced_local",
        implementation_sha256=provenance.get("implementation_sha256"),
        configuration_sha256=sha256(json.dumps(provenance.get("config", {}), sort_keys=True)),
        budgets=budgets, action_audit=audit, bindings=bindings)
    atomic_json(target / "evidence.json", evidence)
    return evidence


def validate_failed_export(result, private_run):
    """Admit real captured diagnostics without treating failure as submission."""
    if result.get("status") != "failed":
        return False
    if (result.get("fixture") is not False or result.get("provider") != "ollama_native"
            or safe_error_type(result.get("error_type")) is None):
        raise ValueError("failed diagnostic export lacks a real provider error identity")
    patch = result.get("model_patch")
    path = Path(private_run) / "model.patch"
    if (not isinstance(patch, str) or len(patch.encode()) > 8 * 1024 * 1024 or path.is_symlink()
            or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024
            or path.read_bytes() != patch.encode() or result.get("patch_sha256") != sha256(patch)):
        raise ValueError("failed diagnostic patch differs from the captured private artifact")
    if public_preview(patch)[1]:
        raise ValueError("failed diagnostic patch contains a private or credential pattern")
    return True


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
    result = {}
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
                "preload": "empty generate with explicit configured num_ctx; no task prompt or model-selected action",
                "prewarm_runtime": getattr(daemon, "prewarm_telemetry", None)}
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
            summary.update(agent_pipeline_invoked=True, attempt_id="a" + uuid.uuid4().hex,
                instance_id=task.instance_id, model_digest=daemon.identity["digest"], bindings=bindings,
                model_status="unknown_started", model_calls=None, usage=None)
            summary["runtime_telemetry"] = [runtime_telemetry(daemon, "before_agent_pipeline")]
            atomic_json(public / "summary.json", summary)
            try:
                result = run_repository(effective, trial_args, trial)
            finally:
                summary["runtime_telemetry"].append(runtime_telemetry(daemon, "after_agent_pipeline"))
                atomic_json(public / "summary.json", summary)
            measured_usage, measured_calls = normalized_usage(result, trial)
            requested = actual_request_count(trial)
            summary.update(model_status=result.get("status"), exit_status=result.get("exit_status"),
                           seconds=result.get("seconds"), usage=measured_usage, model_calls=measured_calls,
                           attempt_started=requested > 0, attempted_count=int(requested > 0),
                           observed_provider_requests=requested, model_error_type=safe_error_type(result.get("error_type")))
            atomic_json(public / "summary.json", summary)
            if requested == 0:
                raise ValueError("no complete actual agent outcome; setup failures remain infrastructure unknown")
            failed_export = validate_failed_export(result, trial)
            if not failed_export and not isinstance(result.get("delivery"), dict):
                raise ValueError("actual agent outcome lacks delivery metadata")
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
            core_checks = checks_passed(result.get("public_checks"), task.public_checks)
            core_passed = (result.get("status") == "submitted" and result.get("exit_status") == "Submitted"
                and result.get("patch_valid") is True and core_checks is True)
            external_known = not verified["infrastructure_failures"] and len(verified["runs"]) == 1
            external_passed = checks_passed(verified["runs"][0].get("public_checks"), task.public_checks) if external_known else None
            if external_known and verified["runs"][0].get("verification_status") == "patch_invalid":
                external_passed = False
            external_known = external_known and external_passed is not None
            summary.update(model_digest=daemon.identity["digest"], bindings=bindings, model_status=result.get("status"),
                exit_status=result.get("exit_status"), internal_independent_checks_passed=core_checks,
                external_independent_checks_passed=external_passed if external_known else None,
                delivery_success=(core_passed and external_passed) if external_known else None,
                complete=external_known, action_audit=evidence["action_audit"],
                public_verification_sha256=sha256((public / "verification.json").read_bytes()))
        atomic_json(public / "summary.json", summary)
        return summary
    except Exception as exc:
        summary.update(complete=False, runner_error_type=type(exc).__name__,
                       infrastructure_error_type=safe_error_type(result.get("error_type")) or type(exc).__name__)
        if summary.get("agent_pipeline_invoked"):
            try:
                measured_usage, measured_calls = normalized_usage(result, trial)
                summary.update(usage=measured_usage, model_calls=measured_calls)
                requested = actual_request_count(trial)
                if measured_calls and requested == 0:
                    raise ValueError("reported provider usage has no persisted request audit")
                summary.update(attempt_started=requested > 0, attempted_count=int(requested > 0),
                               observed_provider_requests=requested, usage=measured_usage, model_calls=measured_calls,
                               model_error_type=safe_error_type(result.get("error_type")),
                               provider_diagnostics=request_audit_statistics(trial)["diagnostics"])
            except (OSError, ValueError):
                summary.update(attempt_started=None, attempted_count=None, observed_provider_requests=None,
                               attempt_accounting="unknown_unreadable_request_audit")
        atomic_json(public / "summary.json", summary)
        raise
    finally:
        if image is not None:
            removed = subprocess.run(["docker", "image", "rm", image["operator_image_tag"]], capture_output=True, timeout=60)
            summary["operator_image_cleanup_exit_code"] = removed.returncode
            atomic_json(public / "summary.json", summary)


def implementation_identity():
    """Match the actual CLI code identity and freeze the available skill resources."""
    from delivery_agent.skills import SkillRegistry
    digest = hashlib.sha256()
    for path in sorted((ROOT / "delivery_agent").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    skills = SkillRegistry([ROOT / "delivery_agent" / "resources" / "skills"])
    return digest.hexdigest(), skills.manifest()["manifest_sha256"]


def artifact_ref(path, public):
    path, public = Path(path), Path(public).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(public):
        raise ValueError("cohort artifact must remain in its public root")
    return {"path": path.resolve().relative_to(public).as_posix(), "sha256": sha256(path.read_bytes())}


def load_public_json(root, reference, cap=1024 * 1024):
    root = Path(root).resolve()
    path = root / reference["path"]
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > cap:
        raise ValueError("cohort reference escaped its bounded public root")
    raw = path.read_bytes()
    if sha256(raw) != reference["sha256"]:
        raise ValueError("cohort artifact hash differs from its receipt")
    return json.loads(raw)


def suite_tasks():
    directory = ROOT / "examples" / "delivery-suite-v1"
    return {name: load_suite_task(directory / name) for name in
        ("more-itertools-first-factory.json", "more-itertools-last-factory.json", "more-itertools-split-after.json")}


def freeze_cohort(args, tasks, image, identity):
    implementation, tools = implementation_identity()
    budgets = {key: getattr(args, key) for key in ("max_steps", "context_tokens", "max_output_tokens", "wall_seconds",
        "request_timeout", "command_timeout", "cost_limit")}
    budgets["temperature"] = 0
    arms = [{"id": "baseline", "profile": "baseline", "knowledge": "none"},
            {"id": "retrieval", "profile": "retrieval", "knowledge": "none"}]
    if args.mode == "transfer":
        arms.append({"id": "verified-source", "profile": "retrieval", "knowledge": "verified_source"})
    targets = []
    for filename, role in (("more-itertools-last-factory.json", "near_transfer"),
                           ("more-itertools-split-after.json", "holdout")):
        task, raw = tasks[filename]
        allowed_arms = [row["id"] for row in arms if role == "near_transfer" or row["knowledge"] == "none"]
        targets.append({"instance_id": task.instance_id, "role": role,
            "task_sha256": sha256(raw), "task_fingerprint": task.fingerprint(), "repo": task.repo,
            "base_commit": task.base_commit, "inference_image_id": image["inference_image_id"],
            "public_base_image_id": image["public_base_image_id"],
            "public_checks_sha256": sha256(json.dumps(list(task.public_checks), ensure_ascii=False, separators=(",", ":"))),
            "arms": allowed_arms})
    return {"schema": 1, "kind": "fixed_closure_cohort", "fixture": False, "frozen_before_execution": True,
        "suite_id": "delivery-suite-v1", "mode": args.mode, "model": {"name": args.model, "digest": identity["digest"]},
        "common": {"provider": "ollama_native", "backend": "docker", "tools_sha256": tools,
                   "implementation_sha256": implementation, "budgets": budgets},
        "arms": arms, "reference_arm": "baseline", "seeds": [42, 43], "targets": targets,
        "source_attempts": ([{"instance_id": tasks["more-itertools-first-factory.json"][0].instance_id, "seed": 42}]
                            if args.mode == "transfer" else []),
        "image": {key: value for key, value in image.items() if key != "operator_image_tag"},
        "knowledge_namespace": "closure:more-itertools:81c21a8db1c922260cd673841257fb9b112c1318",
        "source_commit": git(ROOT, "rev-parse", "HEAD").stdout.decode().strip(),
        "limitations": ["Ten paired public target trials, not an official benchmark score.",
                        "Verified source experience is permitted only on the distinct near-transfer task."]}


def checks_passed(rows, expected):
    if not isinstance(rows, list) or len(rows) != len(expected):
        return None
    if any(row.get("command") != command for row, command in zip(rows, expected)):
        raise ValueError("check receipt differs from its frozen public command")
    if any(type(row.get("returncode")) is not int for row in rows):
        return None
    return all(row.get("returncode") == 0 and row.get("passed") is True for row in rows)


def normalized_usage(result, private_trial=None):
    delivery = result.get("delivery") or {}
    usage = delivery.get("usage") or {}
    calls = delivery.get("model_calls")
    if type(calls) is not int or calls < 0:
        calls = None
    observed = usage.get("calls_with_usage", 0)
    unknown = usage.get("unknown_requests", 0)
    if type(observed) is not int or type(unknown) is not int or min(observed, unknown) < 0:
        raise ValueError("invalid actual provider usage counters")
    if calls is not None and observed > calls:
        raise ValueError("measured usage exceeds the reported provider calls")
    tokens = {key: usage.get(key) for key in ("input_tokens", "output_tokens")}
    if any(value is not None and (type(value) is not int or value < 0) for value in tokens.values()):
        raise ValueError("invalid actual provider token measurements")
    unknown = max(unknown, calls - observed if calls is not None else 0)
    complete = (usage.get("complete") is True and calls is not None and observed == calls and unknown == 0
                and all(value is not None for value in tokens.values()))
    measured = {"complete": complete, **tokens, "calls_with_usage": observed, "unknown_requests": unknown}
    if not complete and private_trial is not None:
        audit = Path(private_trial) / "agent" / "request-views.jsonl"
        # Missing durable evidence cannot erase a result's known calls or
        # confirmed partial token subtotal. Request-denominator checks remain
        # separate and fail closed when those sources disagree.
        if not audit.exists() and (calls or observed or unknown or any(value is not None for value in tokens.values())):
            return measured, calls
        recovered = request_audit_statistics(private_trial)
        if (calls is not None and recovered["model_calls"] < calls or
                recovered["usage"]["calls_with_usage"] < observed or
                any(value is not None and (recovered["usage"][key] is None or
                    recovered["usage"][key] < value) for key, value in tokens.items())):
            calls = max(calls or 0, recovered["model_calls"])
            measured["unknown_requests"] = max(unknown, calls - observed)
            return measured, calls
        return recovered["usage"], recovered["model_calls"]
    return measured, calls


def actual_request_count(private_trial):
    """Count persisted provider requests, excluding checkout/setup/template work."""
    path = Path(private_trial) / "agent" / "request-views.jsonl"
    if not path.exists():
        if (path.parent / "checkpoint.json").exists():
            raise ValueError("checkpoint without a provider request audit cannot establish zero requests")
        return 0
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("provider request audit violates bounds")
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "request":
            calls.append(row.get("call"))
    if any(type(call) is not int or call < 1 for call in calls) or len(calls) != len(set(calls)):
        raise ValueError("provider request audit counters are ambiguous")
    return len(calls)


def safe_error_type(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", value) else None


def request_audit_statistics(private_trial):
    """Recover confirmed provider measurements without publishing raw requests.

    Incomplete token values are confirmed subtotals, not total usage. Requests
    without confirmed token usage remain unknown, including provider timeouts.
    A checksum-validated checkpoint can cover the durable-response-before-log
    crash window; it never creates an unaudited provider request.
    """
    directory = Path(private_trial) / "agent"
    audit = directory / "request-views.jsonl"
    if not audit.exists() and (directory / "checkpoint.json").exists():
        raise ValueError("checkpoint without a provider request audit cannot establish zero usage")
    requested, responses, errors = set(), {}, set()
    if audit.exists():
        if audit.is_symlink() or audit.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("provider request audit violates bounds")
        for line in audit.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            event, call = row.get("event"), row.get("call")
            if event not in {"request", "response", "error"}:
                continue
            if type(call) is not int or call < 1:
                raise ValueError("provider request audit counters are ambiguous")
            if event == "request":
                if call in requested:
                    raise ValueError("provider request audit contains duplicate requests")
                requested.add(call)
            elif call not in requested:
                raise ValueError("provider observation has no preceding request")
            elif event == "response":
                if call in responses:
                    raise ValueError("provider request audit contains duplicate responses")
                responses[call] = row.get("usage")
            elif error := safe_error_type(row.get("exception_type")):
                errors.add(error)
    covered, prompt, completion, unknown_hint = 0, 0, 0, 0
    for usage in responses.values():
        if not isinstance(usage, dict):
            continue
        left = usage.get("prompt_tokens", usage.get("input_tokens"))
        right = usage.get("completion_tokens", usage.get("output_tokens"))
        if all(type(value) is int and value >= 0 for value in (left, right)):
            covered += 1
            prompt += left
            completion += right
    diagnostics = {"usage_source": "durable_request_audit", "request_error_types": sorted(errors),
                   "request_audit_sha256": file_sha256(audit) if audit.is_file() else None,
                   "checkpoint_usage_recovered": False,
                   "incomplete_token_values": "confirmed subtotals; unknown requests are excluded"}
    checkpoint = directory / "checkpoint.json"
    if checkpoint.exists():
        try:
            if checkpoint.is_symlink() or checkpoint.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("checkpoint measurement artifact violates bounds")
            from delivery_agent.checkpoint import inspect_checkpoint
            measured = inspect_checkpoint(checkpoint)["state"].get("model_usage", {})
            keys = ("n_calls", "calls_with_usage", "input_tokens", "output_tokens", "unknown_requests")
            if (any(type(measured.get(key)) is not int or measured[key] < 0 for key in keys)
                    or measured["n_calls"] != len(requested) or measured["calls_with_usage"] > len(requested)
                    or measured["unknown_requests"] > len(requested)):
                raise ValueError("checkpoint measurements differ from audited provider requests")
            if measured["calls_with_usage"] == covered and (
                    measured["input_tokens"] != prompt or measured["output_tokens"] != completion):
                raise ValueError("checkpoint measurements contradict confirmed audit totals")
            if measured["calls_with_usage"] > covered:
                if measured["input_tokens"] < prompt or measured["output_tokens"] < completion:
                    raise ValueError("checkpoint measurements discard confirmed audit usage")
                covered, prompt, completion = (measured[key] for key in
                                               ("calls_with_usage", "input_tokens", "output_tokens"))
                diagnostics.update(usage_source="durable_checkpoint_and_request_audit",
                                   checkpoint_usage_recovered=True)
            diagnostics["checkpoint_sha256"] = file_sha256(checkpoint)
            unknown_hint = measured["unknown_requests"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            diagnostics["checkpoint_error_type"] = type(exc).__name__
    calls = len(requested)
    unknown = max(calls - covered, unknown_hint)
    usage = {"complete": covered == calls and unknown == 0, "input_tokens": prompt if covered or calls == 0 else None,
             "output_tokens": completion if covered or calls == 0 else None,
             "calls_with_usage": covered, "unknown_requests": unknown}
    return {"model_calls": calls, "usage": usage, "diagnostics": diagnostics}


def observed_source_reuse(audit, origin):
    """A match must come from a completed actual tool result, never model prose."""
    if not origin or not Path(audit).is_file():
        return False
    required = ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref", "verification_sha256")
    for line in Path(audit).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if (row.get("event") == "action" and row.get("outcome") == "completed" and row.get("returncode") == 0
                and row.get("action_kind") == "delivery-knowledge/read"
                and row.get("knowledge_operation") == "read"
                and row.get("knowledge_namespace_sha256") == origin["namespace_sha256"]):
            if any(all(record.get(key) == origin[key] for key in required) for record in row.get("knowledge_records", [])):
                return True
    return False


def execute_cohort_trial(args, plan, task, raw, image, daemon, private, public, relative, *, arm, role, origin=None):
    """Use the real agent once and independently grade its real patch, including failures."""
    from delivery_agent.knowledge import KnowledgeStore
    private, public = Path(private), Path(public)
    target = public / relative
    target.mkdir(parents=True, exist_ok=False)
    effective, public_task = replace(task, image=image["inference_image_id"]), replace(task, image=image["public_base_digest"])
    trial_args = argparse.Namespace(**vars(args))
    trial_args.profile = arm["profile"]
    trial_args.model, trial_args.provider, trial_args.backend = "ollama_chat/" + args.model, "ollama_native", "docker"
    trial_args.api_base, trial_args.skills = "http://127.0.0.1:11434", []
    trial_args.knowledge_namespace = plan["knowledge_namespace"]
    trial_args.knowledge_db = str(private / "knowledge.sqlite")
    store = KnowledgeStore(trial_args.knowledge_db, plan["knowledge_namespace"])
    if origin is not None:
        imported = store.put(**origin["record"], ttl_days=None)
        verify_seed_metadata(imported, origin["origin"])
    frozen_task = {"task_sha256": sha256(raw), "task_fingerprint": task.fingerprint(), "repo": task.repo,
        "base_commit": task.base_commit, "inference_image_id": image["inference_image_id"],
        "public_base_image_id": image["public_base_image_id"],
        "public_checks_sha256": sha256(json.dumps(list(task.public_checks), ensure_ascii=False, separators=(",", ":")))}
    budgets = {**plan["common"]["budgets"], "seed": args.seed}
    summary = {"schema": 1, "fixture": False, "kind": "real_docker_closure_trial", "attempt_started": False,
        "attempted_count": 0, "instance_id": task.instance_id, "model_status": "infrastructure_failed",
        "delivery_success": None, "complete": False, "official_resolved": None, "model": args.model,
        "model_digest": plan["model"]["digest"], "budgets": budgets, "profile": arm["profile"], "arm": arm["id"],
        "hardware": hardware_metadata(), "source_task_sha256": sha256(raw),
        "prewarm_runtime": getattr(daemon, "prewarm_telemetry", None)}
    result, evidence, verified = {}, None, None
    internal, external = None, None
    failure = False
    summary_path = target / "summary.json"
    atomic_json(summary_path, summary)
    try:
        baseline = run_baseline_checks(public_task, private)
        atomic_json(target / "baseline-receipt.json", baseline)
        if baseline["resolved_image_id"] != image["public_base_image_id"] or not baseline["feature_gap_confirmed"]:
            raise ValueError("target baseline is unknown or has no missing feature")
        preflight_args = argparse.Namespace(**vars(args))
        preflight_args.profile = arm["profile"]
        preflight = preflight_context(effective, preflight_args, private / "baseline-workspace", knowledge=store)
        atomic_json(target / "context-preflight.json", preflight)
        if not preflight["initial_fits"] or not preflight["synthetic_ascii_tool_view_fits"]:
            raise ValueError("cohort protected template and latest observation do not fit")
        daemon.assert_identity()
        if daemon.identity["digest"] != plan["model"]["digest"]:
            raise ValueError("cohort model differs from the predeclared weights")
        summary.update(attempt_started=True, attempted_count=1, attempt_id="a" + uuid.uuid4().hex,
                       model_status="unknown_started")
        summary["runtime_telemetry"] = [runtime_telemetry(daemon, "before_agent_pipeline")]
        atomic_json(summary_path, summary)
        try:
            result = run_repository(effective, trial_args, private / "trial")
        finally:
            summary["runtime_telemetry"].append(runtime_telemetry(daemon, "after_agent_pipeline"))
            atomic_json(summary_path, summary)
        requested = actual_request_count(private / "trial")
        summary.update(attempt_started=requested > 0, attempted_count=int(requested > 0),
                       observed_provider_requests=requested, agent_pipeline_invoked=True)
        summary.update(model_status=result.get("status"), exit_status=result.get("exit_status"), seconds=result.get("seconds"),
                       model_error_type=safe_error_type(result.get("error_type")))
        atomic_json(summary_path, summary)
        if requested == 0:
            raise ValueError("no complete actual agent outcome; setup failures remain infrastructure unknown")
        failed_export = validate_failed_export(result, private / "trial")
        if not failed_export and not isinstance(result.get("delivery"), dict):
            raise ValueError("actual agent outcome lacks delivery metadata")
        if (result.get("provenance", {}).get("model_digest") != plan["model"]["digest"]
                or result.get("provenance", {}).get("implementation_sha256") != plan["common"]["implementation_sha256"]):
            raise ValueError("trial identity differs from the frozen cohort")
        daemon.assert_identity()
        bindings = {"source_task_sha256": sha256(raw), "source_task_fingerprint": task.fingerprint(),
                    "effective_task_fingerprint": effective.fingerprint(), "public_task_fingerprint": public_task.fingerprint(),
                    **{key: value for key, value in image.items() if key != "operator_image_tag"}}
        run = target / "model-run"
        evidence = export_trial(result, public_task, run, profile=arm["profile"], budgets=budgets,
                                bindings=bindings, private_run=private / "trial")
        manifest = target / "repository-ablation.json"
        atomic_json(manifest, {"schema": 1, "kind": "real_model_repository_ablation", "runs": [{"id": args.label,
            "task": os.path.relpath(run / "task.json", target).replace("\\", "/"),
            "patch": os.path.relpath(run / "model.patch", target).replace("\\", "/"),
            "evidence": os.path.relpath(run / "evidence.json", target).replace("\\", "/")}],
            "limitations": ["Real Docker inference; independent verifier uses the frozen portable public base."]})
        verified = grade(manifest, target / "verification.json", pull_images=True, command_timeout=args.command_timeout)
        internal = checks_passed(result.get("public_checks"), task.public_checks)
        if verified["infrastructure_failures"] or len(verified["runs"]) != 1:
            failure = True
        else:
            external = checks_passed(verified["runs"][0].get("public_checks"), task.public_checks)
            if verified["runs"][0].get("verification_status") == "patch_invalid":
                external = False
        submitted = result.get("status") == "submitted" and result.get("exit_status") == "Submitted"
        # Missing verification is unknown, never a model failure or success.
        if external is None or (submitted and internal is None):
            failure = True
        if failure:
            external = None
        success = None if failure else submitted and result.get("patch_valid") is True and internal is True and external is True
        summary.update(complete=not failure, delivery_success=success, internal_checks_passed=internal,
                       external_checks_passed=external, action_audit=evidence["action_audit"])
    except Exception as exc:
        failure = True
        external = None
        requested = actual_request_count(private / "trial")
        summary.update(attempt_started=requested > 0, attempted_count=int(requested > 0),
                       observed_provider_requests=requested)
        summary.update(runner_error_type=type(exc).__name__,
                       infrastructure_error_type=safe_error_type(result.get("error_type")) or type(exc).__name__,
                       model_error_type=safe_error_type(result.get("error_type")), complete=False, delivery_success=None)
    finally:
        store.close()
        atomic_json(summary_path, summary)
    usage, calls = normalized_usage(result, private / "trial")
    if calls and not summary["attempt_started"]:
        summary.update(usage=usage, model_calls=calls, attempt_started=None, attempted_count=None,
                       observed_provider_requests=None, attempt_accounting="unknown_missing_request_audit")
        atomic_json(summary_path, summary)
        raise ValueError("reported provider usage has no matching persisted request audit")
    submitted = result.get("status") == "submitted" and result.get("exit_status") == "Submitted"
    status = result.get("status") if result.get("status") in {"submitted", "stopped", "failed"} else "infrastructure_failed"
    summary.update(model_status=status, exit_status=result.get("exit_status"), seconds=result.get("seconds"),
                   usage=usage, model_calls=calls, internal_checks_passed=internal, external_checks_passed=external)
    if failure:
        summary["provider_diagnostics"] = request_audit_statistics(private / "trial")["diagnostics"]
    atomic_json(summary_path, summary)
    receipt = {"schema": 1, "kind": "closure_trial_receipt", "fixture": False,
        "instance_id": task.instance_id, "seed": args.seed, "arm": arm["id"], "role": role,
        "model": plan["model"], "common": plan["common"], "profile": arm["profile"], "knowledge": arm["knowledge"],
        "task": frozen_task, "status": status, "exit_status": result.get("exit_status"), "submitted": submitted,
        "internal_checks_passed": internal, "external_checks_passed": external,
        "delivery_success": summary["delivery_success"], "usage": usage, "model_calls": calls,
        "seconds": result.get("seconds"), "infrastructure_failed": failure,
        "attempt_started": summary["attempt_started"], "attempted_count": summary["attempted_count"],
        "knowledge_observed_reuse": observed_source_reuse(target / "model-run" / "action-audit.jsonl", origin["origin"] if origin else None),
        "artifacts": {"summary": artifact_ref(summary_path, public),
            "evidence": artifact_ref(target / "model-run" / "evidence.json", public) if evidence else None,
            "verification": artifact_ref(target / "verification.json", public) if verified else None}}
    atomic_json(target / "trial-receipt.json", receipt)
    return receipt, result, private / "trial"


def verify_seed_metadata(actual, expected):
    verification_hash = sha256(json.dumps(actual.get("verification", {}), ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False))
    actual = {**actual, "verification_sha256": verification_hash}
    keys = ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref", "verification_sha256")
    if any(actual.get(key) != expected.get(key) for key in keys) or expected.get("verified") is not True:
        raise ValueError("reconstructed experience differs from the independently verified source")


def collect_python_sources(workspace):
    package = Path(workspace).resolve()
    files = []
    for current, directories, names in os.walk(package, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        if any((Path(current) / name).is_symlink() for name in directories):
            raise ValueError("source guard input contains a directory symlink")
        files.extend(Path(current) / name for name in names if name.endswith(".py"))
        if len(files) > 200:
            raise ValueError("source guard exceeds its repository budget")
    result = {}
    for path in sorted(files):
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(package) or resolved.stat().st_size > 512 * 1024:
            raise ValueError("source guard input escaped its bounded package")
        result[resolved.relative_to(package).as_posix()] = resolved.read_text(encoding="utf-8")
    if sum(len(value.encode()) for value in result.values()) > 4 * 1024 * 1024:
        raise ValueError("source guard exceeds its total byte budget")
    return result


def build_verified_seed(plan, source, source_raw, result, trial, receipt, private, public, tasks):
    from delivery_agent.knowledge import KnowledgeStore, MAX_CONTENT_CHARS
    from delivery_agent.repository import apply_fresh
    from scripts.closure_seed_guard import evaluate_source_seed
    package = {"schema": 1, "kind": "verified_closure_seed_package", "fixture": False,
        "eligible": False, "plan_sha256": sha256((public / "suite-plan.json").read_bytes()),
        "record": None, "origin": None, "source_receipt": artifact_ref(public / "source-trial" / "trial-receipt.json", public)}
    if receipt["delivery_success"] is not True or receipt["infrastructure_failed"]:
        package["reason"] = "source_not_independently_delivered"
        return package
    pristine = checkout(source, private / "seed-pristine", source=trial / "workspace")
    candidate = apply_fresh(source, result["model_patch"], private / "seed-candidate", source=pristine)
    guard = evaluate_source_seed(collect_python_sources(pristine), collect_python_sources(candidate))
    checks = []
    with DockerSession(candidate, plan["image"]["public_base_digest"], timeout=90) as verifier:
        for filename in ("more-itertools-last-factory.json", "more-itertools-split-after.json"):
            task = tasks[filename][0]
            output = verifier.execute({"command": task.public_checks[-1]})
            errors = re.findall(r"\b(AssertionError|TypeError|UnicodeDecodeError|AttributeError|KeyError)\b", output["output"])
            checks.append({"instance_id": task.instance_id, "returncode": output["returncode"],
                "command_sha256": sha256(task.public_checks[-1]), "output_sha256": sha256(output["output"]),
                "output_preview": public_preview(output["output"])[0],
                "target_feature_still_missing": output["returncode"] != 0 and bool(errors), "exception_types": sorted(set(errors))})
    guard["functional_targets"] = checks
    guard["eligible"] = guard["eligible"] is True and all(row["target_feature_still_missing"] for row in checks)
    atomic_json(public / "source-leakage-guard.json", guard)
    package["guard"] = artifact_ref(public / "source-leakage-guard.json", public)
    if not guard["eligible"]:
        package["reason"] = "source_changed_or_solved_a_target"
        return package
    store = KnowledgeStore(private / "frozen-seed.sqlite", plan["knowledge_namespace"])
    metadata = store.ingest_run(trial, ttl_days=None)
    body, offset = "", 0
    while True:
        chunk = store.read(metadata["source_id"], offset=offset, limit=4000)
        body += chunk["text"]
        if len(body) > MAX_CONTENT_CHARS:
            raise ValueError("verified seed exceeds its content budget")
        if chunk["next_offset"] is None:
            break
        offset = chunk["next_offset"]
    store.close()
    if public_preview(body)[1]:
        package["reason"] = "source_content_has_private_or_credential_pattern"
        return package
    record = {key: metadata[key] for key in ("title", "source_type", "source_ref", "repo", "base_commit", "task_id", "verified")}
    record.update(body=body, verification=metadata["verification"])
    origin = {key: metadata[key] for key in ("source_id", "content_sha256", "task_id", "namespace_sha256", "verified", "source_ref")}
    origin["verification_sha256"] = sha256(json.dumps(record["verification"], ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":"), allow_nan=False))
    package.update(eligible=True, reason="independently_verified_source_only", record=record, origin=origin,
                   source_task_sha256=sha256(source_raw))
    return package


def run_cohort_seed(args):
    if args.context_tokens != 24000 or args.model != "qwen3:4b-instruct" or args.ollama_mode != "json":
        raise ValueError("the fixed initial suite cohort requires Qwen3 4B, 24K, and JSON commands")
    private, public = Path(args.output).resolve(), Path(args.evidence).resolve()
    if private == public or private.is_relative_to(public) or public.is_relative_to(private):
        raise ValueError("cohort private and public roots must be separate")
    private.mkdir(parents=True, exist_ok=False)
    public.mkdir(parents=True, exist_ok=False)
    tasks = suite_tasks()
    source, raw = tasks["more-itertools-first-factory.json"]
    image = None
    try:
        _, _, image = prepare_task_image(source, private)
        with OwnedOllama(args.ollama, private, model=args.model, context_tokens=args.context_tokens) as daemon:
            plan = freeze_cohort(args, tasks, image, daemon.identity)
            atomic_json(public / "suite-plan.json", plan)  # Before any source or target task model request.
            matrix = {"include": [{"instance_id": row["instance_id"], "seed": seed, "arm": arm}
                for row in plan["targets"] for seed in plan["seeds"] for arm in row["arms"]]}
            atomic_json(public / "matrix.json", matrix)
            atomic_json(public / "environment.json", {"model": daemon.identity, "hardware": hardware_metadata(),
                "ollama_version": OLLAMA_VERSION, "ollama_archive_sha256": OLLAMA_ARCHIVE_SHA256,
                "image": plan["image"], "official_resolved": None,
                "prewarm_runtime": getattr(daemon, "prewarm_telemetry", None)})
            sources = []
            if args.mode == "transfer":
                source_private = private / "source"
                source_private.mkdir()
                source_args = argparse.Namespace(**vars(args))
                source_args.seed = 42
                arm = {"id": "source", "profile": "recent", "knowledge": "none"}
                receipt, result, trial = execute_cohort_trial(source_args, plan, source, raw, image, daemon,
                    source_private, public, "source-trial", arm=arm, role="source")
                sources.append({"instance_id": source.instance_id, "seed": 42,
                                "receipt": artifact_ref(public / "source-trial" / "trial-receipt.json", public)})
                try:
                    package = build_verified_seed(plan, source, raw, result, trial, receipt, private, public, tasks)
                except Exception as exc:
                    # A failed source gate blocks only seeded target arms; preserve
                    # the planned cohort and continue all unseeded comparisons.
                    package = {"schema": 1, "kind": "verified_closure_seed_package", "fixture": False,
                        "eligible": False, "reason": "source_guard_or_ingestion_failed", "error_type": type(exc).__name__,
                        "record": None, "origin": None, "plan_sha256": sha256((public / "suite-plan.json").read_bytes())}
            else:
                package = {"schema": 1, "kind": "verified_closure_seed_package", "fixture": False, "eligible": False,
                    "reason": "pair_mode_has_no_verified_source_treatment", "record": None, "origin": None,
                    "plan_sha256": sha256((public / "suite-plan.json").read_bytes())}
            atomic_json(public / "seed-package.json", package)
            atomic_json(public / "seed-provenance.json", {key: package.get(key) for key in
                ("schema", "kind", "fixture", "eligible", "reason", "plan_sha256", "origin", "source_receipt", "guard")})
            atomic_json(public / "observations-source.json", {"schema": 1, "kind": "closure_cohort_observations",
                "plan_sha256": sha256((public / "suite-plan.json").read_bytes()), "runs": [], "source_attempts": sources})
        archive = public / "tools-image.tar"
        subprocess.run(["docker", "image", "save", "--output", str(archive), image["operator_image_tag"]], check=True, timeout=180)
        if archive.stat().st_size > 2 * 1024 ** 3:
            raise ValueError("owned public tools image exceeds the transfer budget")
        atomic_json(public / "image-transfer.json", {"schema": 1, "kind": "public_tools_image_transfer",
            "image_id": image["inference_image_id"], "archive_sha256": file_sha256(archive),
            "archive_bytes": archive.stat().st_size, "contents": "public Python base plus git/ripgrep; built before model execution"})
        return {"complete": True, "mode": args.mode, "expected_target_trials": len(matrix["include"]),
                "source_seed_eligible": package["eligible"], "delivery_success": None}
    finally:
        if image:
            subprocess.run(["docker", "image", "rm", image["operator_image_tag"]], capture_output=True, timeout=60)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def bounded_json(path, cap=2 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > cap:
        raise ValueError("cohort JSON violates artifact bounds")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("cohort JSON must be an object")
    return value


def validate_cohort_inputs(args, directory):
    """Bind downloaded public inputs to this checkout and the frozen task suite."""
    from scripts.summarize_closure_suite import _plan
    plan = bounded_json(Path(directory) / "suite-plan.json")
    _plan(plan)  # Consume the same strict contract used by the final aggregator.
    if (plan.get("mode") != "transfer" or plan["model"]["name"] != "qwen3:4b-instruct"
            or plan["common"]["budgets"]["context_tokens"] != 24000):
        raise ValueError("downloaded plan is outside the fixed transfer cohort")
    implementation, tool_hash = implementation_identity()
    if (implementation != plan["common"]["implementation_sha256"]
            or tool_hash != plan["common"]["tools_sha256"]
            or git(ROOT, "rev-parse", "HEAD").stdout.decode().strip() != plan["source_commit"]):
        raise ValueError("target worker code differs from the pre-execution plan")
    tasks = suite_tasks()
    mapping = {task.instance_id: (task, raw) for task, raw in tasks.values()}
    image = plan["image"]
    if (not IMAGE_ID.fullmatch(str(image.get("inference_image_id", "")))
            or not IMAGE_ID.fullmatch(str(image.get("public_base_image_id", "")))
            or not re.fullmatch(r"python@sha256:[0-9a-f]{64}", str(image.get("public_base_digest", "")))):
        raise ValueError("cohort image identities are unresolved")
    for target in plan["targets"]:
        task, raw = mapping[target["instance_id"]]
        if (target["task_sha256"] != sha256(raw) or target["task_fingerprint"] != task.fingerprint()
                or target["repo"] != task.repo or target["base_commit"] != task.base_commit
                or target["inference_image_id"] != image["inference_image_id"]
                or target["public_base_image_id"] != image["public_base_image_id"]
                or target["public_checks_sha256"] != sha256(json.dumps(list(task.public_checks), ensure_ascii=False,
                                                                           separators=(",", ":")))):
            raise ValueError("target task differs from the frozen source suite")
    args.model = plan["model"]["name"]
    args.ollama_mode = "json"
    for key, value in plan["common"]["budgets"].items():
        if key != "temperature":
            setattr(args, key, value)
    return plan, mapping


def validate_seed_package(directory, plan):
    package = bounded_json(Path(directory) / "seed-package.json")
    if (package.get("schema") != 1 or package.get("kind") != "verified_closure_seed_package"
            or package.get("fixture") is not False
            or package.get("plan_sha256") != file_sha256(Path(directory) / "suite-plan.json")
            or type(package.get("eligible")) is not bool):
        raise ValueError("seed package differs from the fixed plan")
    if not package["eligible"]:
        return None
    source = load_public_json(directory, package["source_receipt"])
    guard = load_public_json(directory, package["guard"])
    origin, record = package["origin"], package["record"]
    source_ids = [row["instance_id"] for row in plan["source_attempts"]]
    if (source.get("delivery_success") is not True or source.get("infrastructure_failed") is not False
            or source.get("internal_checks_passed") is not True or source.get("external_checks_passed") is not True
            or source.get("status") != "submitted" or source.get("exit_status") != "Submitted"
            or source.get("model") != plan["model"] or source.get("common") != plan["common"]
            or source.get("instance_id") not in source_ids or source.get("role") != "source"
            or origin.get("task_id") != source["instance_id"] or record.get("task_id") != source["instance_id"]
            or guard.get("eligible") is not True or len(guard.get("functional_targets", [])) != 2
            or any(row.get("target_feature_still_missing") is not True for row in guard["functional_targets"])
            or set(row["instance_id"] for row in guard["functional_targets"]) != set(row["instance_id"] for row in plan["targets"])
            or sha256(record.get("body", "")) != origin.get("content_sha256")
            or record.get("verified") is not True or public_preview(record.get("body", ""))[1]):
        raise ValueError("source package lacks independent delivery or leakage evidence")
    # Read-only validation checks independent verifier evidence too, before any
    # source experience can enter a target database.
    from scripts.summarize_closure_suite import _receipt
    _receipt(source, (source["instance_id"], 42, source["arm"]),
             {**source["task"], "instance_id": source["instance_id"], "role": "source"},
             {"id": source["arm"], "profile": source["profile"], "knowledge": "none"}, plan, Path(directory))
    return package


def blocked_trial(plan, target, arm, seed, public, relative, reason):
    """Record a planned trial prevented before its first model request."""
    directory = Path(public) / relative
    directory.mkdir(parents=True, exist_ok=False)
    summary = {"schema": 1, "fixture": False, "kind": "real_docker_closure_trial", "attempt_started": False,
        "attempted_count": 0, "model_status": "infrastructure_failed", "delivery_success": None,
        "complete": False, "blocked_reason": reason, "instance_id": target["instance_id"]}
    atomic_json(directory / "summary.json", summary)
    task_fields = ("task_sha256", "task_fingerprint", "repo", "base_commit", "inference_image_id",
                   "public_base_image_id", "public_checks_sha256")
    receipt = {"schema": 1, "kind": "closure_trial_receipt", "fixture": False,
        "instance_id": target["instance_id"], "seed": seed, "arm": arm["id"], "role": target["role"],
        "model": plan["model"], "common": plan["common"], "profile": arm["profile"], "knowledge": arm["knowledge"],
        "task": {key: target[key] for key in task_fields}, "status": "infrastructure_failed", "exit_status": None,
        "submitted": False, "internal_checks_passed": None, "external_checks_passed": None, "delivery_success": None,
        "usage": {"complete": True, "input_tokens": 0, "output_tokens": 0, "calls_with_usage": 0, "unknown_requests": 0},
        "model_calls": 0, "seconds": None, "infrastructure_failed": True, "attempt_started": False, "attempted_count": 0,
        "knowledge_observed_reuse": False, "artifacts": {"summary": artifact_ref(directory / "summary.json", public),
                                                        "evidence": None, "verification": None}}
    atomic_json(directory / "trial-receipt.json", receipt)
    return receipt


def load_transferred_image(directory, plan):
    archive = Path(directory) / "tools-image.tar"
    receipt = bounded_json(Path(directory) / "image-transfer.json")
    if (archive.is_symlink() or not archive.is_file() or archive.stat().st_size > 2 * 1024 ** 3
            or receipt.get("schema") != 1 or receipt.get("kind") != "public_tools_image_transfer"
            or receipt.get("image_id") != plan["image"]["inference_image_id"]
            or receipt.get("archive_bytes") != archive.stat().st_size
            or receipt.get("archive_sha256") != file_sha256(archive)):
        raise ValueError("public tools image transfer differs from frozen source")
    subprocess.run(["docker", "image", "load", "--input", str(archive)], check=True, capture_output=True, timeout=300)
    if inspect_image(receipt["image_id"])["Id"] != receipt["image_id"]:
        raise ValueError("loaded Docker image differs from the source tools image")
    subprocess.run(["docker", "pull", plan["image"]["public_base_digest"]], check=True, capture_output=True, timeout=600)
    if inspect_image(plan["image"]["public_base_digest"])["Id"] != plan["image"]["public_base_image_id"]:
        raise ValueError("target independent verifier base differs from source")


def run_cohort_target(args):
    inputs = Path(args.inputs).resolve()
    plan, tasks = validate_cohort_inputs(args, inputs)
    targets = [row for row in plan["targets"] if row["instance_id"] == args.instance_id]
    if (len(targets) != 1 or args.seed not in plan["seeds"] or args.arm not in targets[0]["arms"]):
        raise ValueError("trial is outside the predeclared target cohort")
    target = targets[0]
    arm = next(row for row in plan["arms"] if row["id"] == args.arm)
    private, public = Path(args.output).resolve(), Path(args.evidence).resolve()
    if (private == public or private.is_relative_to(public) or public.is_relative_to(private)
            or inputs == private or inputs.is_relative_to(private) or private.is_relative_to(inputs)):
        raise ValueError("target input, private and public roots must remain separate")
    private.mkdir(parents=True, exist_ok=False)
    public.mkdir(parents=True, exist_ok=False)
    relative = "target-" + args.instance_id + "-" + str(args.seed) + "-" + args.arm
    receipt = None
    try:
        package = validate_seed_package(inputs, plan) if arm["knowledge"] == "verified_source" else None
        if arm["knowledge"] == "verified_source" and package is None:
            receipt = blocked_trial(plan, target, arm, args.seed, public, relative, "verified_source_unavailable")
        else:
            load_transferred_image(inputs, plan)
            with OwnedOllama(args.ollama, private, model=args.model, context_tokens=args.context_tokens) as daemon:
                task, raw = tasks[args.instance_id]
                receipt, _, _ = execute_cohort_trial(args, plan, task, raw, plan["image"], daemon,
                    private, public, relative, arm=arm, role=target["role"], origin=package)
    except Exception as exc:
        if (public / relative).exists():
            raise  # Preserve an interrupted actual attempt as a missing receipt, never claim zero model calls.
        receipt = blocked_trial(plan, target, arm, args.seed, public, relative, type(exc).__name__)
    finally:
        if receipt is not None:
            atomic_json(public / ("observations-" + relative + ".json"), {"schema": 1, "kind": "closure_cohort_observations",
                "plan_sha256": file_sha256(inputs / "suite-plan.json"), "source_attempts": [],
                "runs": [{"instance_id": args.instance_id, "seed": args.seed, "arm": args.arm,
                          "receipt": artifact_ref(public / relative / "trial-receipt.json", public)}]})
    return {"mode": "target", "complete": receipt["infrastructure_failed"] is False,
            "model_status": receipt["status"], "delivery_success": receipt["delivery_success"]}


def aggregate_cohort(args):
    from scripts.summarize_closure_suite import summarize
    public = Path(args.evidence).resolve()
    plan_hash = file_sha256(public / "suite-plan.json")
    combined = {"schema": 1, "kind": "closure_cohort_observations", "plan_sha256": plan_hash,
                "runs": [], "source_attempts": []}
    files = sorted(public.glob("observations-*.json"))
    if len(files) > 20:
        raise ValueError("cohort observations exceed the fixed trial bound")
    for path in files:
        part = bounded_json(path)
        if (part.get("schema") != 1 or part.get("kind") != combined["kind"] or part.get("plan_sha256") != plan_hash):
            raise ValueError("cohort observations differ from the frozen plan")
        combined["runs"].extend(part["runs"])
        combined["source_attempts"].extend(part["source_attempts"])
    atomic_json(public / "observations.json", combined)
    result = summarize(public / "suite-plan.json", public / "observations.json", public / "cohort-summary.json")
    return {"mode": "aggregate", "complete": result["complete"], "delivery_success": None,
            "expected_target_trials": result["expected_target_attempts"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama")
    parser.add_argument("--task", default="examples/delivery-suite-v1/requests-2931-public.json")
    parser.add_argument("--mode", choices=["proof", "transfer", "target", "aggregate"], default="proof")
    parser.add_argument("--inputs", default="runs/closure-seed-inputs")
    parser.add_argument("--instance-id")
    parser.add_argument("--arm", choices=["baseline", "retrieval", "verified-source"])
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
    if args.mode != "aggregate" and not args.ollama:
        parser.error("real inference requires the fixed local Ollama executable")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", args.label) or not 1 <= args.max_steps <= 100
            or not 4096 <= args.context_tokens <= 32768 or not 128 <= args.max_output_tokens <= 4000
            or args.max_output_tokens + 1000 >= args.context_tokens or not 60 <= args.wall_seconds <= 3600
            or not 10 <= args.request_timeout <= 600 or not 1 <= args.command_timeout <= 300
            or not math.isfinite(args.cost_limit) or args.cost_limit <= 0):
        parser.error("invalid bounded delivery trial parameters")
    try:
        result = {"proof": run_proof, "transfer": run_cohort_seed, "target": run_cohort_target,
                  "aggregate": aggregate_cohort}[args.mode](args)
    except Exception as exc:
        print("Delivery closure infrastructure failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps({key: result.get(key) for key in ("mode", "model_status", "exit_status", "complete", "delivery_success")}))
    return 0 if (result["delivery_success"] is True if args.mode == "proof" else result["complete"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
