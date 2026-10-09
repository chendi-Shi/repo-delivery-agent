"""Pinned mini-swe-agent integration; selection changes views, never evidence.

Use ``run_agent(model, env, task=..., agent_config=..., output_dir=...)`` with
objects created by upstream get_model/get_sb_environment. mini-swe-agent is an
optional import so history and context policy tests run without model packages.
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .history import HistoryEnvironment, HistoryStore

UPSTREAM_VERSION = "2.4.6"
UPSTREAM_COMMIT = "04d809ceab9df28f9adaed044884180159172930"

HISTORY_GUIDE = """
History toolbox (interpreted by the agent harness, available through bash calls):
delivery-history search 'literal query' [limit 1..8]
delivery-history read e000001 [character-offset] [limit 1..4000]
delivery-history note short_name 'fact, failed attempt, or constraint with source IDs'
Keep at most 8 notes of 500 characters each. They are your notes, not verified facts.
Tool output views may be shortened; read its source ID to recover complete evidence.
Use one history command per bash call. Do not combine it with shell operators.
"""


class ContextBudgetExceeded(RuntimeError):
    """Mandatory protocol messages cannot fit; no model request was issued."""


class InvalidToolHistory(ValueError):
    """A context view would contain incomplete or orphaned tool results."""


@dataclass(frozen=True)
class ContextConfig:
    profile: str = "retrieval"
    context_tokens: int = 12000
    reserve_tokens: int = 1000
    recent_groups: int = 3
    max_tool_chars: int = 4000

    def __post_init__(self) -> None:
        if self.profile not in {"baseline", "recent", "retrieval"}:
            raise ValueError("profile must be baseline, recent, or retrieval")
        if self.context_tokens <= self.reserve_tokens or self.reserve_tokens < 0:
            raise ValueError("context_tokens must exceed a nonnegative reserve_tokens")
        if self.recent_groups < 1 or self.max_tool_chars < 100:
            raise ValueError("recent_groups must be positive and max_tool_chars at least 100")


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative UTF-8 byte budget, NOT measured provider token usage.

    Each byte is budgeted as one token plus a message framing allowance. This is
    deliberately conservative for byte tokenizers. Tool schema and completion
    space are supplied separately via reserve_tokens; no model-specific tokenizer
    guarantee is claimed. Reports label this value as an estimate.
    """
    return sum(len(json.dumps({key: value for key, value in message.items() if key != "extra"},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 32
               for message in messages)


def _source(message: dict[str, Any], index: int) -> str:
    return (message.get("extra") or {}).get("delivery_source_id", HistoryStore.source_id(index + 1))


def message_groups(messages: list[dict[str, Any]]) -> tuple[list[int], list[list[int]]]:
    """Protect all systems/the first user; make assistant tool loops indivisible."""
    mandatory = [index for index, message in enumerate(messages) if message.get("role") == "system"]
    first_user = next((index for index, message in enumerate(messages) if message.get("role") == "user"), None)
    if first_user is not None:
        mandatory.append(first_user)
    mandatory = sorted(set(mandatory))
    groups: list[list[int]] = []
    current: list[int] = []
    for index, message in enumerate(messages):
        if index in mandatory:
            if current:
                groups.append(current)
                current = []
            continue
        if message.get("role") == "assistant" and current:
            groups.append(current)
            current = []
        current.append(index)
    if current:
        groups.append(current)
    # Validate the provider-facing native-tool protocol before any selection.
    for group in groups:
        expected: list[str] = []
        observed: list[str] = []
        for index in group:
            message = messages[index]
            if message.get("role") == "assistant":
                calls = message.get("tool_calls") or []
                expected.extend(call.get("id", "") for call in calls)
            elif message.get("role") == "tool":
                observed.append(message.get("tool_call_id", ""))
        if expected or observed:
            if not all(expected) or len(expected) != len(set(expected)) or sorted(expected) != sorted(observed):
                raise InvalidToolHistory("assistant tool calls and results must form a complete, unique group")
    return mandatory, groups


class ContextPolicy:
    def __init__(self, config: ContextConfig):
        self.config = config

    def select(self, messages: list[dict[str, Any]], notes: list[dict[str, str]] | None = None
               ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        mandatory, groups = message_groups(messages)
        prepared = copy.deepcopy(messages)
        shortened: list[str] = []
        if self.config.profile != "baseline":
            for index, message in enumerate(prepared):
                content = message.get("content")
                if message.get("role") == "tool" and isinstance(content, str) and len(content) > self.config.max_tool_chars:
                    source = _source(message, index)
                    half = self.config.max_tool_chars // 2
                    message["content"] = (content[:half] + f"\n[view shortened; full source {source}; "
                                           "use delivery-history read]\n" + content[-half:])
                    shortened.append(source)
        note_message = None
        if notes:
            note_message = {"role": "user", "content": "Agent-maintained notes (unverified):\n" +
                            json.dumps(notes, ensure_ascii=False)}
        cap = self.config.context_tokens - self.config.reserve_tokens

        def view(indices: set[int]) -> list[dict[str, Any]]:
            result = [prepared[index] for index in sorted(indices)]
            if note_message:
                # Insert immediately after the original user task. Additional
                # system messages elsewhere cannot shift this into a tool loop.
                original_user = next((index for index in sorted(indices)
                                      if prepared[index].get("role") == "user"), None)
                insert = sorted(indices).index(original_user) + 1 if original_user is not None else 0
                result.insert(insert, note_message)
            return result

        if self.config.profile == "baseline":
            chosen = set(range(len(messages)))
            if estimate_tokens(view(chosen)) > cap:
                raise ContextBudgetExceeded("full baseline history exceeds the configured byte budget")
        else:
            chosen = set(mandatory)
            # The latest complete group is mandatory; additional recent groups
            # are best-effort, so a large earlier result cannot evict current work.
            if groups:
                chosen.update(groups[-1])
            if estimate_tokens(view(chosen)) > cap:
                raise ContextBudgetExceeded("system, original task, notes and newest complete group exceed context budget")
            for group in reversed(groups[-self.config.recent_groups:-1]):
                candidate = chosen | set(group)
                if estimate_tokens(view(candidate)) <= cap:
                    chosen = candidate
            if self.config.profile == "retrieval":
                query_text = " ".join(str(messages[index].get("content") or "")
                                      for index in mandatory + (groups[-1] if groups else []))
                query_text += " " + json.dumps(notes or [], ensure_ascii=False)
                terms = set(re.findall(r"[\w./-]{3,}", query_text.lower()[-16000:]))
                ranked = []
                for group in groups:
                    if set(group) <= chosen:
                        continue
                    text = " ".join(str(messages[index].get("content") or "") for index in group).lower()
                    words = set(re.findall(r"[\w./-]{3,}", text))
                    score = len(terms & words) / math.sqrt(max(1, len(words)))
                    failure = any((messages[index].get("extra") or {}).get("returncode") not in (None, 0)
                                  for index in group)
                    ranked.append((score + (0.25 if failure else 0.0), group[-1], group))
                for score, _, group in sorted(ranked, reverse=True):
                    if score <= 0:
                        continue
                    candidate = chosen | set(group)
                    if estimate_tokens(view(candidate)) <= cap:
                        chosen = candidate
        selected_view = view(chosen)
        return selected_view, {
            "profile": self.config.profile, "selected_source_ids": [_source(messages[index], index) for index in sorted(chosen)],
            "excluded_source_ids": [_source(message, index) for index, message in enumerate(messages) if index not in chosen],
            "shortened_source_ids": [source for source in shortened if source in {_source(messages[index], index) for index in chosen}],
            "estimated_input_tokens": estimate_tokens(selected_view), "estimate_method": "utf8_bytes_plus_message_framing",
            "context_tokens": self.config.context_tokens, "reserve_tokens": self.config.reserve_tokens,
            "notes": notes or [],
        }


class ContextModel:
    def __init__(self, model: Any, history: HistoryStore, config: ContextConfig, output_dir: Path):
        self.model = model
        self.config = getattr(model, "config", None)
        self.history = history
        self.policy = ContextPolicy(config)
        self.output_dir = output_dir
        self.n_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls_with_usage = 0
        self.unknown_requests = 0
        self.recovery = None

    def _log(self, event: dict[str, Any]) -> None:
        with (self.output_dir / "request-views.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        view, metadata = self.policy.select(messages, self.history.notes())
        self.n_calls += 1
        if self.recovery is not None:
            self.recovery.begin_model({"messages": view, "kwargs": kwargs})
        self._log({"event": "request", "call": self.n_calls, **metadata,
                   "messages": [{key: value for key, value in message.items() if key != "extra"} for message in view]})
        started = time.monotonic()
        try:
            response = self.model.query(view, **kwargs)
        except BaseException as exc:
            if self.recovery is not None:
                from .recovery import _interrupt
                interruption = _interrupt(exc)
                if interruption is not None:
                    self.recovery.complete_model({"interrupt": interruption})
            self._log({"event": "error", "call": self.n_calls, "exception_type": type(exc).__name__,
                       "elapsed_seconds": time.monotonic() - started})
            raise
        extra = response.get("extra") or {}
        raw_response = extra.get("response") or {}
        usage = raw_response.get("usage") if isinstance(raw_response, dict) else None
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            if isinstance(prompt, int) and isinstance(completion, int):
                self.input_tokens += prompt
                self.output_tokens += completion
                self.calls_with_usage += 1
        if self.recovery is not None:
            self.recovery.complete_model({"response": response})
        self._log({"event": "response", "call": self.n_calls, "usage": usage,
                   "cost": extra.get("cost"), "elapsed_seconds": time.monotonic() - started})
        return response

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return self.model.format_message(**kwargs)

    def format_observation_messages(self, message: dict[str, Any], outputs: list[dict[str, Any]],
                                    template_vars: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.model.format_observation_messages(message, outputs, template_vars)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return self.model.get_template_vars(**kwargs)

    def serialize(self) -> dict[str, Any]:
        result = copy.deepcopy(self.model.serialize())
        result.setdefault("info", {})["delivery"] = {
            "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
            "context": asdict(self.policy.config), "model_calls": self.n_calls,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                      "calls_with_usage": self.calls_with_usage, "unknown_requests": self.unknown_requests,
                      "complete": self.calls_with_usage == self.n_calls and self.unknown_requests == 0},
            "artifacts": {"history": "history.sqlite", "requests": "request-views.jsonl",
                          "trajectory": "trajectory.json"},
        }
        return result


def require_upstream() -> Any:
    try:
        installed = importlib.metadata.version("mini-swe-agent")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("install the pinned integration: pip install 'mini-swe-agent==2.4.6'") from exc
    if installed != UPSTREAM_VERSION:
        raise RuntimeError(f"mini-swe-agent {UPSTREAM_VERSION} required; found {installed}")
    from minisweagent.agents.default import DefaultAgent
    return DefaultAgent


def make_agent_class(base_class: Any = None) -> Any:
    """Build the actual DefaultAgent subclass lazily; base injection is for tests."""
    base = require_upstream() if base_class is None else base_class

    class DeliveryAgent(base):
        def __init__(self, model: Any, env: Any, *, output_dir: Path | str,
                     context_config: ContextConfig | None = None, recovery_binding: dict[str, Any] | None = None,
                     resume: bool = False, recovery_policy: str = "stop", toolbox: Any = None, **kwargs: Any):
            self.delivery_output = Path(output_dir)
            self.delivery_output.mkdir(parents=True, exist_ok=True)
            if not resume and any((self.delivery_output / name).exists() for name in
                   ("history.sqlite", "request-views.jsonl", "trajectory.json")):
                raise ValueError("output directory already contains agent artifacts; use a fresh trial directory")
            self.delivery_history = HistoryStore(self.delivery_output / "history.sqlite")
            wrapped_model = ContextModel(model, self.delivery_history, context_config or ContextConfig(), self.delivery_output)
            wrapped_env = HistoryEnvironment(toolbox or env, self.delivery_history)
            from .checkpoint import CheckpointStore, fingerprint
            from .recovery import RecoveryController, RecoveryEnvironment
            recovery_env = RecoveryEnvironment(wrapped_env)
            self.delivery_recovery = None
            self.delivery_resume = resume
            self.delivery_recovery_binding = recovery_binding or {}
            kwargs["output_path"] = self.delivery_output / "trajectory.json"
            kwargs["instance_template"] = kwargs.get("instance_template", "{{task}}") + HISTORY_GUIDE
            if toolbox is not None:
                kwargs["instance_template"] += toolbox.guide()
            try:
                super().__init__(wrapped_model, recovery_env, **kwargs)
                # Keep the optional protocol-double path usable for policy
                # tests; genuine DefaultAgent provides all these counters.
                if hasattr(self, "n_calls") and hasattr(self, "cost"):
                    import hashlib
                    code = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(Path(__file__).parent.glob("*.py"))}
                    binding = {"runtime": self.delivery_recovery_binding,
                               "agent_config": fingerprint({key: value for key, value in kwargs.items()
                                                            if key != "output_path"}),
                               "context_config": fingerprint(asdict(wrapped_model.policy.config)),
                               "model_config": fingerprint(model.serialize().get("info", {}).get("config", {}).get("model", {})),
                               "upstream_version": UPSTREAM_VERSION,
                               "code_hash": fingerprint(code)}
                    store = CheckpointStore(self.delivery_output / "checkpoint.json", binding, resume=resume)
                    try:
                        self.delivery_recovery = RecoveryController(self, store, resume=resume, policy=recovery_policy)
                    except BaseException:
                        store.close()
                        raise
                    wrapped_model.recovery = self.delivery_recovery
                    recovery_env.controller = self.delivery_recovery
                else:
                    self.env = wrapped_env
            except BaseException:
                self.delivery_history.close()
                raise

        def add_messages(self, *messages: dict[str, Any]) -> list[dict[str, Any]]:
            # Upstream stores these same dictionaries in the complete trajectory.
            # Index before adding the ID to the message: evidence stays the raw
            # content, and IDs remain stable across every selected request view.
            for message in messages:
                source = self.delivery_history.append(message)
                message.setdefault("extra", {})["delivery_source_id"] = source
            # A successfully formatted action observation completes a clean
            # step. Persist its format-error reset with the observations, not
            # only later in DefaultAgent.run's following statement.
            if self.delivery_recovery is not None and self.delivery_recovery.in_actions:
                self.n_consecutive_format_errors = 0
            result = super().add_messages(*messages)
            if self.delivery_recovery is not None:
                clear = self.delivery_recovery.messages_complete_transition(messages)
                self.delivery_recovery.commit(clear=clear)
            return result

        def query(self) -> dict[str, Any]:
            # DefaultAgent increments its API counter before model.query(). A
            # policy rejection issues no request, so keep that counter truthful.
            previous_calls = self.n_calls
            try:
                return super().query()
            except (ContextBudgetExceeded, InvalidToolHistory):
                self.n_calls = previous_calls
                if self.delivery_recovery is not None:
                    self.delivery_recovery.commit()
                raise

        def execute_actions(self, message: dict[str, Any]) -> list[dict[str, Any]]:
            if self.delivery_recovery is None:
                return super().execute_actions(message)
            self.delivery_recovery.begin_actions(message)
            try:
                return super().execute_actions(message)
            finally:
                self.delivery_recovery.in_actions = False

        def run(self, task: str = "", **kwargs: Any) -> dict[str, Any]:
            if self.delivery_recovery is None:
                return super().run(task, **kwargs)
            # The first user message includes templates/toolbox guidance, so
            # bind the actual supplied task separately from message contents.
            from .checkpoint import CheckpointMismatch, fingerprint
            from .recovery import raise_recorded
            from minisweagent.exceptions import FormatError, InterruptAgentFlow
            recovery = self.delivery_recovery
            task_hash = fingerprint({"task": task, "vars": kwargs})
            if self.delivery_resume:
                if recovery.store.data["state"].get("task_hash") != task_hash:
                    raise CheckpointMismatch("resume task or template variables differ")
                recovery.ensure_unambiguous()
                if self.messages and self.messages[-1].get("role") == "exit":
                    return self.messages[-1].get("extra", {})
            else:
                recovery.task_hash = task_hash
                return super().run(task, **kwargs)
            recovery.task_hash = task_hash
            first = True
            while True:
                try:
                    transition = recovery.transition
                    if first and transition and transition["kind"] == "model" and transition.get("result"):
                        result = transition["result"]
                        if "interrupt" in result:
                            raise_recorded(result["interrupt"])
                        message = result["response"]
                        self.cost += message.get("extra", {}).get("cost", 0.0)
                        self.add_messages(message)
                        self.execute_actions(message)
                    elif first and self.messages and self.messages[-1].get("role") == "assistant":
                        self.execute_actions(self.messages[-1])
                    else:
                        self.step()
                    self.n_consecutive_format_errors = 0
                except FormatError as exc:
                    self.cost += exc.messages[0].get("extra", {}).get("cost", 0.0)
                    self.n_consecutive_format_errors += 1
                    if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                        self.add_messages(*exc.messages, {"role": "exit", "content": "RepeatedFormatError",
                                                          "extra": {"exit_status": "RepeatedFormatError", "submission": ""}})
                    else:
                        self.add_messages(*exc.messages)
                except InterruptAgentFlow as exc:
                    self.add_messages(*exc.messages)
                except Exception as exc:
                    self.handle_uncaught_exception(exc)
                    raise
                finally:
                    first = False
                    self.save(self.config.output_path)
                if self.messages[-1].get("role") == "exit":
                    break
            return self.messages[-1].get("extra", {})

        def save(self, path: Any, *extra_dicts: dict[str, Any]) -> dict[str, Any]:
            result = super().save(path, *extra_dicts)
            if self.delivery_recovery is not None:
                self.delivery_recovery.commit()
            return result

        def close(self) -> None:
            try:
                self.delivery_history.close()
            finally:
                if self.delivery_recovery is not None:
                    self.delivery_recovery.store.close()

    DeliveryAgent.__module__ = __name__
    return DeliveryAgent


def run_agent(model: Any, env: Any, *, task: str, agent_config: dict[str, Any], output_dir: Path | str,
              profile: str = "retrieval", context_tokens: int = 12000, reserve_tokens: int = 1000,
              recent_groups: int = 3, max_tool_chars: int = 4000, base_class: Any = None,
              recovery_binding: dict[str, Any] | None = None, resume: bool = False,
              recovery_policy: str = "stop", toolbox: Any = None) -> dict[str, Any]:
    agent = make_agent_class(base_class)(model, env, output_dir=output_dir,
                                       context_config=ContextConfig(profile, context_tokens, reserve_tokens,
                                                                    recent_groups, max_tool_chars),
                                       recovery_binding=recovery_binding, resume=resume, recovery_policy=recovery_policy,
                                       toolbox=toolbox,
                                       **copy.deepcopy(agent_config))
    try:
        result = agent.run(task)
        return {**result, "delivery": agent.model.serialize()["info"]["delivery"]}
    finally:
        try:
            agent.save(agent.delivery_output / "trajectory.json")
        finally:
            agent.close()
