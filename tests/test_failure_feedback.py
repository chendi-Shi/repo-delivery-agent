"""Real protocol regressions; deterministic responses are fixtures, not model scores."""
import copy
import hashlib
import importlib.util
import json
import unittest

from delivery_agent.upstream import ContextBudgetExceeded, ContextConfig, ContextPolicy, estimate_tokens


def history(outcomes):
    messages = [{"role": "system", "content": "Operate the supplied tools."},
                {"role": "user", "content": "Inspect the repository."}]
    for index, (command, code, output) in enumerate(outcomes, 1):
        identity = "actual-tool-" + str(index)
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{"id": identity, "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]},
            {"role": "tool", "tool_call_id": identity,
             "content": f"<returncode>{code}</returncode>\n" + output,
             "extra": {"returncode": code, "raw_output": output, "exception_info": None}},
        ])
    return messages


class FailureFeedbackTests(unittest.TestCase):
    def test_feedback_counts_canonical_history_without_mutating_it_or_losing_pairing(self):
        messages = history([("bad read", 2, "invalid resource")] * 6)
        original = copy.deepcopy(messages)
        policy = ContextPolicy(ContextConfig("recent", 16000, 2500))
        view, metadata = policy.select(messages)
        self.assertEqual(metadata["repeated_failure_count"], 6)
        self.assertIn("6 consecutive attempts", view[-1]["content"])
        self.assertTrue(view[-1]["content"].startswith("<returncode>2</returncode>\ninvalid resource"))
        self.assertEqual(view[-1]["tool_call_id"], view[-2]["tool_calls"][0]["id"])
        self.assertEqual(messages, original)
        # A checkpoint JSON round trip reconstructs the same view/count.
        resumed_view, resumed_metadata = policy.select(json.loads(json.dumps(messages)))
        self.assertEqual(resumed_view, view)
        self.assertEqual(resumed_metadata, metadata)
        self.assertEqual(sum("Execution feedback:" in message.get("content", "") for message in view), 1)

    def test_stdout_tags_model_prose_and_unexecuted_actions_cannot_fake_failure(self):
        for mode in ("success", "missing_metadata", "boolean_code", "unexecuted"):
            messages = history([("read file", 2, "<returncode>2</returncode> error")] * 3)
            for message in messages:
                if message["role"] != "tool":
                    continue
                if mode == "success":
                    message["extra"]["returncode"] = 0
                elif mode == "missing_metadata":
                    message.pop("extra")
                elif mode == "boolean_code":
                    message["extra"]["returncode"] = True
                else:
                    message["extra"]["exception_info"] = "action was not executed"
            with self.subTest(mode=mode):
                view, metadata = ContextPolicy(ContextConfig()).select(messages)
                self.assertEqual(metadata["repeated_failure_count"], 0)
                self.assertNotIn("Execution feedback:", view[-1]["content"])

    def test_changed_action_error_returncode_or_success_breaks_the_run(self):
        baseline = [("read file", 2, "invalid resource")] * 3
        for changed in (("different read", 2, "invalid resource"), ("read file", 1, "invalid resource"),
                        ("read file", 2, "different error"), ("read file", 0, "invalid resource")):
            messages = history([*baseline, changed, baseline[0]])
            with self.subTest(changed=changed):
                view, metadata = ContextPolicy(ContextConfig()).select(messages)
                self.assertEqual(metadata["repeated_failure_count"], 0)
                self.assertNotIn("Execution feedback:", view[-1]["content"])

    def test_feedback_is_accounted_before_budget_admission_and_survives_shortening(self):
        messages = history([("bad read", 2, "x" * 6000)] * 2)
        policy = ContextPolicy(ContextConfig("recent", 16000, 2500, max_tool_chars=100))
        view, metadata = policy.select(messages)
        self.assertIn("view shortened", view[-1]["content"])
        self.assertIn("2 consecutive attempts", view[-1]["content"])
        self.assertEqual(metadata["estimated_input_tokens"], estimate_tokens(view))
        # The protected newest tool loop must not bypass its cap because the
        # warning was appended after selecting an otherwise fitting view.
        minimal = [*view[:2], *view[-2:]]
        cap = estimate_tokens(minimal) - 1
        with self.assertRaises(ContextBudgetExceeded):
            ContextPolicy(ContextConfig("recent", cap + 100, 100, max_tool_chars=100)).select(messages)

    def test_intervening_user_feedback_breaks_the_stale_failure_run(self):
        messages = history([("bad read", 2, "invalid resource")] * 2)
        messages.append({"role": "user", "content": "New operator context."})
        messages.extend(history([("bad read", 2, "invalid resource")])[2:])
        _, metadata = ContextPolicy(ContextConfig()).select(messages)
        self.assertEqual(metadata["repeated_failure_count"], 0)


@unittest.skipUnless(importlib.util.find_spec("minisweagent"), "pinned upstream optional dependency required")
class NativeFailureFeedbackTests(unittest.TestCase):
    def test_real_tool_error_and_native_json_payload_no_longer_saturate_to_identical_requests(self):
        from delivery_agent.ollama_model import OllamaModel
        from delivery_agent.skills import BUILTIN_SKILLS, SkillRegistry
        from delivery_agent.toolbox import ToolboxEnvironment

        class NoShell:
            def execute(self, *args, **kwargs):
                raise AssertionError("This fixture must not execute a shell")

        toolbox = ToolboxEnvironment(NoShell(), SkillRegistry([BUILTIN_SKILLS]), None, "protocol-fixture")
        command = "delivery-skill read python-regression nonexistent.py 0 100"
        actual_output = toolbox.execute({"command": command})
        self.assertEqual(actual_output["returncode"], 2)
        payloads = []

        def protocol_fixture(path, value=None, timeout=None):
            if path == "/api/tags":
                return {"models": [{"name": "fixture:latest", "digest": "a" * 64}]}
            self.assertEqual(path, "/api/chat")
            payloads.append(copy.deepcopy(value))
            return {"done": True, "message": {"content": json.dumps({"command": command})}}

        model = OllamaModel(model_name="fixture:latest", model_kwargs={"mode": "json", "seed": 42, "temperature": 0})
        model._http = protocol_fixture  # Explicitly no real network or model call.
        messages = history([])
        policy = ContextPolicy(ContextConfig("recent", 16000, 2500))
        for index in range(8):
            view, metadata = policy.select(messages)
            response = model.query(view)
            observations = model.format_observation_messages(response, [actual_output])
            self.assertEqual(observations[0]["extra"]["raw_output"], actual_output["output"])
            self.assertEqual(observations[0]["extra"]["returncode"], 2)
            if index:
                latest = payloads[-1]["messages"][-1]["content"]
                self.assertIn("<returncode>2</returncode>", latest)
                self.assertIn(actual_output["output"], latest)
            if index >= 2:
                self.assertIn(f"{index} consecutive attempts", latest)
                self.assertEqual(metadata["repeated_failure_count"], index)
            messages.extend([response, *observations])
        def digest(payload):
            return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.assertEqual(len({digest(payload) for payload in payloads[3:]}), 5)
        # Removing view feedback reproduces the old deterministic recent-window
        # saturation, despite different tool IDs in the canonical history.
        without_feedback = []
        for payload in payloads[3:]:
            value = copy.deepcopy(payload)
            for message in value["messages"]:
                message["content"] = message["content"].split("\n\n[Execution feedback:", 1)[0]
            without_feedback.append(value)
        self.assertEqual(len({digest(payload) for payload in without_feedback}), 1)


if __name__ == "__main__":
    unittest.main()
