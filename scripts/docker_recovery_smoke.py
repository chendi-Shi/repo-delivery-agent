"""Real Docker + genuine DefaultAgent process crash/attach check, with no LLM."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.checkpoint import inspect_checkpoint
from delivery_agent.repository import atomic_json, git
from delivery_agent.runtime import DockerSession
from delivery_agent.upstream import make_agent_class


class FixtureModel:
    config = SimpleNamespace(model_name="trusted/docker-recovery-fixture")

    def __init__(self, crash=False):
        self.crash = crash

    def query(self, messages, **kwargs):
        index = sum(message.get("role") == "assistant" for message in messages)
        if index == 0:
            command = 'python -c "from pathlib import Path; p=Path(\'counter\'); p.write_text(str(int(p.read_text())+1)); print(\'changed once\')"'
        else:
            command = "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ntrusted recovery done\\n'"
        identity = "call" + str(index)
        return {"role": "assistant", "content": "trusted integration action",
                "tool_calls": [{"id": identity, "type": "function", "function":
                                {"name": "bash", "arguments": json.dumps({"command": command})}}],
                "extra": {"actions": [{"command": command, "tool_call_id": identity}], "cost": 0.01,
                          "response": {"usage": {"prompt_tokens": 12, "completion_tokens": 3}}}}

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        if self.crash:
            # The action result was committed to checkpoint, but observations
            # are not yet in DefaultAgent's trajectory. This really dies.
            os._exit(73)
        return [{"role": "tool", "tool_call_id": action["tool_call_id"], "content": output["output"],
                 "extra": {"raw_output": output["output"], "returncode": output["returncode"]}}
                for action, output in zip(message["extra"]["actions"], outputs)]

    def get_template_vars(self, **kwargs):
        return {"model_name": self.config.model_name, **kwargs}

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": self.config.model_name}}}}


def worker(root, resume):
    identity = json.loads((root / "identity.json").read_text())
    environment = DockerSession.attach(root / "workspace", identity)
    agent = make_agent_class()(FixtureModel(crash=not resume), environment, output_dir=root / "trial",
                              system_template="Trusted recovery integration", instance_template="{{task}}",
                              step_limit=4, cost_limit=1, recovery_binding=identity, resume=resume)
    try:
        result = agent.run("Increment counter exactly once and submit")
        if result.get("exit_status") != "Submitted":
            raise RuntimeError("recovered DefaultAgent did not submit")
        atomic_json(root / "worker-result.json", {"result": result, "calls": agent.n_calls, "cost": agent.cost,
                    "usage": agent.model.serialize()["info"]["delivery"]["usage"],
                    "history_count": agent.delivery_history.count()})
    finally:
        agent.close()
        environment.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--output")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.worker is not None:
        worker(args.worker, args.resume)
        return
    if args.output is None:
        parser.error("--output is required")
    output = Path(args.output)
    if output.exists():
        parser.error("Use a fresh output file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        work = root / "workspace"
        work.mkdir()
        git(work, "init")
        (work / "counter").write_text("0", encoding="utf-8")
        git(work, "add", ".")
        git(work, "-c", "user.name=Recovery Integration", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "trusted base")
        commit = git(work, "rev-parse", "HEAD").stdout.decode().strip()
        original = DockerSession(work, args.image)
        identity = original.identity(commit)
        atomic_json(root / "identity.json", identity)
        original.preserve()
        original.close()
        try:
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(root)]
            crashed = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if crashed.returncode != 73:
                raise RuntimeError("worker did not crash at the tested boundary: " + crashed.stderr[-4000:])
            before = inspect_checkpoint(root / "trial" / "checkpoint.json")
            if before["transition"]["pending"] is not None or len(before["transition"]["completed"]) != 1:
                raise RuntimeError("completed tool result was not durable before process death")
            if (work / "counter").read_text() != "1":
                raise RuntimeError("initial real container action did not execute once")
            restored = subprocess.run(command + ["--resume"], capture_output=True, text=True, timeout=120)
            if restored.returncode:
                raise RuntimeError("recovery failed: " + restored.stderr[-4000:])
            if (work / "counter").read_text() != "1":
                raise RuntimeError("completed action was replayed against the real container")
            result = json.loads((root / "worker-result.json").read_text())
            if result["calls"] != 2 or abs(result["cost"] - 0.02) > 1e-9 or result["history_count"] != 6:
                raise RuntimeError("recovery reset or duplicated counters/history")
            report = {"fixture": True, "label": "TRUSTED CRASH RECOVERY; NOT MODEL PERFORMANCE", "passed": True,
                      "image_id": identity["image_id"], "checks": ["real process crash", "same container reattachment",
                      "isolation identity validation", "completed action not repeated", "budget/history continuation",
                      "submission after recovery", "resumed container cleanup"], "model_calls": result["calls"],
                      "usage": result["usage"]}
            atomic_json(output, report)
            print(json.dumps(report))
        finally:
            # This ID was created by this integration test. It is never inferred
            # from a broad Docker listing or used to stop unrelated containers.
            cleanup = subprocess.run(["docker", "rm", "-f", identity["container_id"]],
                                     capture_output=True, timeout=30)
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise RuntimeError("test-owned container cleanup is unconfirmed")


if __name__ == "__main__":
    main()
