"""Direct Ollama provider implementing the pinned mini-swe-agent Model protocol."""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
import urllib.request
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit


@dataclass
class OllamaConfig:
    model_name: str
    model_kwargs: dict
    cost_tracking: str = "ignore_errors"

    def model_dump(self, **kwargs):
        return copy.deepcopy(asdict(self))


class OllamaModel:
    """Real HTTP inference, no automatic retries and no fabricated dollar cost.

    JSON mode supports local coding models without native tool-call training;
    tools mode uses actual Ollama function calls. Both produce the exact upstream
    action/observation protocol and retain the original provider response.
    """
    def __init__(self, *, model_name, model_kwargs=None, cost_tracking="ignore_errors", **kwargs):
        if kwargs:
            raise ValueError("Unsupported native Ollama configuration")
        settings = copy.deepcopy(model_kwargs or {})
        allowed = {"api_base", "timeout", "max_tokens", "seed", "temperature", "num_ctx", "mode", "think"}
        if set(settings) - allowed:
            raise ValueError("Unsupported Ollama options")
        base = settings.get("api_base", "http://127.0.0.1:11434").rstrip("/")
        endpoint = urlsplit(base)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("Ollama endpoint must not contain credentials")
        mode = settings.get("mode", "json")
        if mode not in {"json", "tools"}:
            raise ValueError("Ollama mode must be json or tools")
        for key in ("timeout", "max_tokens", "num_ctx"):
            value = settings.get(key, {"timeout": 180, "max_tokens": 1200, "num_ctx": 8192}[key])
            if type(value) is not int or value <= 0:
                raise ValueError("Ollama budgets must be positive integers")
        temperature = settings.get("temperature", 0)
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(temperature):
            raise ValueError("Temperature must be finite")
        self.config = OllamaConfig(model_name, settings, cost_tracking)
        self.base, self.mode = base, mode
        self.short_name = model_name.split("/", 1)[-1]
        self.identity = None

    def _http(self, path, value=None, timeout=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(value, ensure_ascii=False).encode() if value is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout or self.config.model_kwargs.get("timeout", 180)) as reply:
            raw = reply.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("Ollama response exceeded limit")
        return json.loads(raw)

    def resolve_identity(self):
        models = self._http("/api/tags", timeout=15).get("models", [])
        wanted = self.short_name if ":" in self.short_name else self.short_name + ":latest"
        matches = [item for item in models if item.get("name") in {self.short_name, wanted}]
        if not matches or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", matches[0].get("digest", "")):
            raise RuntimeError("Local model tag is not available")
        current = {"name": matches[0]["name"], "digest": matches[0]["digest"]}
        if self.identity is not None and self.identity != current:
            raise RuntimeError("Local model weights changed during the trial")
        self.identity = current
        return copy.deepcopy(self.identity)

    def query(self, messages, **kwargs):
        from minisweagent.models.utils.actions_toolcall import BASH_TOOL, parse_toolcall_actions
        from minisweagent.exceptions import FormatError
        from minisweagent.models import GLOBAL_MODEL_STATS
        self.resolve_identity()
        request_id = uuid.uuid4().hex
        prepared = []
        for message in messages:
            role, content = message.get("role"), message.get("content") or ""
            if self.mode == "json":
                if role == "tool":
                    prepared.append({"role": "user", "content": "Tool observation:\n" + content})
                elif role == "assistant" and message.get("tool_calls"):
                    commands = [json.loads(call["function"]["arguments"])["command"] for call in message["tool_calls"]]
                    prepared.append({"role": role, "content": json.dumps({"command": commands[0]})
                        if len(commands) == 1 else json.dumps({"commands": commands})})
                else:
                    prepared.append({"role": role, "content": content})
            else:
                item = {"role": role, "content": content}
                if message.get("tool_calls"):
                    item["tool_calls"] = [{"function": {"name": call["function"]["name"],
                        "arguments": json.loads(call["function"]["arguments"])}} for call in message["tool_calls"]]
                if role == "tool":
                    item["tool_name"] = "bash"
                prepared.append(item)
        options = self.config.model_kwargs
        payload = {"model": self.short_name, "messages": prepared, "stream": False,
                   "options": {"temperature": options.get("temperature", 0), "seed": options.get("seed", 42),
                               "num_predict": options.get("max_tokens", 1200), "num_ctx": options.get("num_ctx", 8192)}}
        if self.short_name.startswith("qwen3") or "think" in options:
            payload["think"] = options.get("think", False)
        if self.mode == "json":
            prepared.insert(0, {"role": "system", "content": "Return exactly one JSON object with a command string. "
                "Use the available command protocol to perform one action. No Markdown or text outside JSON."})
            payload["format"] = {"type": "object", "properties": {"command": {"type": "string"}},
                                 "required": ["command"], "additionalProperties": False}
        else:
            payload["tools"] = [BASH_TOOL]
        response = self._http("/api/chat", payload)
        if response.get("done") is not True or not isinstance(response.get("message"), dict):
            raise RuntimeError("Incomplete Ollama response")
        usage = {"prompt_tokens": response.get("prompt_eval_count"), "completion_tokens": response.get("eval_count")}
        usage = {key: value if type(value) is int and value >= 0 else None for key, value in usage.items()}
        extra = {"cost": 0.0, "cost_unknown": True, "cost_source": "unpriced_local",
                 "response": {"usage": usage, "ollama": response}, "request_id": request_id, "timestamp": time.time()}
        GLOBAL_MODEL_STATS.add(0.0)
        try:
            if self.mode == "json":
                value = json.loads(response["message"].get("content", ""))
                if not isinstance(value, dict) or set(value) != {"command"} or not isinstance(value["command"], str):
                    raise ValueError("Expected a command object")
                calls = [{"function": {"name": "bash", "arguments": value}}]
            else:
                calls = response["message"].get("tool_calls", [])
            canonical, objects = [], []
            for index, call in enumerate(calls):
                function = call["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                identifier = f"ollama_{request_id}_{index}"
                canonical.append({"id": identifier, "type": "function", "function": {
                    "name": function["name"], "arguments": arguments}})
                objects.append(SimpleNamespace(id=identifier, function=SimpleNamespace(name=function["name"], arguments=arguments)))
            actions = parse_toolcall_actions(objects, format_error_template="{{error}}")
        except FormatError as exc:
            exc.messages[0].setdefault("extra", {}).update(extra)
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise FormatError({"role": "user", "content": "Invalid command format; return a JSON command object.",
                               "extra": {**extra, "interrupt_type": "FormatError"}}) from exc
        return {"role": "assistant", "content": response["message"].get("content", "") if self.mode == "tools" else "",
                "tool_calls": canonical, "extra": {**extra, "actions": actions}}

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages
        return format_toolcall_observation_messages(actions=message.get("extra", {}).get("actions", []), outputs=outputs,
            observation_template="<returncode>{{output.returncode}}</returncode>\n{{output.output}}", template_vars=template_vars)

    def get_template_vars(self, **kwargs):
        return {**self.config.model_dump(), **kwargs}

    def serialize(self):
        return {"info": {"config": {"model": self.config.model_dump(), "model_type": "delivery_agent.ollama_model.OllamaModel"},
                         "provider": "ollama_native", "model_identity": self.identity, "cost_source": "unpriced_local"}}
