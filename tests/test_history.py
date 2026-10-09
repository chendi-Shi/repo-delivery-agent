from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery_agent.history import HistoryEnvironment, HistoryStore


class EnvironmentDouble:
    config = {"image": "double"}

    def __init__(self):
        self.calls = []

    def execute(self, action, cwd="", **kwargs):
        self.calls.append((action, cwd, kwargs))
        return {"output": "forwarded", "returncode": 0, "exception_info": ""}

    def get_template_vars(self, **kwargs):
        return {"cwd": "/testbed", **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment": self.config}}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        self.history = HistoryStore(self.path)
        self.environment = EnvironmentDouble()
        self.proxy = HistoryEnvironment(self.environment, self.history)

    def tearDown(self):
        self.history.close()
        self.temp.cleanup()

    def test_truncated_view_retains_complete_unicode_raw_evidence_after_reopen(self):
        raw = "prefix\n" + "中间证据 hidden_config=正确值\n" * 100 + "tail"
        source = self.history.append({"role": "tool", "content": "prefix...[truncated]...tail",
                                      "extra": {"raw_output": raw}})
        hits = self.history.search("hidden_config")
        self.assertEqual(hits[0]["source_id"], source)
        self.assertIn("正确值", hits[0]["snippet"])
        self.history.close()
        self.history = HistoryStore(self.path)
        page = self.history.read(source, 0, 4000)
        self.assertEqual(page["text"], raw[:4000])
        self.assertEqual(page["total_chars"], len(raw))

    def test_read_pagination_reconstructs_full_output_and_limits(self):
        raw = "abcdefghij" * 1500
        source = self.history.append({"role": "user", "content": raw})
        collected = ""
        offset = 0
        while True:
            page = self.history.read(source, offset, 4000)
            collected += page["text"]
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(collected, raw)
        for args in [(source, -1, 1), (source, 0, 4001), ("e999999", 0, 10), ("../../secret", 0, 10)]:
            with self.assertRaises(ValueError):
                self.history.read(*args)

    def test_search_is_literal_and_bounded(self):
        for _ in range(12):
            self.history.append({"role": "user", "content": "100%_config missing"})
        self.assertEqual(len(self.history.search("%_", 8)), 8)
        self.assertEqual(self.history.search("not_found"), [])
        for query, limit in [("", 5), ("x" * 201, 5), ("x", 9), ("x", 0)]:
            with self.assertRaises(ValueError):
                self.history.search(query, limit)

    def test_notes_have_persistent_cardinality_and_size_bounds(self):
        for index in range(8):
            self.history.note(f"n{index}", f"constraint from e00000{index + 1}")
        self.history.note("n0", "updated")
        self.assertEqual(len(self.history.notes()), 8)
        with self.assertRaises(ValueError):
            self.history.note("overflow", "fact")
        for name, text in [("has spaces", "fact"), ("", "fact"), ("valid", "x" * 501)]:
            with self.assertRaises(ValueError):
                self.history.note(name, text)

    def test_history_commands_never_reach_execution_environment(self):
        source = self.history.append({"role": "tool", "content": "hidden setting"})
        commands = ["delivery-history search 'hidden' 2", f"delivery-history read {source} 0 10",
                    "delivery-history note finding 'Use e000001'", "delivery-history search 'hidden' && touch bad",
                    "delivery-history note x '$(read-secret)'", "delivery-history search 'unterminated",
                    "delivery-history read e000001 -1", "delivery-history read e000001 0 5000",
                    "delivery-history", "delivery-history unknown", "delivery-history search 'x'; touch bad",
                    "delivery-history; touch bad", "delivery-history|touch bad"]
        results = [self.proxy.execute({"command": command}) for command in commands]
        self.assertEqual(self.environment.calls, [])
        self.assertEqual(results[0]["returncode"], 0)
        self.assertEqual(json.loads(results[1]["output"])["text"], "hidden set")
        self.assertEqual(results[3]["returncode"], 2)
        self.assertEqual(results[5]["returncode"], 2)
        self.assertIn("$(read-secret)", self.history.notes()[-1]["text"])

    def test_regular_command_and_proxy_metadata_are_forwarded(self):
        action = {"command": "python -m pytest", "tool_call_id": "call1"}
        self.proxy.execute(action, cwd="/testbed", timeout=30)
        self.assertEqual(self.environment.calls, [(action, "/testbed", {"timeout": 30})])
        self.assertEqual(self.proxy.get_template_vars(foo="bar")["foo"], "bar")
        self.assertEqual(self.proxy.serialize(), self.environment.serialize())
        self.proxy.execute({"command": "echo 'delivery-history'"})
        self.assertEqual(len(self.environment.calls), 2)
        self.proxy.execute({"command": "delivery-history-extra 'unterminated"})
        self.assertEqual(len(self.environment.calls), 3)


if __name__ == "__main__":
    unittest.main()
