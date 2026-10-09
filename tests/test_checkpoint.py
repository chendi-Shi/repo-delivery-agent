from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.checkpoint import (CheckpointMismatch, CheckpointStore, RecoveryBusy,
                                       get_pending, inspect_checkpoint)


class CheckpointTests(unittest.TestCase):
    def test_integrity_binding_and_pending_are_checked_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            store = CheckpointStore(path, {"task": "x", "image": "sha256:a"})
            store.write(state={"messages": []}, transition={"pending": {"kind": "model"}})
            store.close()
            self.assertEqual(get_pending(path), {"kind": "model"})
            with self.assertRaises(CheckpointMismatch):
                inspect_checkpoint(path, {"task": "changed", "image": "sha256:a"})
            value = json.loads(path.read_text())
            value["state"]["messages"] = [{"role": "user", "content": "modified"}]
            path.write_text(json.dumps(value))
            with self.assertRaises(CheckpointMismatch):
                inspect_checkpoint(path)

    def test_failed_atomic_replacement_preserves_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            store = CheckpointStore(path, {})
            store.write(state={"revision": "old"}, transition=None)
            previous = path.read_bytes()
            with patch("delivery_agent.checkpoint.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    store.write(state={"revision": "new"}, transition=None)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            store.close()

    def test_writer_lock_rejects_concurrent_owner_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            first = CheckpointStore(path, {})
            with self.assertRaises(RecoveryBusy):
                CheckpointStore(path, {})
            first.close()
            second = CheckpointStore(path, {})
            second.close()


# This child really imports the installed, pinned DefaultAgent and really dies
# with os._exit at protocol boundaries. Its model/environment are trusted local
# fixtures: these checks establish recovery semantics, never model performance.
CHILD = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from delivery_agent.upstream import make_agent_class
from delivery_agent.recovery import AmbiguousRecovery
from minisweagent.exceptions import Submitted, FormatError

root = Path(sys.argv[1])
mode, resume, policy = sys.argv[2], sys.argv[3] == "resume", sys.argv[4]
output = root / "trial"

def log(name, text):
    with (root / name).open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")

class Model:
    config = SimpleNamespace(model_name="trusted/recovery-fixture")
    def query(self, messages, **kwargs):
        log("model.log", "physical-request")
        if mode == "pending_model" and not resume:
            os._exit(73)
        index = sum(message.get("role") == "assistant" for message in messages)
        if mode == "format_completed" and index == 0:
            raise FormatError({"role":"assistant", "content":"invalid action", "extra":{"cost":0.013}},
                              {"role":"user", "content":"Please emit a valid bash action"})
        command = "modify" if index == 0 else "submit"
        identity = "call" + str(index + 1)
        commands = [command, "inspect"] if mode.startswith("multi_tool") and index == 0 else [command]
        actions = [{"command":value,"tool_call_id":identity + "_" + str(number)} for number,value in enumerate(commands)]
        return {"role":"assistant", "content":"perform " + command,
                "tool_calls":[{"id":action["tool_call_id"],"type":"function","function":{"name":"bash","arguments":json.dumps({"command":action["command"]})}}
                              for action in actions],
                "extra":{"actions":actions,"cost":0.01,
                         "response":{"usage":{"prompt_tokens":12,"completion_tokens":3}}}}
    def format_message(self, **kwargs):
        return kwargs
    def format_observation_messages(self, message, outputs, template_vars=None):
        if mode in {"tool_completed", "multi_tool_completed"} and not resume:
            os._exit(73)
        return [{"role":"tool", "tool_call_id": action["tool_call_id"], "content":output["output"],
                 "extra":{"raw_output":output["output"],"returncode":output["returncode"]}}
                for action, output in zip(message["extra"]["actions"], outputs)]
    def get_template_vars(self, **kwargs):
        return {"model_name":self.config.model_name, **kwargs}
    def serialize(self):
        return {"info":{"config":{"model":{"model_name":self.config.model_name}}}}

class Environment:
    config = SimpleNamespace(cwd="/workspace")
    def execute(self, action, cwd="", **kwargs):
        if action["command"] == "submit":
            raise Submitted({"role":"exit","content":"done","extra":{"exit_status":"Submitted","submission":"fixture patch"}})
        if action["command"] == "inspect":
            log("inspections.log", "physical-inspection")
            if mode == "multi_tool_pending" and not resume:
                os._exit(73)
            return {"output":"inspection done","returncode":0,"exception_info":""}
        log("tools.log", "physical-command")
        counter = root / "counter"
        counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
        if mode == "pending_tool" and not resume:
            os._exit(73)
        return {"output":"changed once","returncode":0,"exception_info":""}
    def get_template_vars(self, **kwargs):
        return {"cwd":self.config.cwd, **kwargs}
    def serialize(self):
        return {"info":{"config":{"environment":{"cwd":self.config.cwd}}}}

agent = make_agent_class()(Model(), Environment(), output_dir=output,
    system_template="Repair source", instance_template="{{task}}", step_limit=6, cost_limit=1,
    recovery_binding={"image_id":"sha256:fixture", "workspace":str(root.resolve()), "base_commit":"a"*40, "container_id":"trusted-fixture"},
    resume=resume, recovery_policy=policy)
if mode in {"model_completed", "format_completed", "model_log_error"} and not resume:
    original_log = agent.model._log
    def crash_after_response(event):
        if event["event"] == ("error" if mode == "format_completed" else "response"):
            if mode == "model_log_error":
                raise RuntimeError("trusted local logging fault after durable response")
            os._exit(73)
        original_log(event)
    agent.model._log = crash_after_response
if mode == "step_complete" and not resume:
    original_save = agent.save
    def crash_after_save(*args, **kwargs):
        result = original_save(*args, **kwargs)
        os._exit(73)
    agent.save = crash_after_save
try:
    result = agent.run("Implement fixture repair")
    print(json.dumps({"result":result,"calls":agent.n_calls,"cost":agent.cost,
                      "usage":agent.model.serialize()["info"]["delivery"]["usage"],
                      "sources":[message["extra"]["delivery_source_id"] for message in agent.messages],
                      "history_count":agent.delivery_history.count()}))
except AmbiguousRecovery:
    print("AMBIGUOUS; no operation retried")
    sys.exit(31)
finally:
    agent.close()
'''


try:
    HAS_UPSTREAM = importlib.metadata.version("mini-swe-agent") == "2.4.6"
except importlib.metadata.PackageNotFoundError:
    HAS_UPSTREAM = False


@unittest.skipUnless(HAS_UPSTREAM, "optional genuine mini-swe-agent==2.4.6 is not installed")
class ActualCrashRecoveryTests(unittest.TestCase):
    def child(self, root: Path, mode: str, *, resume: bool = False, policy: str = "stop"):
        script = root / "child.py"
        script.write_text(CHILD, encoding="utf-8")
        snapshot = root / "source" / "delivery_agent"
        if not snapshot.exists():
            # Keep code immutable across both processes even while other
            # developers are editing the shared working tree. This is a real
            # source copy, not patched upstream metadata or a model substitute.
            import delivery_agent
            shutil.copytree(Path(delivery_agent.__file__).parent, snapshot,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join([str(snapshot.parent)] +
                                                    [str(Path(item).resolve()) for item in sys.path if item])
        environment["MSWEA_SILENT_STARTUP"] = "1"
        argv = [sys.executable, str(script), str(root), mode, "resume" if resume else "fresh", policy]
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        process = subprocess.Popen(argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, **options)
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            # Kill only the process/tree that this test created. Other workers,
            # model servers and their PIDs are never discovered or terminated.
            if process.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

    def test_completed_response_tool_and_step_survive_real_process_crash_without_replay(self):
        for mode in ("model_completed", "tool_completed", "step_complete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                crashed = self.child(root, mode)
                self.assertEqual(crashed.returncode, 73, crashed.stderr)
                restored = self.child(root, mode, resume=True)
                self.assertEqual(restored.returncode, 0, restored.stderr)
                result = json.loads(restored.stdout.splitlines()[-1])
                self.assertEqual(result["result"]["exit_status"], "Submitted")
                self.assertEqual(result["calls"], 2)
                self.assertAlmostEqual(result["cost"], 0.02)
                self.assertEqual(result["usage"]["input_tokens"], 24)
                self.assertTrue(result["usage"]["complete"])
                self.assertEqual((root / "counter").read_text(), "1")
                self.assertEqual((root / "model.log").read_text().count("physical-request"), 2)
                self.assertEqual(result["history_count"], 6)
                self.assertEqual(result["sources"], [f"e{index:06d}" for index in range(1, 7)])
                self.assertIsNone(get_pending(root / "trial" / "checkpoint.json"))

    def test_pending_remote_requests_and_commands_stop_until_explicit_retry(self):
        for mode in ("pending_model", "pending_tool"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                crashed = self.child(root, mode)
                self.assertEqual(crashed.returncode, 73, crashed.stderr)
                before = (root / "model.log").read_text()
                stopped = self.child(root, mode, resume=True)
                self.assertEqual(stopped.returncode, 31, stopped.stderr)
                self.assertEqual((root / "model.log").read_text(), before)
                self.assertIsNotNone(get_pending(root / "trial" / "checkpoint.json"))
                retried = self.child(root, mode, resume=True, policy="retry")
                self.assertEqual(retried.returncode, 0, retried.stderr)
                result = json.loads(retried.stdout.splitlines()[-1])
                self.assertEqual(result["result"]["exit_status"], "Submitted")
                self.assertEqual((root / "counter").read_text(), "2" if mode == "pending_tool" else "1")
                self.assertEqual(result["calls"], 3 if mode == "pending_model" else 2)
                self.assertEqual(result["usage"]["unknown_requests"], 1 if mode == "pending_model" else 0)
                record = inspect_checkpoint(root / "trial" / "checkpoint.json")
                self.assertEqual(record["recovery_decisions"][0]["policy"], "retry")

    def test_multiple_actions_preserve_completed_first_command_when_second_is_ambiguous(self):
        for mode in ("multi_tool_pending", "multi_tool_completed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                crashed = self.child(root, mode)
                self.assertEqual(crashed.returncode, 73, crashed.stderr)
                if mode == "multi_tool_pending":
                    stopped = self.child(root, mode, resume=True)
                    self.assertEqual(stopped.returncode, 31, stopped.stderr)
                restored = self.child(root, mode, resume=True,
                                      policy="retry" if mode == "multi_tool_pending" else "stop")
                self.assertEqual(restored.returncode, 0, restored.stderr)
                result = json.loads(restored.stdout.splitlines()[-1])
                self.assertEqual(result["result"]["exit_status"], "Submitted")
                self.assertEqual((root / "counter").read_text(), "1")
                self.assertEqual((root / "tools.log").read_text().count("physical-command"), 1)
                self.assertEqual((root / "inspections.log").read_text().count("physical-inspection"),
                                 2 if mode == "multi_tool_pending" else 1)
                self.assertEqual(result["history_count"], 7)

    def test_known_format_error_is_restored_without_rebilling_or_resetting_counter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            crashed = self.child(root, "format_completed")
            self.assertEqual(crashed.returncode, 73, crashed.stderr)
            restored = self.child(root, "format_completed", resume=True)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            result = json.loads(restored.stdout.splitlines()[-1])
            self.assertEqual(result["result"]["exit_status"], "Submitted")
            self.assertEqual(result["calls"], 2)
            self.assertAlmostEqual(result["cost"], 0.023)
            self.assertFalse(result["usage"]["complete"])
            state = inspect_checkpoint(root / "trial" / "checkpoint.json")["state"]
            self.assertEqual(state["n_consecutive_format_errors"], 1)

    def test_harness_error_after_response_does_not_discard_known_result_or_replay_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            failed = self.child(root, "model_log_error")
            self.assertEqual(failed.returncode, 1, failed.stderr)
            record = inspect_checkpoint(root / "trial" / "checkpoint.json")
            self.assertIsNotNone(record["transition"]["result"]["response"])
            restored = self.child(root, "model_log_error", resume=True)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            result = json.loads(restored.stdout.splitlines()[-1])
            self.assertEqual(result["calls"], 2)
            self.assertEqual((root / "model.log").read_text().count("physical-request"), 2)
            self.assertEqual((root / "counter").read_text(), "1")
            record = inspect_checkpoint(root / "trial" / "checkpoint.json")
            self.assertEqual(record["uncommitted_diagnostics"][0]["extra"]["exit_status"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
