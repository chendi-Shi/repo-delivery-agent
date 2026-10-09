"""Recover known results without repeating model requests or tool side effects.

An operation has three durable phases: pending, known result, and committed
messages. A crash while pending is ambiguous, even if the remote service may
have finished it. Only an explicit retry decision can cross that boundary.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointMismatch, CheckpointStore, fingerprint


class AmbiguousRecovery(RuntimeError):
    """The remote request/command outcome is unknown; no automatic replay."""


def _interrupt(value: BaseException) -> dict[str, Any] | None:
    from minisweagent.exceptions import InterruptAgentFlow
    if isinstance(value, InterruptAgentFlow):
        return {"exception": type(value).__name__, "messages": list(value.messages)}
    return None


def raise_recorded(value: dict[str, Any]) -> None:
    from minisweagent import exceptions
    permitted = {"InterruptAgentFlow", "Submitted", "LimitsExceeded", "TimeExceeded", "UserInterruption", "FormatError"}
    if value.get("exception") not in permitted:
        raise CheckpointMismatch("unsupported persisted agent-flow exception")
    raise getattr(exceptions, value["exception"])(*value["messages"])


class RecoveryController:
    def __init__(self, agent: Any, store: CheckpointStore, *, resume: bool, policy: str = "stop"):
        if policy not in {"stop", "retry"}:
            raise ValueError("recovery policy must be stop or explicit retry")
        self.agent, self.store = agent, store
        self.transition = copy.deepcopy(store.data.get("transition"))
        self.resuming = resume
        self.action_index = 0
        self.in_actions = False
        self.ambiguous = False
        self.task_hash = store.data.get("state", {}).get("task_hash")
        if resume:
            state = copy.deepcopy(store.data["state"])
            if self.transition and not self.transition.get("pending"):
                count = self.transition.get("message_count", len(state["messages"]))
                if len(state["messages"]) > count:
                    store.data.setdefault("uncommitted_diagnostics", []).extend(state["messages"][count:])
                    state["messages"] = state["messages"][:count]
            self.restore(state)
            if self.transition and self.transition.get("pending"):
                pending = copy.deepcopy(self.transition["pending"])
                if policy == "stop":
                    self.ambiguous = True
                else:
                    store.data["recovery_decisions"].append({
                        "time": time.time(), "policy": "retry", "acknowledged_pending": pending,
                        "risk": "may repeat billing or command side effects"})
                    if "message_count" in pending:
                        state = self.snapshot()
                        state["messages"] = state["messages"][:pending["message_count"]]
                        self.restore(state)
                    self.transition["pending"] = None
                    # The original attempted model call remains counted. Its
                    # cost and usage cannot be reconstructed from no response.
                    if pending["kind"] == "model":
                        agent.model.unknown_requests += 1
                        self.transition = None
                    self.commit(clear=False)

    def snapshot(self) -> dict[str, Any]:
        agent = self.agent
        return {"messages": copy.deepcopy(agent.messages), "n_calls": agent.n_calls,
                "cost": agent.cost, "n_consecutive_format_errors": agent.n_consecutive_format_errors,
                "start_time": agent._start_time, "extra_template_vars": copy.deepcopy(agent.extra_template_vars),
                "model_usage": {name: getattr(agent.model, name) for name in
                                ("n_calls", "input_tokens", "output_tokens", "calls_with_usage", "unknown_requests")},
                "notes": agent.delivery_history.notes(), "task_hash": self.task_hash}

    def restore(self, state: dict[str, Any]) -> None:
        required = {"messages", "n_calls", "cost", "n_consecutive_format_errors", "start_time",
                    "extra_template_vars", "model_usage", "notes"}
        if not required <= state.keys():
            raise CheckpointMismatch("checkpoint state is incomplete")
        agent = self.agent
        agent.messages = copy.deepcopy(state["messages"])
        for name in ("n_calls", "cost", "n_consecutive_format_errors", "_start_time", "extra_template_vars"):
            key = "start_time" if name == "_start_time" else name
            setattr(agent, name, copy.deepcopy(state[key]))
        for name, value in state["model_usage"].items():
            setattr(agent.model, name, value)
        # SQLite and the checkpoint are independently durable. A process can
        # die between their commits. Reconcile the trusted index to checkpoint
        # messages, preserving exact stable source IDs, rather than duplicating.
        db = agent.delivery_history.db
        with db:
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM notes")
            import json
            for index, message in enumerate(agent.messages, 1):
                source = (message.get("extra") or {}).get("delivery_source_id")
                if source != agent.delivery_history.source_id(index):
                    raise CheckpointMismatch("checkpoint history source IDs are inconsistent")
                raw = copy.deepcopy(message)
                raw.get("extra", {}).pop("delivery_source_id", None)
                db.execute("INSERT INTO events(id, message, evidence) VALUES (?, ?, ?)",
                           (index, json.dumps(raw, ensure_ascii=False), agent.delivery_history.evidence(raw)))
            for note in state["notes"]:
                db.execute("INSERT INTO notes(name,text) VALUES (?,?)", (note["name"], note["text"]))

    def ensure_unambiguous(self) -> None:
        if self.ambiguous:
            kind = self.transition["pending"]["kind"]
            raise AmbiguousRecovery(f"pending {kind} outcome is unknown; choose explicit retry or a fresh trial")

    def commit(self, *, clear: bool = False) -> None:
        if clear and self.transition and not self.transition.get("pending"):
            self.transition = None
        self.store.write(state=self.snapshot(), transition=self.transition)

    def messages_complete_transition(self, messages: tuple[dict[str, Any], ...]) -> bool:
        """A diagnostic exit is not the committed consumption of a result."""
        transition = self.transition
        if transition is None or transition.get("pending"):
            return False
        if transition["kind"] == "actions" and self.in_actions:
            return True
        result = transition.get("result") if transition["kind"] == "model" else None
        if transition["kind"] == "actions" and transition.get("completed"):
            result = transition["completed"][-1]
        if not result:
            return False
        if "response" in result:
            expected = [result["response"]]
        elif "interrupt" in result:
            expected = result["interrupt"]["messages"]
        else:
            return False
        def raw(value):
            value = copy.deepcopy(value)
            value.get("extra", {}).pop("delivery_source_id", None)
            return value
        return [raw(value) for value in messages[:len(expected)]] == [raw(value) for value in expected]

    def begin_model(self, request: dict[str, Any]) -> None:
        self.ensure_unambiguous()
        if self.transition is not None:
            raise CheckpointMismatch("model request started before prior transition was committed")
        self.transition = {"kind": "model", "pending": {"kind": "model", "request_hash": fingerprint(request),
                           "started_at": time.time(), "message_count": len(self.agent.messages)}, "result": None,
                           "message_count": len(self.agent.messages)}
        self.commit()

    def complete_model(self, result: dict[str, Any]) -> None:
        self.transition["pending"] = None
        self.transition["result"] = copy.deepcopy(result)
        self.commit()

    def begin_actions(self, message: dict[str, Any]) -> None:
        self.ensure_unambiguous()
        source = (message.get("extra") or {}).get("delivery_source_id")
        if self.transition is None:
            self.transition = {"kind": "actions", "assistant_source_id": source,
                               "completed": [], "pending": None, "message_count": len(self.agent.messages)}
            self.commit()
        if self.transition.get("kind") != "actions" or self.transition.get("assistant_source_id") != source:
            raise CheckpointMismatch("action transition does not match the assistant message")
        self.action_index, self.in_actions = 0, True

    def execute(self, environment: Any, action: dict[str, Any], cwd: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self.in_actions:
            raise CheckpointMismatch("tool execution outside an action transition")
        index = self.action_index
        self.action_index += 1
        request = {"action": action, "cwd": cwd, "kwargs": kwargs}
        completed = self.transition["completed"]
        if index < len(completed):
            result = completed[index]
            if result["request_hash"] != fingerprint(request):
                raise CheckpointMismatch("persisted tool result belongs to a different command")
        else:
            self.transition["pending"] = {"kind": "tool", "ordinal": index,
                                           "request_hash": fingerprint(request), "started_at": time.time(),
                                           "message_count": len(self.agent.messages)}
            self.commit()
            try:
                output = environment.execute(action, cwd=cwd, **kwargs)
                result = {"request_hash": fingerprint(request), "output": output}
            except BaseException as exc:
                interruption = _interrupt(exc)
                if interruption is None:
                    raise
                result = {"request_hash": fingerprint(request), "interrupt": interruption}
            completed.append(copy.deepcopy(result))
            self.transition["pending"] = None
            self.commit()
        if "interrupt" in result:
            raise_recorded(result["interrupt"])
        return copy.deepcopy(result["output"])


class RecoveryEnvironment:
    def __init__(self, environment: Any):
        self.environment = environment
        self.config = getattr(environment, "config", None)
        self.controller: RecoveryController | None = None

    def execute(self, action: dict[str, Any], cwd: str = "", **kwargs: Any) -> dict[str, Any]:
        return self.controller.execute(self.environment, action, cwd=cwd, **kwargs)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return self.environment.get_template_vars(**kwargs)

    def serialize(self) -> dict[str, Any]:
        return self.environment.serialize()

    def cleanup(self) -> None:
        cleanup = getattr(self.environment, "cleanup", None)
        if cleanup:
            cleanup()
