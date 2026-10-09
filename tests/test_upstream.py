from __future__ import annotations

import copy
import importlib.metadata
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from delivery_agent.history import HistoryStore
from delivery_agent.upstream import (
    ContextBudgetExceeded, ContextConfig, ContextModel, ContextPolicy,
    InvalidToolHistory, UPSTREAM_VERSION, estimate_tokens, make_agent_class, require_upstream,
)


def assistant(call_id, command="python -m pytest"):
    return {"role": "assistant", "content": "Inspect failure and repair source.",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "bash", "arguments": json.dumps({"command": command})}}],
            "extra": {"actions": [{"command": command, "tool_call_id": call_id}]}}


def tool(call_id, content="tests passed", raw=None, returncode=0):
    return {"role": "tool", "tool_call_id": call_id, "content": content,
            "extra": {"raw_output": raw if raw is not None else content, "returncode": returncode}}


class ModelDouble:
    """Faithful v2 model protocol, including raw response/provider usage."""
    config = SimpleNamespace(model_name="double/model")

    def __init__(self):
        self.views = []

    def query(self, messages, **kwargs):
        self.views.append(copy.deepcopy(messages))
        response = assistant(f"call{len(self.views)}")
        response["extra"].update(cost=0.012, response={"usage": {"prompt_tokens": 57, "completion_tokens": 9}})
        return response

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [tool(action["tool_call_id"], output["output"], returncode=output["returncode"])
                for action, output in zip(message["extra"]["actions"], outputs)]

    def get_template_vars(self, **kwargs):
        return {"model_name": self.config.model_name, **kwargs}

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": self.config.model_name}}}}


class EnvironmentDouble:
    config = SimpleNamespace(cwd="/testbed")

    def execute(self, action, cwd="", **kwargs):
        return {"output": "test result", "returncode": 0, "exception_info": ""}

    def get_template_vars(self, **kwargs):
        return {"cwd": self.config.cwd, **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment": {"cwd": self.config.cwd}}}}


class BaseAgentDouble:
    """Small protocol double, never used as an evidence of model performance."""
    def __init__(self, model, env, **kwargs):
        self.model, self.env = model, env
        self.config = SimpleNamespace(**kwargs)
        self.messages = []

    def add_messages(self, *messages):
        self.messages.extend(messages)
        return list(messages)

    def serialize(self):
        return {**self.model.serialize(), "messages": self.messages}

    def save(self, path):
        Path(path).write_text(json.dumps(self.serialize(), ensure_ascii=False), encoding="utf-8")


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{"role": "system", "content": "Fix code; preserve original constraints."},
                         {"role": "user", "content": "Fix timezone regression in parser.py"}]

    def test_recent_keeps_original_task_and_complete_parallel_tool_loop(self):
        self.messages += [assistant("old"), tool("old", "x" * 2500),
                          {"role": "assistant", "content": "Parallel inspection",
                           "tool_calls": [{"id": "a"}, {"id": "b"}]},
                          tool("a", "source code"), tool("b", "test output")]
        policy = ContextPolicy(ContextConfig("recent", 1300, 100, 1, 400))
        view, metadata = policy.select(self.messages)
        self.assertEqual(view[:2], self.messages[:2])
        self.assertEqual([message.get("tool_call_id") for message in view if message["role"] == "tool"], ["a", "b"])
        self.assertEqual(len(metadata["excluded_source_ids"]), 2)
        self.assertLessEqual(estimate_tokens(view), 1200)

    def test_retrieval_prefers_older_relevant_evidence_over_large_irrelevant_group(self):
        self.messages += [assistant("relevant", "cat parser.py"), tool("relevant", "timezone regression parser.py UTC offset"),
                          assistant("irrelevant", "cat unrelated.bin"), tool("irrelevant", "x" * 3000),
                          assistant("latest"), tool("latest", "Need earlier timezone evidence")]
        view, metadata = ContextPolicy(ContextConfig("retrieval", 1800, 100, 1, 1000)).select(self.messages)
        ids = [message.get("tool_call_id") for message in view if message["role"] == "tool"]
        self.assertIn("relevant", ids)
        self.assertNotIn("irrelevant", ids)
        self.assertIn("latest", ids)
        self.assertIn("e000005", metadata["excluded_source_ids"])

    def test_view_shortening_never_mutates_raw_trajectory(self):
        raw = "start\n" + "middle hidden_config=7\n" * 300 + "end"
        self.messages += [assistant("a"), tool("a", raw, raw)]
        original = copy.deepcopy(self.messages)
        view, metadata = ContextPolicy(ContextConfig("recent", 1300, 100, 1, 200)).select(self.messages)
        self.assertEqual(self.messages, original)
        self.assertIn("delivery-history read", view[-1]["content"])
        self.assertEqual(view[-1]["extra"]["raw_output"], raw)
        self.assertEqual(metadata["shortened_source_ids"], ["e000004"])

    def test_orphan_missing_and_duplicate_tool_results_rejected(self):
        cases = [[tool("a")], [assistant("a")], [assistant("a"), tool("b")],
                 [assistant("a"), tool("a"), tool("a")]]
        policy = ContextPolicy(ContextConfig())
        for case in cases:
            with self.subTest(case=case), self.assertRaises(InvalidToolHistory):
                policy.select(self.messages + case)

    def test_mandatory_overflow_blocks_model_and_creates_no_request(self):
        with tempfile.TemporaryDirectory() as temp:
            history = HistoryStore(Path(temp) / "history.sqlite")
            model = ModelDouble()
            proxy = ContextModel(model, history, ContextConfig("recent", 300, 100, 1, 100), Path(temp))
            with self.assertRaises(ContextBudgetExceeded):
                proxy.query(self.messages + [assistant("a"), tool("a")])
            self.assertEqual(model.views, [])
            self.assertFalse((Path(temp) / "request-views.jsonl").exists())
            history.close()

    def test_baseline_refuses_overflow_instead_of_silently_dropping_context(self):
        with self.assertRaises(ContextBudgetExceeded):
            ContextPolicy(ContextConfig("baseline", 500, 100)).select(
                self.messages + [assistant("a"), tool("a", "x" * 1000)])

    def test_notes_are_bounded_memory_and_do_not_split_tool_pairs(self):
        self.messages += [assistant("a"), tool("a")]
        notes = [{"name": "constraint", "text": "Use e000002; preserve UTC"}]
        view, metadata = ContextPolicy(ContextConfig()).select(self.messages, notes)
        self.assertIn("unverified", view[2]["content"])
        self.assertEqual(view[3:], self.messages[2:])
        self.assertEqual(metadata["notes"], notes)

    def test_proxy_records_actual_usage_separately_from_selection_estimate(self):
        with tempfile.TemporaryDirectory() as temp:
            history = HistoryStore(Path(temp) / "history.sqlite")
            model = ModelDouble()
            proxy = ContextModel(model, history, ContextConfig(), Path(temp))
            proxy.query(self.messages)
            delivery = proxy.serialize()["info"]["delivery"]
            self.assertEqual(delivery["usage"]["input_tokens"], 57)
            self.assertEqual(delivery["usage"]["output_tokens"], 9)
            events = [json.loads(line) for line in (Path(temp) / "request-views.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["event"], "request")
            self.assertNotEqual(events[0]["estimated_input_tokens"], 57)
            self.assertEqual(events[1]["usage"]["prompt_tokens"], 57)
            self.assertEqual(proxy.format_message(role="user", content="x"), {"role": "user", "content": "x"})
            self.assertEqual(proxy.get_template_vars()["model_name"], "double/model")
            history.close()

    def test_subclass_indexes_original_messages_and_prohibits_artifact_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            cls = make_agent_class(BaseAgentDouble)
            agent = cls(ModelDouble(), EnvironmentDouble(), output_dir=temp, system_template="system", instance_template="{{task}}")
            self.assertIsInstance(agent, BaseAgentDouble)
            message = tool("a", "short", "full hidden evidence")
            agent.add_messages(message)
            self.assertEqual(agent.delivery_history.read("e000001")["text"], "full hidden evidence")
            self.assertEqual(agent.messages[0]["extra"]["delivery_source_id"], "e000001")
            agent.save(Path(temp) / "trajectory.json")
            agent.close()
            with self.assertRaises(ValueError):
                cls(ModelDouble(), EnvironmentDouble(), output_dir=temp)

    def test_configuration_rejects_invalid_profiles_and_budgets(self):
        for kwargs in [{"profile": "unknown"}, {"context_tokens": 100, "reserve_tokens": 100},
                       {"recent_groups": 0}, {"max_tool_chars": 5}]:
            with self.assertRaises(ValueError):
                ContextConfig(**kwargs)


try:
    INSTALLED = importlib.metadata.version("mini-swe-agent") == UPSTREAM_VERSION
except importlib.metadata.PackageNotFoundError:
    INSTALLED = False


@unittest.skipUnless(INSTALLED, "optional mini-swe-agent==2.4.6 is not installed")
class ActualUpstreamTests(unittest.TestCase):
    def test_actual_budget_rejection_does_not_increment_upstream_api_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            model = ModelDouble()
            agent = make_agent_class()(model, EnvironmentDouble(), output_dir=temp,
                                       context_config=ContextConfig("recent", 400, 100),
                                       system_template="Repair source", instance_template="{{task}}")
            with self.assertRaises(ContextBudgetExceeded):
                agent.run("Fix parser.py")
            self.assertEqual(agent.n_calls, 0)
            self.assertEqual(model.views, [])
            agent.close()

    def test_actual_default_agent_runs_tool_loop_and_limits_without_provider(self):
        # Uses the real pinned run/query/action/serialize paths and trusted model
        # and environment doubles. This is an adapter contract test, not a score.
        with tempfile.TemporaryDirectory() as temp:
            agent = make_agent_class()(ModelDouble(), EnvironmentDouble(), output_dir=temp,
                                       system_template="Repair source", instance_template="{{task}}",
                                       step_limit=1, cost_limit=1.0)
            self.assertIsInstance(agent, require_upstream())
            result = agent.run("Fix parser.py")
            self.assertEqual(result["exit_status"], "LimitsExceeded")
            data = json.loads((Path(temp) / "trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(data["info"]["mini_version"], UPSTREAM_VERSION)
            self.assertEqual(data["info"]["model_stats"]["api_calls"], 1)
            self.assertEqual(data["messages"][2]["extra"]["actions"][0]["command"], "python -m pytest")
            self.assertEqual(data["messages"][3]["extra"]["raw_output"], "test result")
            self.assertEqual(data["info"]["delivery"]["usage"]["input_tokens"], 57)
            agent.close()

    def test_actual_upstream_submission_is_preserved_and_is_not_reported_as_resolved(self):
        from minisweagent.exceptions import Submitted

        class SubmitEnvironment(EnvironmentDouble):
            def execute(self, action, cwd="", **kwargs):
                raise Submitted({"role": "exit", "content": "diff --git a/a.py b/a.py\n",
                                 "extra": {"exit_status": "Submitted", "submission": "diff --git a/a.py b/a.py\n"}})

        with tempfile.TemporaryDirectory() as temp:
            agent = make_agent_class()(ModelDouble(), SubmitEnvironment(), output_dir=temp,
                                       system_template="Repair source", instance_template="{{task}}",
                                       step_limit=2, cost_limit=1.0)
            result = agent.run("Fix a.py")
            self.assertEqual(result["exit_status"], "Submitted")
            self.assertEqual(result["submission"], "diff --git a/a.py b/a.py\n")
            self.assertNotIn("resolved", result)
            self.assertEqual(agent.delivery_history.read("e000004")["text"], result["submission"])
            agent.close()


if __name__ == "__main__":
    unittest.main()
