"""Public task-contract checks; fixtures do not measure LLM performance."""
import ast
import hashlib
import json
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.repository import RepoTask


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "examples" / "delivery-suite-v1"
MORE_SHA = "81c21a8db1c922260cd673841257fb9b112c1318"
REQUESTS_SHA = "5f7a3a74aab1625c2bb65f643197ee885e3da576"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def program(command):
    parts = shlex.split(command)
    if len(parts) != 3 or parts[:2] != ["python", "-c"]:
        raise ValueError("public checks must be one bounded Python program")
    return parts[2]


def fixture_digest(source, methods):
    """Construct an AST identity for synthetic old-test fixtures, not a solution."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if hasattr(node, "type_params"):
            node._fields = tuple(name for name in node._fields if name != "type_params")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    rows = []
    for cls, name in methods:
        selected = {node.name: node for node in classes[cls].body if isinstance(node, ast.FunctionDef)}
        rows.append((cls, name, ast.dump(selected[name], include_attributes=False)))
    raw = json.dumps(sorted(rows), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class DeliverySuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = read_json(SUITE / "manifest.json")
        cls.entries = cls.manifest["tasks"]
        cls.values = [read_json(SUITE / entry["path"]) for entry in cls.entries]
        cls.tasks = [RepoTask.parse(value) for value in cls.values]

    def test_four_distinct_frozen_candidates_with_explicit_roles(self):
        self.assertEqual(self.manifest["suite_id"], "delivery-suite-v1")
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(len(self.tasks), 4)
        self.assertEqual(len({task.instance_id for task in self.tasks}), 4)
        self.assertEqual([entry["role"] for entry in self.entries],
                         ["source", "near_transfer", "independent", "official_public_probe"])
        self.assertEqual([entry["api"] for entry in self.entries[:3]], ["first", "last", "split_after"])
        for task in self.tasks[:3]:
            self.assertEqual(task.repo, "more-itertools/more-itertools")
            self.assertEqual(task.base_commit, MORE_SHA)
            self.assertEqual(task.image, "python:3.12-slim")
        self.assertEqual(self.tasks[3].repo, "psf/requests")
        self.assertEqual(self.tasks[3].base_commit, REQUESTS_SHA)

    def test_task_bytes_and_parser_fingerprints_match_the_manifest(self):
        for entry, task in zip(self.entries, self.tasks):
            with self.subTest(task=task.instance_id):
                self.assertEqual(Path(entry["path"]).name, entry["path"])
                raw = (SUITE / entry["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                self.assertEqual(task.fingerprint(), entry["fingerprint"])
                self.assertEqual(task.instance_id, entry["instance_id"])

    def test_batch_is_an_exact_ordered_copy_of_the_four_tasks(self):
        self.assertEqual(read_json(SUITE / "batch.json"), self.values)
        self.assertEqual(hashlib.sha256((SUITE / "batch.json").read_bytes()).hexdigest(),
                         self.manifest["batch_sha256"])

    def test_host_baseline_receipt_matches_frozen_commands_and_feature_failures(self):
        proof = read_json(SUITE / self.manifest["baseline_evidence"])
        self.assertTrue(proof["fixture"])
        self.assertEqual(proof["llm_calls"], 0)
        self.assertFalse(proof["model_score_eligible"])
        self.assertIn("not claimed", proof["target_container_validation"])
        self.assertIn("collections aliases", proof["execution"])
        self.assertEqual(len(proof["tasks"]), 4)
        for receipt, entry, task in zip(proof["tasks"], self.entries, self.tasks):
            self.assertEqual(receipt["actual_commit"], task.base_commit)
            self.assertEqual(receipt["task_sha256"], entry["sha256"])
            self.assertEqual(receipt["fingerprint"], task.fingerprint())
            self.assertTrue(receipt["source_clean"])
            self.assertEqual(receipt["checks"][0]["exit_code"], 0)
            self.assertNotEqual(receipt["checks"][1]["exit_code"], 0)
            for check, command in zip(receipt["checks"], task.public_checks):
                self.assertEqual(check["command_sha256"], hashlib.sha256(command.encode("utf-8")).hexdigest())
            failure = receipt["checks"][1]["stderr"]
            if task.repo == "psf/requests":
                self.assertIn("UnicodeDecodeError", failure)
                self.assertIn("'ascii' codec", failure)
            else:
                self.assertIn("KeyError", failure)
                self.assertIn("keep_separator" if entry["api"] == "split_after" else "default_factory", failure)

    def test_agent_inputs_contain_only_public_repo_task_fields(self):
        expected = {"instance_id", "repo", "base_commit", "problem_statement", "image", "public_checks"}
        for value, task in zip(self.values, self.tasks):
            self.assertEqual(set(value), expected)
            self.assertEqual(len(task.public_checks), 2)
            self.assertNotIn("sweb.eval", task.image)
            self.assertNotIn("swebench/", task.image)
        self.assertTrue(self.manifest["selection_policy"]["retain_all_attempts"])
        self.assertFalse(self.manifest["scoring_policy"]["fixture_is_model_performance"])
        self.assertFalse(self.manifest["scoring_policy"]["official_public_probe_is_official_resolve_score"])

    def test_requests_reuses_original_problem_statement_utf8_exactly(self):
        original_path = ROOT / "examples" / "official-smoke" / "psf__requests-2931.json"
        original = read_json(original_path)
        public = self.values[3]
        for field in ("instance_id", "repo", "base_commit", "problem_statement"):
            self.assertEqual(public[field].encode("utf-8"), original[field].encode("utf-8"))
        self.assertEqual(public["image"], "python:3.8-slim")
        self.assertEqual(original["public_checks"], [])
        provenance = self.manifest["requests_origin"]
        self.assertEqual(provenance["public_bug_report"], "https://github.com/psf/requests/issues/2930")
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(),
                         provenance["frozen_original_task_sha256"])
        self.assertEqual(hashlib.sha256(public["problem_statement"].encode("utf-8")).hexdigest(),
                         provenance["problem_statement_utf8_sha256"])

    def test_near_transfer_uses_a_different_task_and_no_current_answer(self):
        policy = self.manifest["knowledge_policy"]
        source, target = self.tasks[:2]
        self.assertNotEqual(source.instance_id, target.instance_id)
        self.assertEqual(policy["source_instance_id"], source.instance_id)
        self.assertEqual(policy["allowed_target_instance_ids"], [target.instance_id])
        self.assertTrue(policy["require_independent_verified_source_run"])
        self.assertTrue(policy["exclude_current_task_sources_in_search_and_read"])
        self.assertFalse(policy["agent_self_report_is_verified"])
        self.assertFalse(policy["cross_repository_sharing"])
        self.assertFalse(self.manifest["comparison_policy"]["near_transfer_proves_general_improvement"])
        self.assertTrue(self.manifest["comparison_policy"]["same_task_retries_share_one_task_identity"])

    def test_all_public_programs_are_bounded_and_compile_without_a_shell(self):
        for task in self.tasks:
            self.assertIn("Path.cwd().resolve()", program(task.public_checks[0]))
            for index, command in enumerate(task.public_checks):
                with self.subTest(task=task.instance_id, index=index):
                    self.assertLessEqual(len(command), 4000)
                    code = program(command)
                    compile(code, task.instance_id + ":public:" + str(index), "exec")

    def test_original_method_guard_is_shared_and_allows_added_tests(self):
        guards = [program(task.public_checks[0]) for task in self.tasks[:3]]
        self.assertEqual(guards[0], guards[1])
        self.assertEqual(guards[0], guards[2])
        snapshot = self.manifest["upstream_regression_snapshot"]
        self.assertEqual(snapshot["method_count"], 20)
        self.assertEqual(len(snapshot["methods"]), 20)
        self.assertEqual(snapshot["methods"], sorted(snapshot["methods"]))
        self.assertIn(snapshot["ast_sha256"], guards[0])
        for cls, name in snapshot["methods"]:
            self.assertIn(cls, guards[0])
            self.assertIn(name, guards[0])
        self.assertIn("suite.countTestCases() >= 20", guards[0])
        self.assertIn("result.skipped", guards[0])
        self.assertIn('x != "type_params"', guards[0])
        self.assertNotIn("read_bytes()", guards[0], "do not lock the entire upstream test file")

    def _synthetic_old_methods(self):
        methods = self.manifest["upstream_regression_snapshot"]["methods"]
        classes = {}
        for cls, name in methods:
            classes.setdefault(cls, []).append(name)
        return "\n".join("class " + cls + ":\n" + "\n".join(
            "    def " + name + "(self):\n        self.assertEqual(1, 1)" for name in names)
            for cls, names in classes.items()) + "\n"

    def _execute_guard_prefix(self, candidate_source, original_source):
        methods = self.manifest["upstream_regression_snapshot"]["methods"]
        expected_digest = fixture_digest(original_source, methods)
        code = program(self.tasks[0].public_checks[0])
        tree = ast.parse(code)
        before_runner = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "tests.test_more":
                break
            before_runner.append(node)
        prefix = ast.Module(body=before_runner, type_ignores=[])
        frozen_digest = self.manifest["upstream_regression_snapshot"]["ast_sha256"]
        for node in ast.walk(prefix):
            if isinstance(node, ast.Constant) and node.value == frozen_digest:
                node.value = expected_digest
        fixture_root = (ROOT / "synthetic-suite-fixture").resolve()
        module = types.ModuleType("more_itertools")
        module.__file__ = str(fixture_root / "more_itertools" / "__init__.py")
        with patch.dict(sys.modules, {"more_itertools": module}), \
                patch.object(Path, "cwd", return_value=fixture_root), \
                patch.object(Path, "read_text", return_value=candidate_source):
            exec(compile(prefix, "synthetic-guard-fixture", "exec"), {})

    def test_actual_guard_accepts_comments_formatting_and_new_methods(self):
        old = self._synthetic_old_methods()
        changed = old.replace("self.assertEqual(1, 1)", "self.assertEqual( 1, 1 )  # formatting")
        changed += "\nclass ExtraTests:\n    def test_new(self):\n        assert True\n"
        changed = changed.replace("class FirstTests:\n", "class FirstTests:\n    def test_added(self):\n        assert True\n")
        self._execute_guard_prefix(changed, old)

    def test_actual_guard_rejects_weakened_missing_or_skipped_original_methods(self):
        old = self._synthetic_old_methods()
        weakened = old.replace("self.assertEqual(1, 1)", "pass", 1)
        decorated = old.replace("    def test_even", '    @unittest.skip("bypass")\n    def test_even', 1)
        missing = old.replace("def test_even", "def removed_test_even", 1)
        for candidate in (weakened, decorated, missing):
            with self.subTest(candidate=candidate[:60]):
                with self.assertRaises((AssertionError, KeyError)):
                    self._execute_guard_prefix(candidate, old)

    def test_requests_checks_only_prepare_offline_and_cover_compatibility(self):
        regression, binary = (program(command) for command in self.tasks[3].public_checks)
        for code in (regression, binary):
            self.assertIn("example.invalid", code)
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {"send", "put", "post", "get", "urlopen"})
            self.assertIn(".prepare()", code)
        self.assertIn("parse_qs", regression)
        self.assertIn("application/x-www-form-urlencoded", regression)
        self.assertIn("bytes(range(256))", binary)
        self.assertIn('isinstance(prepared.body, bytes)', binary)
        self.assertIn('prepared.body == payload', binary)
        self.assertIn('str(len(payload))', binary)


if __name__ == "__main__":
    unittest.main()
