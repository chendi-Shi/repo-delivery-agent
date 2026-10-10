"""Static leakage guard for a genuinely verified source task, without execution.

Call this only after the source patch passed independent verification in a fresh
checkout. An eligible receipt is not a verification claim: the runner must also
execute the two transfer feature checks there and confirm the features remain
absent. This helper never changes, filters, or deletes the source patch/attempt.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath


PACKAGE = "more_itertools"
PROTECTED = ("last", "split_after")
MAX_FILES = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_VIOLATIONS = 64
_BUILTINS = {"exec", "eval", "setattr", "delattr", "getattr", "globals", "locals", "vars", "__import__"}


def ast_sha256(node):
    """Hash the entire definition/expression, excluding locations/type_params."""
    canonical = copy.deepcopy(node)
    for child in ast.walk(canonical):
        if hasattr(child, "type_params"):
            child._fields = tuple(name for name in child._fields if name != "type_params")
    return hashlib.sha256(ast.dump(canonical, include_attributes=False).encode("utf-8")).hexdigest()


def _sources(values):
    if not isinstance(values, Mapping) or len(values) > MAX_FILES:
        raise ValueError("invalid_source_mapping")
    parsed, total = {}, 0
    for key, source in values.items():
        if not isinstance(key, str) or not isinstance(source, str):
            raise ValueError("invalid_source_mapping")
        path = key.replace("\\", "/")
        parts = path.split("/")
        if (not path or len(path) > 300 or ":" in path or path.startswith("/")
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
                or any(part in {"", ".", "..", ".git"} for part in parts)
                or not path.endswith(".py") or path in parsed):
            raise ValueError("invalid_source_path")
        size = len(source.encode("utf-8"))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("source_budget_exceeded")
        module = ".".join(PurePosixPath(path).with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module[:-9]
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, TypeError, RecursionError):
            raise ValueError("invalid_python_source") from None
        parsed[path] = (module, tree)
    return parsed


def _package(module):
    return module == PACKAGE or module.startswith(PACKAGE + ".")


def _record(kind, path=None, module=None, function=None, old=None, new=None):
    # Do not return source text, dynamic literal values, exception messages, or
    # absolute paths, even when they occur in rejected input.
    return {"kind": kind, "path": path, "module": module, "function": function,
            "old_ast_sha256": old, "new_ast_sha256": new}


def _top_definitions(parsed):
    result = {name: [] for name in PROTECTED}
    for path, (module, tree) in parsed.items():
        if not _package(module):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in result:
                result[node.name].append((path, module, node))
    return result


def _relative_import(module, path, node):
    if not node.level:
        return node.module or ""
    package = module if path.endswith("/__init__.py") else module.rsplit(".", 1)[0]
    components = package.split(".")
    remove = node.level - 1
    if remove:
        components = components[:-remove]
    return ".".join([*components, *([node.module] if node.module else [])])


class _Bindings:
    """Recognize protected namespace writes, including common indirect forms."""
    def __init__(self, path, module, tree, module_names):
        self.path, self.module, self.tree = path, module, tree
        self.module_names = set(module_names) | {PACKAGE}
        self.aliases, self.parents = {}, {}
        self.is_package = _package(module)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
            if isinstance(parent, ast.Import):
                for entry in parent.names:
                    self.aliases[entry.asname or entry.name.split(".")[0]] = entry.name if entry.asname else entry.name.split(".")[0]
                    if _package(entry.name):
                        self.module_names.add(entry.name)
            elif isinstance(parent, ast.ImportFrom):
                origin = _relative_import(module, path, parent)
                for entry in parent.names:
                    if entry.name != "*":
                        self.aliases[entry.asname or entry.name] = origin + "." + entry.name
        # Follow aliases of modules/protected function objects; do not evaluate.
        for _ in range(4):
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = self.reference(node.value)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if value:
                        for target in targets:
                            if isinstance(target, ast.Name) and self.aliases.get(target.id) != value:
                                self.aliases[target.id] = value
                                changed = True
            if not changed:
                break

    def scope(self, node):
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return current
            current = self.parents.get(current)
        return None

    def globals(self, scope):
        if scope is None:
            return set()
        return {name for node in ast.walk(scope) if isinstance(node, ast.Global) and self.scope(node) is scope for name in node.names}

    def global_name(self, node, name):
        scope = self.scope(node)
        return scope is None or name in self.globals(scope)

    def local_name(self, node, name):
        scope = self.scope(node)
        if scope is None or name in self.globals(scope):
            return False
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs]
            if scope.args.vararg:
                arguments.append(scope.args.vararg)
            if scope.args.kwarg:
                arguments.append(scope.args.kwarg)
            if any(arg.arg == name for arg in arguments):
                return True
        return any(isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id == name
                   and self.scope(item) is scope for item in ast.walk(scope))

    def literal(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id == "__name__" and self.is_package:
            return self.module
        return None

    def reference(self, node):
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.aliases[node.id]
            if node.id in _BUILTINS:
                return "builtins." + node.id
            if self.is_package and node.id in PROTECTED and not self.local_name(node, node.id):
                return self.module + "." + node.id
        elif isinstance(node, ast.Attribute):
            base = self.reference(node.value)
            return base + "." + node.attr if base else None
        elif isinstance(node, ast.Subscript) and self.reference(node.value) == "sys.modules":
            value = self.literal(node.slice)
            return value if value and _package(value) else None
        elif isinstance(node, ast.Call):
            name = self.reference(node.func)
            if name == "builtins.globals" and self.is_package:
                return self.module + ".__dict__"
            if name == "builtins.locals" and self.is_package and self.scope(node) is None:
                return self.module + ".__dict__"
            if name == "builtins.vars" and node.args:
                owner = self.reference(node.args[0])
                return owner + ".__dict__" if owner else None
            if name == "builtins.getattr" and len(node.args) >= 2:
                owner, attribute = self.reference(node.args[0]), self.literal(node.args[1])
                return owner + "." + attribute if owner and attribute else None
            if ((isinstance(node.func, ast.Name) and node.func.id == "__import__")
                    or name in {"importlib.import_module", "builtins.__import__"}):
                value = self.literal(node.args[0]) if node.args else None
                return value if value and _package(value) else None
        return None

    def targets_for_reference(self, reference):
        if not reference or not reference.startswith(PACKAGE + "."):
            return set()
        pieces = reference.split(".")
        return {name for name in PROTECTED if name in pieces[1:]}

    def dictionary_owner(self, node):
        reference = self.reference(node)
        if reference and reference.endswith(".__dict__"):
            return reference[:-9]
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            return self.reference(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "vars" and node.args:
                return self.reference(node.args[0])
            if node.func.id == "globals" and self.is_package:
                return self.module
            if node.func.id == "locals" and self.is_package and self.scope(node) is None:
                return self.module
        return None

    def named_write(self, owner, attribute):
        function_targets = self.targets_for_reference(owner)
        if function_targets:
            return function_targets
        if owner in self.module_names:
            return {attribute} if attribute in PROTECTED else set(PROTECTED) if attribute is None else set()
        return set()

    def assignment(self, node):
        if isinstance(node, ast.Name):
            if self.is_package and node.id in PROTECTED and self.global_name(node, node.id):
                return {node.id}
        if isinstance(node, ast.Attribute):
            direct = self.targets_for_reference(self.reference(node))
            return direct | self.named_write(self.reference(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            owner = self.dictionary_owner(node.value)
            if owner:
                return self.named_write(owner, self.literal(node.slice))
            if self.reference(node.value) == "sys.modules":
                key = self.literal(node.slice)
                if key and _package(key) or key is None and self.is_package:
                    return set(PROTECTED)
        if isinstance(node, (ast.Tuple, ast.List)):
            return set().union(*(self.assignment(part) for part in node.elts))
        return set()

    @staticmethod
    def arg(node, index, *names):
        if len(node.args) > index:
            return node.args[index]
        return next((entry.value for entry in node.keywords if entry.arg in names), None)

    def call(self, node):
        ref = self.reference(node.func)
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if ref in {"builtins." + builtin for builtin in _BUILTINS}:
            name = ref.rsplit(".", 1)[1]
        if name in {"exec", "eval"}:
            return set(PROTECTED), "opaque_dynamic_execution"
        if name in {"setattr", "delattr"}:
            owner_node = self.arg(node, 0, "target", "object", "obj")
            attribute = self.literal(self.arg(node, 1, "attribute", "name"))
            qualified = self.literal(owner_node)
            targets = self.targets_for_reference(qualified) if qualified else set()
            return targets | self.named_write(self.reference(owner_node), attribute), "dynamic_rebinding"
        if ref and (ref == "unittest.mock.patch" or ref.endswith(".patch")):
            return self.targets_for_reference(self.literal(self.arg(node, 0, "target"))), "monkeypatch_rebinding"
        if ref and ref.endswith(".patch.object"):
            owner = self.reference(self.arg(node, 0, "target"))
            attribute = self.literal(self.arg(node, 1, "attribute"))
            return self.named_write(owner, attribute), "monkeypatch_rebinding"
        if isinstance(node.func, ast.Attribute):
            owner = self.dictionary_owner(node.func.value)
            if owner and node.func.attr in {"update", "setdefault", "pop", "clear", "__setitem__", "__delitem__"}:
                if node.func.attr == "clear":
                    return self.named_write(owner, None), "namespace_dictionary_rebinding"
                if node.func.attr == "update":
                    targets = set()
                    for argument in node.args:
                        if isinstance(argument, ast.Dict):
                            for key in argument.keys:
                                targets |= self.named_write(owner, self.literal(key))
                        else:
                            targets |= self.named_write(owner, None)
                    for keyword in node.keywords:
                        targets |= self.named_write(owner, keyword.arg)
                    return targets, "namespace_dictionary_rebinding"
                return self.named_write(owner, self.literal(self.arg(node, 0, "key"))), "namespace_dictionary_rebinding"
        return set(), None

    def events(self):
        result = []
        for node in ast.walk(self.tree):
            targets, kind = set(), None
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                items = node.targets if isinstance(node, ast.Assign) else [node.target]
                targets = set().union(*(self.assignment(item) for item in items))
                kind = "namespace_assignment"
            elif isinstance(node, ast.Delete):
                targets = set().union(*(self.assignment(item) for item in node.targets))
                kind = "namespace_deletion"
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                targets, kind = self.assignment(node.target), "namespace_loop_binding"
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                targets = set().union(*(self.assignment(item.optional_vars) for item in node.items if item.optional_vars))
                kind = "namespace_context_binding"
            elif isinstance(node, ast.ExceptHandler) and node.name in PROTECTED:
                if self.is_package and self.global_name(node, node.name):
                    targets, kind = {node.name}, "namespace_exception_binding"
            elif isinstance(node, ast.ClassDef) and node.name in PROTECTED:
                if self.is_package and self.global_name(node, node.name):
                    targets, kind = {node.name}, "namespace_class_binding"
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and self.is_package:
                for entry in node.names:
                    binding = entry.asname or entry.name.split(".")[0]
                    if binding in PROTECTED and self.global_name(node, binding):
                        targets.add(binding)
                    if entry.name == "*":
                        targets |= set(PROTECTED)
                kind = "namespace_import_binding"
            elif isinstance(node, ast.Call):
                targets, kind = self.call(node)
            if targets:
                scope = self.scope(node)
                scope_name = type(scope).__name__ + ":" + getattr(scope, "name", "lambda") if scope else "module"
                digest = ast_sha256(node)
                result.extend((scope_name, kind, name, digest) for name in sorted(targets))
        return result


def _evaluate_source_seed(original_sources, candidate_sources):
    """Return a hash-only static receipt; never infer that a source run passed."""
    receipt = {"schema": 1, "kind": "source_seed_leakage_guard", "eligible": False,
               "targets": [], "violations": [], "omitted_violations": 0, "source_success_verified_here": False,
               "requires_independent_verified_source_run": True,
               "requires_transfer_feature_failures": True}
    for label, values in (("original", original_sources), ("candidate", candidate_sources)):
        try:
            parsed = _sources(values)
        except (ValueError, UnicodeError, RecursionError):
            receipt["violations"].append(_record("invalid_" + label + "_sources"))
            return receipt
        if label == "original":
            original = parsed
        else:
            candidate = parsed
    before, after = _top_definitions(original), _top_definitions(candidate)
    for name in PROTECTED:
        old_entries, new_entries = before[name], after[name]
        old_entry = old_entries[0] if len(old_entries) == 1 else None
        matching = [entry for entry in new_entries if old_entry and entry[0] == old_entry[0]]
        new_entry = matching[0] if len(matching) == 1 else None
        path, module = old_entry[:2] if old_entry else (None, None)
        old_hash = ast_sha256(old_entry[2]) if old_entry else None
        new_hash = ast_sha256(new_entry[2]) if new_entry else None
        receipt["targets"].append({"path": path, "module": module, "function": name,
                                   "old_ast_sha256": old_hash, "new_ast_sha256": new_hash})
        if old_entry is None:
            receipt["violations"].append(_record("missing_or_ambiguous_original_definition", path, module, name, old_hash, new_hash))
        elif new_entry is None or len(new_entries) != 1:
            receipt["violations"].append(_record("missing_or_duplicate_candidate_definition", path, module, name, old_hash, new_hash))
        elif old_hash != new_hash:
            receipt["violations"].append(_record("protected_definition_changed", path, module, name, old_hash, new_hash))
    module_names = {module for module, tree in [*original.values(), *candidate.values()] if _package(module)}
    for path, (module, tree) in candidate.items():
        old_tree = original.get(path, (None, None))[1]
        old_events = set(_Bindings(path, module, old_tree, module_names).events()) if old_tree else set()
        for scope, kind, name, digest in _Bindings(path, module, tree, module_names).events():
            if (scope, kind, name, digest) not in old_events:
                receipt["violations"].append(_record(kind, path, module, name, None, digest))
        if _package(module):
            old_named = {ast_sha256(node) for node in ast.walk(old_tree)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROTECTED} if old_tree else set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROTECTED and node not in tree.body:
                    digest = ast_sha256(node)
                    if digest not in old_named:
                        receipt["violations"].append(_record("new_nested_target_definition", path, module, node.name, None, digest))
    # Stable de-duplication keeps the receipt reproducible and bounded.
    seen, unique = set(), []
    for row in receipt["violations"]:
        identity = tuple(row.items())
        if identity not in seen:
            seen.add(identity)
            unique.append(row)
    receipt["violations"] = unique[:MAX_VIOLATIONS]
    receipt["omitted_violations"] = max(0, len(unique) - MAX_VIOLATIONS)
    receipt["eligible"] = not unique
    return receipt


def evaluate_source_seed(original_sources, candidate_sources):
    """Return a bounded static receipt; malformed/deep input cannot approve a seed."""
    try:
        return _evaluate_source_seed(original_sources, candidate_sources)
    except (RecursionError, ValueError, UnicodeError):
        return {"schema": 1, "kind": "source_seed_leakage_guard", "eligible": False,
                "targets": [], "violations": [_record("source_analysis_failed")],
                "omitted_violations": 0, "source_success_verified_here": False,
                "requires_independent_verified_source_run": True,
                "requires_transfer_feature_failures": True}
