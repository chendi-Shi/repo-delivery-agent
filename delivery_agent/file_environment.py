"""Source-editing backend: generated commands are parsed, never host-executed.

Useful for inference when Docker is unavailable. Test execution is deliberately
deferred to a separate Docker verifier; this backend cannot claim test success.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path, PureWindowsPath


FILE_GUIDE = """Use only these source-editing commands through bash:
delivery-file list [relative-directory]
delivery-file read relative-path [start-line] [line-count]
delivery-file search 'literal query' [relative-path]
delivery-file write relative-path <<'DELIVERY'
full new file text
DELIVERY
delivery-file append relative-path <<'DELIVERY'
additional file text
DELIVERY
delivery-file replace relative-path <<'DELIVERY'
exact old text
---REPLACE---
new text
DELIVERY
One command per call. Replacement must match exactly once. Never edit .git or escape
the workspace. Shell/python/test commands are unavailable; independent tests run later
in Docker. To submit use exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
"""


class FileEnvironment:
    def __init__(self, workspace, image="deferred", timeout=30):
        self.workspace = Path(workspace).resolve()
        self.image_id, self.timeout, self.closed = image, timeout, False
        self.container_id = None

    def _path(self, value):
        if not isinstance(value, str) or not value or ':' in value or '\\' in value or PureWindowsPath(value).is_absolute():
            raise ValueError("Use portable relative source paths without devices or alternate streams")
        devices = {'con', 'prn', 'aux', 'nul', *('com'+str(n) for n in range(1, 10)), *('lpt'+str(n) for n in range(1, 10))}
        for part in Path(value).parts:
            if part.casefold().rstrip(' .') == '.git' or part.split('.')[0].casefold() in devices:
                raise ValueError("Forbidden source path")
            if part not in {'.', '..'} and part.endswith((' ', '.')):
                raise ValueError("Ambiguous portable source path")
        path = self.workspace / value
        if Path(value).is_absolute() or any(part.casefold() == ".git" for part in path.relative_to(self.workspace).parts):
            raise ValueError("Forbidden source path")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("Path escapes workspace")
        if any(part.is_symlink() for part in [path, *list(path.parents)[:len(path.relative_to(self.workspace).parts)]]):
            raise ValueError("Symlink source paths are prohibited")
        return resolved

    @staticmethod
    def _read(path):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Source file exceeds 2 MiB")
        return path.read_text(encoding="utf-8")

    def execute(self, action, cwd="", *, timeout=None):
        if self.closed:
            raise RuntimeError("Source session is closed")
        command = action.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 100000:
            raise ValueError("Expected a bounded command")
        if command.strip() == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT":
            from minisweagent.exceptions import Submitted
            raise Submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": ""}})
        try:
            edit = re.fullmatch(r"delivery-file (write|append|replace) ([^\s]+) <<'([A-Za-z][A-Za-z0-9_]*)'\n(.*)\n\3\s*", command, re.S)
            if edit:
                operation, name, _, body = edit.groups()
                path = self._path(name)
                if operation == "replace":
                    old, marker, new = body.partition("\n---REPLACE---\n")
                    text = self._read(path)
                    if not marker or not old or text.count(old) != 1:
                        raise ValueError("Replacement must have exactly one matching old fragment")
                    text = text.replace(old, new, 1)
                elif operation == "append":
                    text = self._read(path) + body + "\n"
                else:
                    text = body + "\n"
                if len(text.encode()) > 2 * 1024 * 1024:
                    raise ValueError("Source file exceeds 2 MiB")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
                output = f"{operation} completed: {name}"
            else:
                tokens = shlex.split(command)
                if not tokens or tokens[0] != "delivery-file":
                    raise ValueError("Only delivery-file source operations are available; no host commands")
                operation = tokens[1] if len(tokens) > 1 else ""
                if operation == "list" and len(tokens) in (2, 3):
                    directory = self._path(tokens[2] if len(tokens) == 3 else ".")
                    output = "\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in directory.iterdir()
                                               if p.name.casefold() != ".git" and not p.is_symlink())[:200])
                elif operation == "read" and len(tokens) in (3, 4, 5):
                    lines = self._read(self._path(tokens[2])).splitlines()
                    start, count = int(tokens[3]) if len(tokens) > 3 else 1, int(tokens[4]) if len(tokens) > 4 else 60
                    if start < 1 or not 1 <= count <= 200:
                        raise ValueError("Read bounds are 1-based and at most 200 lines")
                    output = "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines[start-1:start-1+count], start-1))
                elif operation == "search" and len(tokens) in (3, 4):
                    query = tokens[2]
                    if not query or len(query) > 200:
                        raise ValueError("Search query limit exceeded")
                    candidates = [self._path(tokens[3])] if len(tokens) == 4 else self.workspace.rglob("*.py")
                    hits = []
                    for index, candidate in enumerate(candidates):
                        if index >= 500:
                            break
                        if candidate.is_symlink() or any(part.casefold() == '.git' for part in candidate.relative_to(self.workspace).parts):
                            continue
                        candidate = self._path(candidate.relative_to(self.workspace).as_posix())
                        for number, line in enumerate(self._read(candidate).splitlines(), 1):
                            if query in line:
                                hits.append(f"{candidate.relative_to(self.workspace).as_posix()}:{number}: {line}")
                                if len(hits) >= 40:
                                    break
                        if len(hits) >= 40:
                            break
                    output = "\n".join(hits)
                else:
                    raise ValueError("Invalid delivery-file operation")
            return {"output": output[:16000], "returncode": 0, "exception_info": ""}
        except (ValueError, OSError, UnicodeError, IndexError) as exc:
            return {"output": str(exc), "returncode": 2, "exception_info": type(exc).__name__}

    def get_template_vars(self, **kwargs):
        return {"cwd": "source workspace", "system": "source-editing-only", "image": self.image_id, **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment_type": "delivery_agent.file_environment.FileEnvironment",
                                  "environment": {"host_execution": False, "test_execution": "deferred"}}}}

    def close(self):
        self.closed = True

    cleanup = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
