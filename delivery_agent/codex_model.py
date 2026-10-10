"""Codex CLI response adapter; command execution belongs to the outer environment.

Each response uses an empty temporary cwd with tools disabled. The selected
messages are the only repository/task material passed to Codex. CLI-generated
system instructions and schema tokens are additional provider context and are
not covered by the outer selected-message byte budget. No sampling seed or
output-token cap is claimed: the CLI does not expose those controls here.
The adapter does not retry CLI invocations. Built-in OpenAI transport retries
cannot be overridden in the verified CLI version; network request counts and
failed-attempt usage therefore remain unknown, not equal to invocation counts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace


MAX_STDOUT = 8 * 1024 * 1024
MAX_STDERR = 1024 * 1024
MAX_STDIN = 8 * 1024 * 1024
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "remote_plugin", "multi_agent", "hooks",
    "code_mode_host", "browser_use", "computer_use", "image_generation",
    "view_image", "goals", "memories", "skill_search", "workspace_dependencies",
    "unbounded_connection_retries",
)
COMMAND_SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}},
                  "required": ["command"], "additionalProperties": False}


class CodexProtocolError(RuntimeError):
    """The CLI did not complete one response-only, usage-bearing model turn."""


class _EventStream:
    """Fail closed on CLI tool activity, including started but unfinished tools."""
    def __init__(self):
        self.thread_started = False
        self.started = False
        self.completed = False
        self.answer = None
        self.usage = None
        self.event_types = []
        self.diagnostics = []

    def feed(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise CodexProtocolError("Codex emitted malformed JSONL") from exc
        if not isinstance(event, dict) or self.completed:
            raise CodexProtocolError("Unexpected Codex event ordering")
        kind = event.get("type")
        self.event_types.append(kind)
        if kind == "thread.started":
            if self.thread_started or self.started:
                raise CodexProtocolError("Codex emitted multiple threads")
            self.thread_started = True
        elif kind == "turn.started":
            if not self.thread_started or self.started:
                raise CodexProtocolError("Codex emitted multiple or unbound turns")
            self.started = True
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                raise CodexProtocolError("Codex item is missing")
            item_kind = item.get("type")
            # These two diagnostics occur in the verified CLI version when its
            # host tools are deliberately disabled. They are never task errors.
            if item_kind == "error" and not self.started and kind == "item.completed":
                message = item.get("message", "")
                if (isinstance(message, str) and message.startswith(
                        "Under-development features enabled: skip_host_skill_discovery.")
                        and "Under-development features are incomplete" in message):
                    self.diagnostics.append("skip_host_skill_discovery_unstable")
                    return
                if message == ("Code Mode is unavailable because code-mode host is disabled. "
                        "Code mode will fail closed; enable `features.code_mode_host` "
                        "and install `codex-code-mode-host`."):
                    self.diagnostics.append("code_mode_host_disabled")
                    return
            if not self.started or item_kind not in {"agent_message", "reasoning"}:
                raise CodexProtocolError("Codex attempted a forbidden tool or emitted an error item")
            if kind == "item.completed" and item_kind == "agent_message":
                if self.answer is not None or not isinstance(item.get("text"), str):
                    raise CodexProtocolError("Codex must return exactly one agent message")
                self.answer = item["text"]
        elif kind == "turn.completed":
            usage = event.get("usage")
            if not self.started or self.answer is None or not isinstance(usage, dict):
                raise CodexProtocolError("Codex turn has no answer or measured usage")
            for key in ("input_tokens", "output_tokens"):
                if type(usage.get(key)) is not int or usage[key] < 0:
                    raise CodexProtocolError("Codex token usage is missing or invalid")
            for key in ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"):
                if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                    raise CodexProtocolError("Codex detailed token usage is invalid")
            self.usage = {k: v for k, v in usage.items() if k in {
                "input_tokens", "output_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "reasoning_output_tokens"}}
            self.completed = True
        else:
            raise CodexProtocolError("Codex emitted an unsupported or failed event")

    def finish(self):
        if not self.completed:
            raise CodexProtocolError("Codex stream ended without a completed turn")
        return {"text": self.answer, "usage": self.usage,
                "event_types": self.event_types, "diagnostics": self.diagnostics}


def _kill_owned_process(process):
    """Terminate this subprocess group, never a process selected by its name."""
    if os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _run_bounded(argv, *, cwd, payload=b"", timeout=180, on_line=None):
    """Drain both pipes concurrently and stop on a byte cap or invalid event.

    No stderr content is copied into exceptions or evidence. Reader memory is
    capped even when a child emits an unterminated line or floods stderr.
    """
    if len(payload) > MAX_STDIN:
        raise ValueError("Codex selected context exceeds the stdin limit")
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **options)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    failures = []
    failed = threading.Event()

    def fail(exc):
        failures.append(exc)
        failed.set()

    def read(name, pipe, limit):
        pending = bytearray()
        try:
            while chunk := pipe.read1(8192):
                if len(buffers[name]) + len(chunk) > limit:
                    raise CodexProtocolError("Codex " + name + " exceeded its byte limit")
                buffers[name].extend(chunk)
                if name == "stdout" and on_line is not None:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remaining = pending.partition(b"\n")
                        pending = bytearray(remaining)
                        if line.strip():
                            on_line(line)
            if name == "stdout" and pending.strip() and on_line is not None:
                on_line(pending)
        except Exception as exc:
            fail(exc)
        finally:
            pipe.close()

    def write():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            # Exit status / missing turn will give a safe, bounded diagnostic.
            pass
        finally:
            process.stdin.close()

    workers = [threading.Thread(target=read, args=("stdout", process.stdout, MAX_STDOUT), daemon=True),
               threading.Thread(target=read, args=("stderr", process.stderr, MAX_STDERR), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None or any(worker.is_alive() for worker in workers):
            if failed.wait(min(0.02, max(0, deadline - time.monotonic()))):
                raise failures[0]
            if time.monotonic() >= deadline:
                raise TimeoutError("Codex response deadline exceeded; CLI invocation was not retried")
        if failures:
            raise failures[0]
        if process.returncode:
            raise CodexProtocolError("Codex process failed with exit code " + str(process.returncode))
    except BaseException:
        _kill_owned_process(process)
        raise
    finally:
        for worker in workers:
            worker.join(timeout=1)
    return bytes(buffers["stdout"]), {"stdout_bytes": len(buffers["stdout"]),
        "stderr_bytes": len(buffers["stderr"]),
        "stderr_sha256": hashlib.sha256(buffers["stderr"]).hexdigest()}


@dataclass
class CodexConfig:
    model_name: str
    model_kwargs: dict
    cost_tracking: str = "ignore_errors"

    def model_dump(self, **kwargs):
        return copy.deepcopy(asdict(self))


class CodexModel:
    def __init__(self, *, model_name, model_kwargs=None, cost_tracking="ignore_errors", **kwargs):
        settings = copy.deepcopy(model_kwargs or {})
        if kwargs or set(settings) - {"binary", "timeout", "reasoning_effort"}:
            raise ValueError("Unsupported Codex options; sampling seed and output token limits are not supported")
        if not isinstance(model_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", model_name):
            raise ValueError("Codex requires an explicit model name")
        timeout = settings.get("timeout", 180)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("Codex timeout must be finite and in (0, 3600]")
        if settings.get("reasoning_effort", "low") not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Unsupported Codex reasoning effort")
        binary = settings.get("binary") or shutil.which("codex")
        if not isinstance(binary, str) or not binary or not Path(binary).is_file():
            raise ValueError("Codex native executable was not found; configure binary explicitly")
        self.binary = str(Path(binary).resolve())
        if os.name == "nt" and Path(self.binary).suffix.lower() != ".exe":
            raise ValueError("Use a native Codex .exe, not a shell wrapper")
        self.config = CodexConfig(model_name, settings, cost_tracking)
        self.identity = None

    def resolve_identity(self):
        with open(self.binary, "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if self.identity is None:
            with tempfile.TemporaryDirectory(prefix="delivery-codex-identity-") as folder:
                stdout, _ = _run_bounded([self.binary, "--version"], cwd=folder, timeout=15)
            version = stdout.decode("utf-8", "strict").strip()
            if not re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
                raise CodexProtocolError("Could not identify the Codex CLI version")
            self.identity = {"kind": "codex_cli", "model": self.config.model_name,
                "cli_version": version, "binary_sha256": digest, "weight_digest_available": False,
                "reasoning_effort": self.config.model_kwargs.get("reasoning_effort", "low")}
        elif self.identity["binary_sha256"] != digest:
            raise CodexProtocolError("Codex executable changed during the trial")
        return copy.deepcopy(self.identity)

    def _argv(self, schema):
        argv = [self.binary, "--ask-for-approval", "never", "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--model", self.config.model_name,
            "--config", 'model_provider="openai"', "--config", 'forced_login_method="chatgpt"',
            "--config", 'web_search="disabled"', "--config", "project_doc_max_bytes=0",
            "--config", "mcp_servers={}", "--config", "suppress_unstable_features_warning=true",
            "--config", "model_reasoning_effort=" + json.dumps(self.config.model_kwargs.get("reasoning_effort", "low")),
            "--enable", "skip_host_skill_discovery", "--output-schema", str(schema), "-"]
        for feature in DISABLED_FEATURES:
            argv.extend(["--disable", feature])
        return argv

    def query(self, messages, **kwargs):
        from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions
        from minisweagent.exceptions import FormatError
        from minisweagent.models import GLOBAL_MODEL_STATS
        if kwargs:
            raise ValueError("Codex query overrides are not supported")
        identity = self.resolve_identity()
        selected = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("Invalid selected Codex message")
            item = {"role": message["role"], "content": message.get("content") or ""}
            if not isinstance(item["content"], str):
                raise ValueError("Codex accepts text messages only")
            for key in ("tool_calls", "tool_call_id"):
                if key in message:
                    item[key] = copy.deepcopy(message[key])
            selected.append(item)
        prompt = ("Act only as a response model for an external repository agent. Do not execute any command, "
            "inspect local files, or call native tools. The messages below are the entire selected conversation. "
            "Return exactly one JSON object with a command string representing the next action for the external "
            "agent's documented command protocol. The external environment will execute it and return its observation.\n"
            + json.dumps({"messages": selected}, ensure_ascii=False))
        stream = _EventStream()
        with tempfile.TemporaryDirectory(prefix="delivery-codex-response-") as folder:
            schema = Path(folder) / "command-schema.json"
            schema.write_text(json.dumps(COMMAND_SCHEMA), encoding="utf-8")
            _, diagnostics = _run_bounded(self._argv(schema), cwd=folder, payload=prompt.encode("utf-8"),
                timeout=self.config.model_kwargs.get("timeout", 180), on_line=stream.feed)
        response = stream.finish()
        request_id = uuid.uuid4().hex
        measured = response["usage"]
        extra = {"cost": 0.0, "reported_cost": None, "cost_unknown": True, "cost_source": "chatgpt_plan_unpriced",
            "response": {"usage": {"prompt_tokens": measured["input_tokens"], "completion_tokens": measured["output_tokens"]},
                "codex_cli": {**response, **diagnostics, "model_identity": identity,
                    "request_count_unit": "cli_invocations", "underlying_transport_request_count": None,
                    "internal_transport_retries_controlled": False,
                    "native_prompt_overhead_outside_selected_context_budget": True}},
            "request_id": request_id, "timestamp": time.time()}
        GLOBAL_MODEL_STATS.add(0.0)
        try:
            value = json.loads(response["text"])
            if not isinstance(value, dict) or set(value) != {"command"} or not isinstance(value["command"], str) or not value["command"].strip():
                raise ValueError("Expected one nonempty command")
            arguments = json.dumps(value, ensure_ascii=False)
            identifier = "codex_" + request_id
            action = SimpleNamespace(id=identifier, function=SimpleNamespace(name="bash", arguments=arguments))
            actions = parse_toolcall_actions([action], format_error_template="{{error}}")
        except FormatError as exc:
            exc.messages[0].setdefault("extra", {}).update(extra)
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise FormatError({"role": "user", "content": "Invalid command format; return a JSON command object.",
                "extra": {**extra, "interrupt_type": "FormatError"}}) from exc
        return {"role": "assistant", "content": "", "tool_calls": [{"id": identifier, "type": "function",
            "function": {"name": "bash", "arguments": arguments}}], "extra": {**extra, "actions": actions}}

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages
        return format_toolcall_observation_messages(actions=message.get("extra", {}).get("actions", []), outputs=outputs,
            observation_template="<returncode>{{output.returncode}}</returncode>\n{{output.output}}", template_vars=template_vars)

    def get_template_vars(self, **kwargs):
        return {**self.config.model_dump(), **kwargs}

    def serialize(self):
        return {"info": {"config": {"model": self.config.model_dump(), "model_type": "delivery_agent.codex_model.CodexModel"},
            "provider": "codex_cli", "model_identity": copy.deepcopy(self.identity), "model_digest": None,
            "cost_source": "chatgpt_plan_unpriced", "request_count_unit": "cli_invocations",
            "underlying_transport_request_count": None,
            "internal_transport_retries_controlled": False,
            "native_prompt_overhead_outside_selected_context_budget": True}}
