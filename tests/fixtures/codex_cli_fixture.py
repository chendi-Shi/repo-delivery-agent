"""Deterministic subprocess protocol fixture; this is not a language model."""
import json
import os
import sys
import time
from pathlib import Path


def emit(value):
    print(json.dumps(value), flush=True)


def option(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if "--version" in sys.argv:
    print("codex-cli 0.154.0")
    raise SystemExit(0)

scenario = option("--fixture-scenario", "response")
if scenario.startswith("flood-"):
    target = sys.stdout if scenario == "flood-stdout" else sys.stderr
    target.write("x" * 262144)
    target.flush()
    time.sleep(20)
    raise SystemExit(0)
if scenario == "nonzero":
    print("private diagnostic must not reach exception", file=sys.stderr)
    raise SystemExit(9)
emit({"type": "thread.started", "thread_id": "fixture-only"})
emit({"type": "turn.started"})
if scenario == "timeout":
    time.sleep(20)
    raise SystemExit(0)
if scenario == "tool":
    emit({"type": "item.started", "item": {"id": "native-command", "type": "command_execution",
        "command": "this must never execute"}})
    time.sleep(20)
    raise SystemExit(0)
payload = sys.stdin.read()
selected = json.loads(payload.split("\n", 1)[1])["messages"]
audit = option("--fixture-audit")
if audit:
    with Path(audit).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"cwd": os.getcwd(), "files": sorted(p.name for p in Path.cwd().iterdir()),
            "args": sys.argv[1:], "selected": selected}) + "\n")
command = ("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" if any(m.get("role") == "tool" for m in selected)
    else "delivery-file replace module.py <<'DELIVERY'\nvalue = 1\n---REPLACE---\nvalue = 2\nDELIVERY")
answer = "malformed command object" if scenario == "bad-command" else json.dumps({"command": command})
emit({"type": "item.completed", "item": {"id": "reply", "type": "agent_message", "text": answer}})
emit({"type": "turn.completed", "usage": {"input_tokens": 101, "cached_input_tokens": 17,
    "cache_write_input_tokens": 0, "output_tokens": 23, "reasoning_output_tokens": 5}})
