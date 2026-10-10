"""Pure AST guard fixtures, never coding-task solutions or model metrics."""
import ast
import copy
import json
import unittest

from scripts.closure_seed_guard import MAX_FILE_BYTES, MAX_VIOLATIONS, ast_sha256, evaluate_source_seed


LAST = "def last(items, default=None):\n    return 'original last fixture'\n"
SPLIT = "def split_after(items, pred, maxsplit=-1):\n    return 'original split fixture'\n"
MODULE = "def first(items, default=None):\n    return 'source fixture'\n\n" + LAST + "\n" + SPLIT
PATH = "more_itertools/more.py"
ORIGINAL = {PATH: MODULE, "more_itertools/__init__.py": "from .more import *\n"}


def candidate(tail="", replacement=None):
    values = dict(ORIGINAL)
    values[PATH] = (MODULE if replacement is None else replacement) + "\n" + tail
    return values


class ClosureSeedGuardTests(unittest.TestCase):
    def assertBlocked(self, values, kind=None, function=None):
        receipt = evaluate_source_seed(ORIGINAL, values)
        self.assertFalse(receipt["eligible"])
        self.assertTrue(receipt["violations"])
        if kind:
            self.assertIn(kind, {row["kind"] for row in receipt["violations"]})
        if function:
            self.assertIn(function, {row["function"] for row in receipt["violations"]})
        return receipt

    def test_unchanged_source_is_static_eligible_and_not_a_verified_run(self):
        receipt = evaluate_source_seed(ORIGINAL, dict(ORIGINAL))
        self.assertTrue(receipt["eligible"])
        self.assertEqual(receipt["schema"], 1)
        self.assertEqual(receipt["kind"], "source_seed_leakage_guard")
        self.assertEqual(receipt["violations"], [])
        self.assertFalse(receipt["source_success_verified_here"])
        self.assertTrue(receipt["requires_independent_verified_source_run"])
        self.assertTrue(receipt["requires_transfer_feature_failures"])
        self.assertEqual([row["function"] for row in receipt["targets"]], ["last", "split_after"])
        for target in receipt["targets"]:
            self.assertEqual(target["module"], "more_itertools.more")
            self.assertEqual(target["path"], PATH)
            self.assertRegex(target["old_ast_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(target["old_ast_sha256"], target["new_ast_sha256"])

    def test_first_change_added_first_tests_comments_and_formatting_are_allowed(self):
        changed = MODULE.replace("return 'source fixture'", "local = 'different source fixture'\n    return local")
        changed = changed.replace("def last(items, default=None):", "def last( items, default = None ): # formatting")
        values = candidate(replacement=changed)
        values["tests/test_new_first.py"] = "from more_itertools import first\n\ndef test_first_extension():\n    assert callable(first)\n"
        self.assertTrue(evaluate_source_seed(ORIGINAL, values)["eligible"])

    def test_entire_protected_definition_includes_signature_decorators_and_body(self):
        changes = (
            MODULE.replace("original last fixture", "changed target body"),
            MODULE.replace("def last(items, default=None)", "def last(items, default=None, *, added=None)"),
            MODULE.replace("def last(items, default=None):", "@decorator\ndef last(items, default=None):"),
        )
        for change in changes:
            with self.subTest(change=change[:70]):
                receipt = self.assertBlocked(candidate(replacement=change), "protected_definition_changed", "last")
                target = receipt["targets"][0]
                self.assertNotEqual(target["old_ast_sha256"], target["new_ast_sha256"])

    def test_missing_duplicate_and_new_nested_target_definitions_are_rejected(self):
        self.assertBlocked(candidate(replacement=MODULE.replace(LAST, "")), "missing_or_duplicate_candidate_definition", "last")
        self.assertBlocked(candidate(LAST), "missing_or_duplicate_candidate_definition", "last")
        self.assertBlocked(candidate("def helper():\n    def last(items):\n        return 'nested target fixture'\n    return last\n"),
                           "new_nested_target_definition", "last")
        without_module = dict(ORIGINAL)
        without_module.pop(PATH)
        self.assertBlocked(without_module, "missing_or_duplicate_candidate_definition")

    def test_added_target_definition_in_another_package_module_is_rejected(self):
        values = dict(ORIGINAL)
        values["more_itertools/new_helpers.py"] = LAST
        self.assertBlocked(values, "missing_or_duplicate_candidate_definition", "last")

    def test_top_level_bindings_and_import_aliases_cannot_override_targets(self):
        changes = (
            "last = first\n", "last: object = first\n", "del last\n",
            "for last in ():\n    pass\n", "with context() as split_after:\n    pass\n",
            "try:\n    pass\nexcept Exception as last:\n    pass\n",
            "class last:\n    pass\n", "from builtins import len as last\n",
            "import types as split_after\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail))

    def test_module_attributes_function_objects_and_transitive_aliases_are_guarded(self):
        changes = (
            "import more_itertools as mi\nmi.last = first\n",
            "import more_itertools.more as core\ncore.split_after = first\n",
            "import more_itertools as mi\nalias = mi\nalias.last = first\n",
            "from .more import last as target\ntarget.__code__ = first.__code__\n",
            "import more_itertools as mi\nfunction_alias = mi.last\nfunction_alias.__defaults__ = (1,)\n",
            "import sys\nsys.modules['more_itertools'].split_after = first\n",
            "import importlib\npkg = importlib.import_module('more_itertools')\npkg.last = first\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail), "namespace_assignment")

    def test_setattr_delattr_and_unknown_dynamic_target_writes_are_rejected(self):
        changes = (
            "import more_itertools as mi\nsetattr(mi, 'last', first)\n",
            "import more_itertools as mi\ndelattr(mi, 'split_after')\n",
            "import more_itertools as mi\nsetattr(mi, field_name, first)\n",
            "setattr(__import__('more_itertools'), 'last', first)\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail), "dynamic_rebinding")

    def test_globals_and_module_dictionary_mutations_are_rejected(self):
        changes = (
            "globals()['last'] = first\n", "globals().update({'split_after': first})\n",
            "globals().update(dynamic_mapping)\n",
            "import more_itertools as mi\nmi.__dict__['last'] = first\n",
            "import more_itertools as mi\nvars(mi)['split_after'] = first\n",
            "import more_itertools as mi\nvars(mi).update({'last': first})\n",
            "import more_itertools as mi\nmi.__dict__.clear()\n",
            "import sys\nsys.modules['more_itertools'] = substitute\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail))

    def test_global_rebinding_inside_helper_is_rejected_but_local_last_is_allowed(self):
        self.assertBlocked(candidate("def helper():\n    global last\n    last = first\n"), "namespace_assignment", "last")
        local_first = MODULE.replace("return 'source fixture'", "last = 'local variable'\n    return last")
        self.assertTrue(evaluate_source_seed(ORIGINAL, candidate(replacement=local_first))["eligible"])
        first_only = candidate("import more_itertools as mi\nsetattr(mi, 'first', replacement_first)\nglobals()['first'] = replacement_first\n")
        self.assertTrue(evaluate_source_seed(ORIGINAL, first_only)["eligible"])

    def test_mock_patch_and_monkeypatch_target_overrides_are_rejected(self):
        changes = (
            "import unittest.mock as mock\nmock.patch('more_itertools.last', return_value=None)\n",
            "import more_itertools as mi\nfrom unittest.mock import patch\npatch.object(mi, 'split_after', first)\n",
            "monkeypatch.setattr('more_itertools.last', first)\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail))

    def test_new_opaque_execution_or_wildcard_binding_is_not_seed_eligible(self):
        self.assertBlocked(candidate("exec(dynamic_code)\n"), "opaque_dynamic_execution")
        self.assertBlocked(candidate("eval(dynamic_code)\n"), "opaque_dynamic_execution")
        self.assertBlocked(candidate("from unknown_package import *\n"), "namespace_import_binding")
        before = dict(ORIGINAL)
        before[PATH] += "\nexec(existing_operator_code)\n"
        self.assertTrue(evaluate_source_seed(before, dict(before))["eligible"])

    def test_namespace_mutation_in_new_test_module_is_also_detected(self):
        values = dict(ORIGINAL)
        values["tests/test_new_first.py"] = "import more_itertools as mi\nsetattr(mi, 'last', substitute)\n"
        self.assertBlocked(values, "dynamic_rebinding", "last")

    def test_aliased_dictionaries_and_builtins_cannot_hide_dynamic_rebinding(self):
        changes = (
            "namespace = globals()\nnamespace['last'] = first\n",
            "import more_itertools as mi\nnamespace = vars(mi)\nnamespace.update({'last': first})\n",
            "import more_itertools as mi\nnamespace = mi.__dict__\nnamespace['split_after'] = first\n",
            "import more_itertools as mi\nwriter = setattr\nwriter(mi, 'last', first)\n",
            "import builtins as b\nimport more_itertools as mi\nwriter = b.setattr\nwriter(mi, 'last', first)\n",
            "import more_itertools as mi\ngetattr(mi, 'last').__code__ = first.__code__\n",
            "execute = exec\nexecute(dynamic_code)\n",
        )
        for tail in changes:
            with self.subTest(tail=tail):
                self.assertBlocked(candidate(tail))
        values = dict(ORIGINAL)
        values["tests/test_new_first.py"] = "execute = exec\nexecute(dynamic_code)\n"
        self.assertBlocked(values, "opaque_dynamic_execution")

    def test_many_violations_produce_a_bounded_ineligible_receipt(self):
        values = candidate("\n".join("last = " + str(index) for index in range(MAX_VIOLATIONS + 10)))
        receipt = self.assertBlocked(values)
        self.assertEqual(len(receipt["violations"]), MAX_VIOLATIONS)
        self.assertEqual(receipt["omitted_violations"], 10)

    def test_receipt_never_exposes_private_paths_source_or_dynamic_literals(self):
        private_path = "C:\\Users\\CompanySecret\\private.py"
        receipt = evaluate_source_seed(ORIGINAL, {private_path: MODULE})
        self.assertFalse(receipt["eligible"])
        self.assertNotIn("CompanySecret", json.dumps(receipt))
        tail = "import more_itertools as mi\nsetattr(mi, 'last', lambda: 'PRIVATE_LITERAL_SECRET')\n"
        receipt = self.assertBlocked(candidate(tail))
        self.assertNotIn("PRIVATE_LITERAL_SECRET", json.dumps(receipt))
        self.assertNotIn("lambda", json.dumps(receipt))
        for row in receipt["violations"]:
            self.assertEqual(set(row), {"kind", "path", "module", "function", "old_ast_sha256", "new_ast_sha256"})

    def test_invalid_syntax_mapping_escape_and_source_budgets_fail_closed(self):
        for value in (None, {"../private.py": MODULE}, {"more_itertools/\nprivate.py": MODULE}, {PATH: "def broken(:\n"},
                      {PATH: " " * (MAX_FILE_BYTES + 1)}, {PATH: 42}):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(evaluate_source_seed(ORIGINAL, value)["eligible"])
        self.assertFalse(evaluate_source_seed({}, ORIGINAL)["eligible"])
        duplicate = {PATH: MODULE, "more_itertools\\more.py": MODULE}
        self.assertFalse(evaluate_source_seed(ORIGINAL, duplicate)["eligible"])

    def test_ast_hash_ignores_locations_and_type_params_but_not_function_body(self):
        original = ast.parse(LAST).body[0]
        changed = copy.deepcopy(original)
        changed.lineno = 100
        changed.col_offset = 20
        changed.type_params = [ast.Name(id="CompatibilityType", ctx=ast.Load())]
        self.assertEqual(ast_sha256(original), ast_sha256(changed))
        changed.body = [ast.Pass()]
        self.assertNotEqual(ast_sha256(original), ast_sha256(changed))


if __name__ == "__main__":
    unittest.main()
