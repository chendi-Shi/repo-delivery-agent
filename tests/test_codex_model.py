"""Codex transport contracts with a deterministic child, never model-quality evidence."""
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent import codex_model
from delivery_agent.codex_model import CodexModel, CodexProtocolError, _EventStream, _run_bounded


FIXTURE = Path(__file__).parent / "fixtures" / "codex_cli_fixture.py"


def successful_events():
    return [{"type": "thread.started", "thread_id": "fixture-only"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"command":"delivery-file list"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 101, "output_tokens": 23, "cached_input_tokens": 17}}]


class CodexTransportTests(unittest.TestCase):
    def test_strict_stream_preserves_measured_usage_without_subtracting_cached_input(self):
        stream = _EventStream()
        for event in successful_events():
            stream.feed(json.dumps(event))
        value = stream.finish()
        self.assertEqual(value["usage"], {"input_tokens": 101, "output_tokens": 23, "cached_input_tokens": 17})
        self.assertEqual(json.loads(value["text"])["command"], "delivery-file list")

    def test_all_native_tool_items_rejected_even_started_or_failed(self):
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "todo_list", "unknown"):
            for event_type in ("item.started", "item.updated", "item.completed"):
                with self.subTest(kind=kind, event_type=event_type):
                    stream = _EventStream()
                    for event in successful_events()[:2]: stream.feed(json.dumps(event))
                    with self.assertRaises(CodexProtocolError):
                        stream.feed(json.dumps({"type": event_type, "item": {"type": kind, "status": "failed"}}))

    def test_missing_invalid_and_boolean_usage_never_becomes_zero(self):
        for usage in ({}, {"input_tokens": 1}, {"input_tokens": True, "output_tokens": 2},
                      {"input_tokens": -1, "output_tokens": 2}, {"input_tokens": 1, "output_tokens": None},
                      {"input_tokens": 1, "output_tokens": 2, "cached_input_tokens": -1}):
            with self.subTest(usage=usage):
                stream = _EventStream()
                for event in successful_events()[:-1]: stream.feed(json.dumps(event))
                with self.assertRaises(CodexProtocolError):
                    stream.feed(json.dumps({"type": "turn.completed", "usage": usage}))

    def test_failed_duplicate_and_incomplete_turns_are_rejected(self):
        alternatives = [[{"type": "error", "message": "failed"}], [{"type": "turn.failed"}],
            successful_events()[:2] + [successful_events()[1]],
            successful_events()[:3] + [successful_events()[2]],
            successful_events() + [{"type": "turn.started"}]]
        for events in alternatives:
            with self.subTest(events=events):
                stream = _EventStream()
                with self.assertRaises(CodexProtocolError):
                    for event in events: stream.feed(json.dumps(event))
                    stream.finish()
        with self.assertRaises(CodexProtocolError): _EventStream().finish()
        with self.assertRaises(CodexProtocolError): _EventStream().feed("not JSON")

    def test_only_known_disabled_tool_diagnostic_is_allowed_before_turn(self):
        diagnostic = {"type": "item.completed", "item": {"type": "error", "message":
            "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
            "enable `features.code_mode_host` and install `codex-code-mode-host`."}}
        stream = _EventStream()
        stream.feed(json.dumps(successful_events()[0])); stream.feed(json.dumps(diagnostic))
        for event in successful_events()[1:]: stream.feed(json.dumps(event))
        self.assertEqual(stream.finish()["diagnostics"], ["code_mode_host_disabled"])
        for message in ("authentication failed", "model unavailable"):
            rejected = copy.deepcopy(diagnostic); rejected["item"]["message"] = message
            with self.assertRaises(CodexProtocolError): _EventStream().feed(json.dumps(rejected))
        stream = _EventStream()
        for event in successful_events()[:2]: stream.feed(json.dumps(event))
        with self.assertRaises(CodexProtocolError): stream.feed(json.dumps(diagnostic))

    def test_unsupported_sampling_controls_and_invalid_config_fail_before_process(self):
        for kwargs in ({"seed": 42}, {"temperature": 0}, {"max_tokens": 100}, {"num_ctx": 16000},
                       {"timeout": True}, {"timeout": float("nan")}, {"timeout": 0}, {"reasoning_effort": "unbounded"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CodexModel(model_name="gpt-6-astra", model_kwargs={"binary": sys.executable, **kwargs})
        with self.assertRaises(ValueError): CodexModel(model_name="", model_kwargs={"binary": sys.executable})

    def test_launch_flags_disable_host_tools_and_do_not_invent_sampling_caps(self):
        model = CodexModel(model_name="gpt-6-astra", model_kwargs={"binary": sys.executable})
        args = model._argv(Path("command-schema.json"))
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-astra")
        for flag in ("--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
            self.assertIn(flag, args)
        disabled = {args[i + 1] for i, arg in enumerate(args) if arg == "--disable"}
        self.assertTrue({"shell_tool", "code_mode_host", "plugins", "apps", "hooks", "multi_agent",
            "browser_use", "computer_use", "view_image", "skill_search"}.issubset(disabled))
        self.assertIn("project_doc_max_bytes=0", args)
        self.assertIn("mcp_servers={}", args)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertFalse(any(value.startswith("model_providers.") for value in args))
        self.assertFalse(model.serialize()["info"]["internal_transport_retries_controlled"])
        self.assertIsNone(model.serialize()["info"]["underlying_transport_request_count"])
        self.assertIn("unbounded_connection_retries", disabled)
        xhigh = CodexModel(model_name="gpt-6-astra", model_kwargs={"binary": sys.executable, "reasoning_effort": "xhigh"})
        self.assertIn('model_reasoning_effort="xhigh"', xhigh._argv(Path("command-schema.json")))
        self.assertFalse(any("max_tokens" in value or "seed=" in value for value in args))

    def test_actual_subprocess_output_and_error_caps_abort_without_waiting_for_child(self):
        with tempfile.TemporaryDirectory() as folder:
            for channel in ("stdout", "stderr"):
                start = time.monotonic()
                with self.subTest(channel=channel), patch.object(codex_model, "MAX_" + channel.upper(), 1024):
                    with self.assertRaisesRegex(CodexProtocolError, "byte limit"):
                        _run_bounded([sys.executable, str(FIXTURE), "--fixture-scenario", "flood-" + channel],
                            cwd=folder, timeout=5)
                self.assertLess(time.monotonic() - start, 8)

    def test_timeout_and_forbidden_tool_event_terminate_actual_child(self):
        with tempfile.TemporaryDirectory() as folder:
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                _run_bounded([sys.executable, str(FIXTURE), "--fixture-scenario", "timeout"], cwd=folder, timeout=0.3)
            self.assertLess(time.monotonic() - start, 8)
            stream = _EventStream()
            with self.assertRaisesRegex(CodexProtocolError, "forbidden tool"):
                _run_bounded([sys.executable, str(FIXTURE), "--fixture-scenario", "tool"],
                    cwd=folder, timeout=5, on_line=stream.feed)

    def test_nonzero_process_does_not_leak_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CodexProtocolError) as caught:
                _run_bounded([sys.executable, str(FIXTURE), "--fixture-scenario", "nonzero"], cwd=folder, timeout=5)
            self.assertIn("exit code 9", str(caught.exception))
            self.assertNotIn("private diagnostic", str(caught.exception))

    def test_identity_hash_is_executable_provenance_not_model_weights(self):
        model = CodexModel(model_name="gpt-6-astra", model_kwargs={"binary": sys.executable})
        with patch.object(codex_model, "_run_bounded", return_value=(b"codex-cli 0.154.0\n", {})) as invoke:
            identity = model.resolve_identity()
            self.assertEqual(invoke.call_args.args[0], [str(Path(sys.executable).resolve()), "--version"])
            self.assertFalse(Path(invoke.call_args.kwargs["cwd"]).exists())
            with open(sys.executable, "rb") as handle:
                self.assertEqual(identity["binary_sha256"], hashlib.file_digest(handle, "sha256").hexdigest())
            self.assertFalse(identity["weight_digest_available"])
            self.assertNotIn("digest", identity)
            self.assertIsNone(model.serialize()["info"]["model_digest"])
            model.resolve_identity()
            self.assertEqual(invoke.call_count, 1)
            model.identity["binary_sha256"] = "0" * 64
            with self.assertRaises(CodexProtocolError): model.resolve_identity()


@unittest.skipUnless(importlib.util.find_spec("minisweagent"), "pinned upstream optional dependency required")
class CodexUpstreamTests(unittest.TestCase):
    def route_fixture(self, audit, scenario="response"):
        def invoke(argv, **kwargs):
            return _run_bounded([argv[0], str(FIXTURE), "--fixture-audit", str(audit),
                "--fixture-scenario", scenario, *argv[1:]], **kwargs)
        return patch.object(codex_model, "_run_bounded", side_effect=invoke)

    def test_real_default_agent_observations_edits_usage_and_request_isolation(self):
        from delivery_agent.file_environment import FileEnvironment
        from delivery_agent.upstream import run_agent
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); workspace = root / "workspace"; workspace.mkdir()
            (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
            audit = root / "fixture-audit.jsonl"
            model = CodexModel(model_name="fixture-explicit-model", model_kwargs={"binary": sys.executable, "timeout": 5})
            with self.route_fixture(audit):
                outcome = run_agent(model, FileEnvironment(workspace), task="Fix value to 2", output_dir=root / "agent",
                    agent_config={"system_template": "Use delivery-file commands.", "instance_template": "{{task}}",
                        "step_limit": 4, "cost_limit": 1, "wall_time_limit_seconds": 30},
                    context_tokens=16000, reserve_tokens=2000)
            self.assertEqual(outcome["exit_status"], "Submitted")
            self.assertEqual((workspace / "module.py").read_text(), "value = 2\n")
            usage = outcome["delivery"]["usage"]
            self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (202, 46))
            requests = [json.loads(line) for line in audit.read_text().splitlines()]
            self.assertEqual(len(requests), 2)
            self.assertNotEqual(requests[0]["cwd"], requests[1]["cwd"])
            for request in requests:
                self.assertEqual(request["files"], ["command-schema.json"])
                self.assertFalse(Path(request["cwd"]).exists())
                self.assertNotEqual(Path(request["cwd"]), workspace)
                self.assertTrue(all(set(m).issubset({"role", "content", "tool_calls", "tool_call_id"}) for m in request["selected"]))
            observation = requests[1]["selected"][-1]
            self.assertEqual(observation["role"], "tool")
            self.assertIn("replace completed: module.py", observation["content"])
            self.assertIn("<returncode>0</returncode>", observation["content"])

    def test_malformed_command_retains_actual_usage_and_unknown_cost(self):
        from minisweagent.exceptions import FormatError
        with tempfile.TemporaryDirectory() as folder:
            audit = Path(folder) / "fixture-audit.jsonl"
            model = CodexModel(model_name="fixture-explicit-model", model_kwargs={"binary": sys.executable})
            with self.route_fixture(audit, "bad-command"), self.assertRaises(FormatError) as caught:
                model.query([{"role": "user", "content": "fixture", "extra": {"not_selected": "private"}}])
            extra = caught.exception.messages[0]["extra"]
            self.assertEqual(extra["response"]["usage"], {"prompt_tokens": 101, "completion_tokens": 23})
            self.assertTrue(extra["cost_unknown"])
            self.assertIsNone(extra["reported_cost"])
            self.assertEqual(extra["cost_source"], "chatgpt_plan_unpriced")
            self.assertNotIn("private", audit.read_text())
            self.assertEqual(len(audit.read_text().splitlines()), 1)

    def test_failed_cli_attempt_is_not_automatically_retried(self):
        model = CodexModel(model_name="fixture-explicit-model", model_kwargs={"binary": sys.executable})
        with patch.object(model, "resolve_identity", return_value={}), patch.object(codex_model, "_run_bounded",
                side_effect=TimeoutError("fixture timeout")) as invoke:
            with self.assertRaises(TimeoutError): model.query([{"role": "user", "content": "fixture"}])
            self.assertEqual(invoke.call_count, 1)
            self.assertFalse(Path(invoke.call_args.kwargs["cwd"]).exists())


if __name__ == "__main__":
    unittest.main()
