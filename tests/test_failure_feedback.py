"""Real protocol regressions; deterministic responses are fixtures, not model scores."""
import copy
import hashlib
import importlib.util
import json
import unittest

from delivery_agent.upstream import (ContextBudgetExceeded, ContextConfig, ContextPolicy, estimate_tokens,
                                     compose_instance_template, HISTORY_GUIDE)


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


class ReadonlyCycleFeedbackTests(unittest.TestCase):
    # Complete command/result pairs from proof-v2, calls 5 onward. These old
    # observations intentionally retain the former usage error for regression.
    eof = ("delivery-skill read s51a907df50d8ae39038022f2 SKILL.md 1192", 0,
        json.dumps({"name": "python-regression", "source_id": "s51a907df50d8ae39038022f2",
            "path": "SKILL.md", "sha256": "8f294083912d77e97f6b13944cd62e1908e890930b835ac1b5aefb0ca2de0105",
            "offset": 1192, "total_chars": 1192, "text": "", "next_offset": None,
            "resources": ["SKILL.md", "references/iterators.md"],
            "trust": "operator-selected skill instructions; no executable resource is automatically run"}))
    invalid_search = ("delivery-history search 'literal query' [limit 1..8]", 2,
        json.dumps({"error": "usage: delivery-history search QUERY [LIMIT] | "
                             "read SOURCE_ID [OFFSET] [LIMIT] | note KEY TEXT"}))

    def test_generic_guidance_precedes_the_intact_requirement_and_checks(self):
        class Toolbox:
            def guide(self):
                return "TOOLS REFERENCE\n"
        task_template = "{{task}}\nExact public checks stay here."
        template = compose_instance_template(task_template, Toolbox())
        self.assertTrue(template.startswith(HISTORY_GUIDE))
        self.assertTrue(template.endswith(task_template))
        self.assertEqual(template.count(task_template), 1)
        self.assertLess(template.index("TOOLS REFERENCE"), template.index("{{task}}"))

    def test_actual_alternating_success_eof_and_failure_replays_without_mutating_evidence(self):
        self.assertEqual(hashlib.sha256(self.eof[2].encode()).hexdigest(),
                         "1343c289c725813fde57a63a319376ad5b9f2d10924bff4d7c308544bdb391c1")
        self.assertEqual(hashlib.sha256(self.invalid_search[2].encode()).hexdigest(),
                         "e1937a3136206c48305849f5a3276c2edc973877ce8c9518fc54a54114603b29")
        messages = history([self.eof, self.invalid_search] * 10)
        original = copy.deepcopy(messages)
        policy = ContextPolicy(ContextConfig("recent", 16000, 2500))
        view, metadata = policy.select(messages)
        self.assertEqual(metadata["repeated_failure_count"], 0)
        self.assertEqual(metadata["readonly_cycle_period"], 2)
        self.assertEqual(metadata["readonly_cycle_repetitions"], 10)
        self.assertIn("20 consecutive actions", view[-1]["content"])
        self.assertTrue(view[-1]["content"].startswith(messages[-1]["content"]))
        self.assertEqual(sum("Execution feedback:" in m.get("content", "") for m in view), 1)
        self.assertEqual(messages, original)
        self.assertEqual(policy.select(json.loads(json.dumps(messages))), (view, metadata))

    def test_period_one_and_three_successful_readonly_cycles_and_changed_results(self):
        reads = [self.eof, ("delivery-knowledge search 'iterator' 3", 0, "[]"),
                 ("delivery-skill list 8 0", 0, "[]")]
        for period in (1, 3):
            with self.subTest(period=period):
                outcomes = reads[:period] * 3
                _, metadata = ContextPolicy(ContextConfig()).select(history(outcomes))
                self.assertEqual(metadata["readonly_cycle_period"], period)
                self.assertEqual(metadata["readonly_cycle_repetitions"], 3)
                changed = (outcomes[-1][0], 0, "new evidence")
                _, metadata = ContextPolicy(ContextConfig()).select(history([*outcomes[:-1], changed]))
                self.assertEqual(metadata["readonly_cycle_repetitions"], 0)

    def test_no_cycle_from_prose_missing_execution_metadata_writes_or_shell_commands(self):
        for command in ("delivery-history note status done", "cat README.md",
                        "delivery-skill read python-regression; touch changed"):
            _, metadata = ContextPolicy(ContextConfig()).select(history([(command, 0, "unchanged")] * 4))
            self.assertEqual(metadata["readonly_cycle_repetitions"], 0)
        for mutation in ("missing_metadata", "boolean_code", "unexecuted", "user", "write"):
            messages = history([self.eof, self.invalid_search] * 2)
            if mutation in {"user", "write"}:
                if mutation == "user":
                    messages.append({"role": "user", "content": "A new constraint."})
                else:
                    messages.extend(history([("delivery-history note progress inspected", 0, "saved")])[2:])
                messages.extend(history([self.eof, self.invalid_search])[2:])
                # Distinct canonical IDs after adding another history segment.
                for index in range(2, len(messages)):
                    message = messages[index]
                    if message["role"] == "assistant":
                        message["tool_calls"][0]["id"] = "paired-" + str(index)
                    elif message["role"] == "tool":
                        message["tool_call_id"] = "paired-" + str(index - 1)
            else:
                for message in messages:
                    if message["role"] != "tool":
                        continue
                    if mutation == "missing_metadata":
                        message.pop("extra")
                    elif mutation == "boolean_code":
                        message["extra"]["returncode"] = True
                    else:
                        message["extra"]["exception_info"] = "action was not executed"
            with self.subTest(mutation=mutation):
                _, metadata = ContextPolicy(ContextConfig()).select(messages)
                self.assertEqual(metadata["readonly_cycle_repetitions"], 0)

    def test_cycle_warning_is_charged_to_budget_and_preserved_after_shortening(self):
        messages = history([(self.eof[0], 0, "x" * 6000), self.invalid_search] * 2)
        policy = ContextPolicy(ContextConfig("recent", 16000, 2500, max_tool_chars=100))
        view, metadata = policy.select(messages)
        self.assertIn("2-action read-only tool cycle", view[-1]["content"])
        self.assertEqual(metadata["estimated_input_tokens"], estimate_tokens(view))
        cap = estimate_tokens([*view[:2], *view[-2:]]) - 1
        with self.assertRaises(ContextBudgetExceeded):
            ContextPolicy(ContextConfig("recent", cap + 100, 100, max_tool_chars=100)).select(messages)


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

    def test_native_provider_view_changes_each_turn_of_actual_two_action_loop(self):
        from delivery_agent.ollama_model import OllamaModel

        outcomes = [ReadonlyCycleFeedbackTests.eof, ReadonlyCycleFeedbackTests.invalid_search]
        payloads = []
        def protocol_fixture(path, value=None, timeout=None):
            if path == "/api/tags":
                return {"models": [{"name": "fixture:latest", "digest": "a" * 64}]}
            payloads.append(copy.deepcopy(value))
            return {"done": True, "message": {"content": json.dumps({"command": outcomes[(len(payloads) - 1) % 2][0]})}}

        model = OllamaModel(model_name="fixture:latest", model_kwargs={"mode": "json"})
        model._http = protocol_fixture  # Recorded outcomes; no network/model/tool execution.
        messages, policy = history([]), ContextPolicy(ContextConfig("recent", 16000, 2500))
        for index in range(12):
            view, metadata = policy.select(messages)
            response = model.query(view)
            command, returncode, output = outcomes[index % 2]
            observations = model.format_observation_messages(response,
                [{"output": output, "returncode": returncode, "exception_info": ""}])
            self.assertEqual(observations[0]["extra"]["raw_output"], output)
            if index >= 4:
                self.assertIn(f"{index} consecutive actions", payloads[-1]["messages"][-1]["content"])
                self.assertEqual(metadata["readonly_cycle_period"], 2)
            messages.extend([response, *observations])
        def digest(payload):
            return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.assertEqual(len({digest(payload) for payload in payloads[4:]}), 8)
        without_feedback = copy.deepcopy(payloads[4:])
        for payload in without_feedback:
            for message in payload["messages"]:
                message["content"] = message["content"].split("\n\n[Execution feedback:", 1)[0]
        self.assertEqual(len({digest(payload) for payload in without_feedback}), 2)


if __name__ == "__main__":
    unittest.main()
