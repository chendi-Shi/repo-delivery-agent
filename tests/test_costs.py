"""Accounting and actual upstream pricing contracts; responses are model-free fixtures."""
import copy
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from delivery_agent.costs import monetary_cost, summarize_cost
from delivery_agent.scheduler import SchedulerError, validate_payload


class MonetaryCostTests(unittest.TestCase):
    def response(self, cost, **extra):
        return {"role": "assistant", "extra": {"response": {"usage": {}}, "cost": cost, **extra}}

    def test_unknown_zero_placeholder_and_missing_response_never_become_free_run(self):
        known = self.response(0.25)
        unknown = self.response(0.0, cost_unknown=True, reported_cost=None)
        result = summarize_cost([known, unknown], request_count=3)
        self.assertIsNone(result["reported_total"])
        self.assertEqual(result["known_subtotal"], 0.25)
        self.assertEqual(result["responses_with_unknown_cost"], 1)
        self.assertEqual(result["requests_without_cost_response"], 1)
        self.assertFalse(result["complete"])
        self.assertIsNone(monetary_cost(unknown["extra"]))
        self.assertIsNone(summarize_cost([])["reported_total"])

    def test_canonical_resume_keeps_costs_and_format_error_costs_without_tool_double_count(self):
        messages = [self.response(0.2), self.response(0.3)]
        messages[-1]["role"] = "user"  # Upstream FormatError response.
        messages.append({"role": "tool", "extra": {"response": {}, "cost": 999}})
        expected = summarize_cost(messages, request_count=2)
        self.assertEqual(expected["reported_total"], 0.5)
        self.assertEqual(summarize_cost(json.loads(json.dumps(messages)), request_count=2), expected)
        self.assertIsNone(summarize_cost(messages, request_count=2, unknown_requests=1)["reported_total"])

    def test_nonfinite_boolean_negative_and_explicit_unknown_amounts_are_not_prices(self):
        for value in (None, True, -1, float("nan"), float("inf")):
            self.assertIsNone(monetary_cost({"cost": value}))

    def test_scheduler_parity_default_and_explicit_unknown_price_policy(self):
        payload = {"task": {"instance_id": "fixture-task", "repo": "owner/repo", "base_commit": "a" * 40,
            "image": "python:3.12-slim", "problem_statement": "Inspect a fixture", "public_checks": []},
            "config": {"model": "openai/custom-fixture"}}
        self.assertIsNone(validate_payload(payload)["config"]["cost_tracking"])
        for mode in ("default", "ignore_errors"):
            candidate = copy.deepcopy(payload)
            candidate["config"]["cost_tracking"] = mode
            self.assertEqual(validate_payload(candidate)["config"]["cost_tracking"], mode)
        for value in (True, "disabled", {}, []):
            candidate = copy.deepcopy(payload)
            candidate["config"]["cost_tracking"] = value
            with self.assertRaises(SchedulerError):
                validate_payload(candidate)
        for provider in ("ollama_native", "codex_cli"):
            candidate = copy.deepcopy(payload)
            candidate["config"].update(provider=provider, cost_tracking="ignore_errors")
            with self.assertRaises(SchedulerError):
                validate_payload(candidate)

    def test_codex_scheduler_configuration_is_bounded_and_executable_stays_worker_local(self):
        payload = {"task": {"instance_id": "fixture-task", "repo": "owner/repo", "base_commit": "a" * 40,
            "image": "python:3.12-slim", "problem_statement": "Inspect a fixture", "public_checks": []},
            "config": {"provider": "codex_cli", "model": "gpt-model-fixture", "reasoning_effort": "xhigh"}}
        checked = validate_payload(payload)
        self.assertEqual(checked["config"]["reasoning_effort"], "xhigh")
        self.assertIsNone(checked["config"]["cost_tracking"])
        for key, value in (("reasoning_effort", "invalid"), ("reasoning_effort", []),
                           ("codex_binary", "host-command.exe"), ("api_base", "http://remote.example")):
            candidate = copy.deepcopy(payload)
            candidate["config"][key] = value
            with self.assertRaises(SchedulerError):
                validate_payload(candidate)


@unittest.skipUnless(importlib.util.find_spec("litellm") and importlib.util.find_spec("minisweagent"),
                     "installed pinned agent extra required for the genuine LiteLLM pricing contract")
class UpstreamPricingContractTests(unittest.TestCase):
    def test_actual_upstream_unknown_price_is_explicit_and_tools_usage_survive(self):
        # Use the installed real upstream class. Only the provider completion and
        # price lookup are fixtures; no API, credentials, or live model are used.
        with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
            from delivery_agent.litellm_model import CostAwareLitellmModel
            from minisweagent.models.litellm_model import LitellmModel
            from litellm import ModelResponse
        self.assertTrue(issubclass(CostAwareLitellmModel, LitellmModel))
        response = ModelResponse(model="custom-fixture", choices=[{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-call",
                "type": "function", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}]}}],
            usage={"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21})
        model = CostAwareLitellmModel(model_name="openai/custom-fixture", cost_tracking="ignore_errors")
        with patch.object(model, "_query", return_value=response), \
                patch("litellm.cost_calculator.completion_cost", side_effect=ValueError("fixture: unknown model price")):
            result = model.query([{"role": "user", "content": "Inspect the fixture."}])
        self.assertEqual(result["extra"]["actions"][0]["command"], "pwd")
        self.assertEqual(result["extra"]["response"]["usage"]["prompt_tokens"], 17)
        self.assertIsNone(result["extra"]["reported_cost"])
        self.assertTrue(result["extra"]["cost_unknown"])
        self.assertEqual(result["extra"]["cost"], 0.0)  # Internal upstream bookkeeping only.
        self.assertIn("known_costs_only", model.serialize()["info"]["cost_policy"]["cost_limit_scope"])
        strict = CostAwareLitellmModel(model_name="openai/custom-fixture", cost_tracking="default")
        with patch("litellm.cost_calculator.completion_cost", side_effect=ValueError("fixture: unknown price")):
            with self.assertRaises(RuntimeError):
                strict._calculate_cost(response)
        with patch("litellm.cost_calculator.completion_cost", return_value=0.125):
            priced = strict._calculate_cost(response)
        self.assertEqual(priced["reported_cost"], 0.125)
        self.assertFalse(priced["cost_unknown"])


if __name__ == "__main__":
    unittest.main()
