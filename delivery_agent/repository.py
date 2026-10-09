"""Immutable task contracts and Git patch delivery, separate from agent execution."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")


def git(path, *args, input=None, timeout=120, check=True):
    env = os.environ.copy()
    # Avoid signing, hooks and credential prompts in managed task checkouts.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(path), *args],
        input=input, capture_output=True, timeout=timeout, env=env)
    if check and result.returncode:
        raise RuntimeError("Git operation failed: " + result.stderr.decode("utf-8", "replace")[:2000])
    return result


@dataclass(frozen=True)
class RepoTask:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    image: str
    public_checks: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Task must be an object")
        allowed = {"instance_id", "repo", "base_commit", "problem_statement", "image", "public_checks"}
        if set(value) - allowed:
            raise ValueError("Unexpected task fields (gold answers and hidden tests must stay out of inputs)")
        fields = {k: value.get(k) for k in allowed - {"public_checks"}}
        if any(not isinstance(v, str) or not v for v in fields.values()):
            raise ValueError("Task requires nonempty string fields")
        if not INSTANCE.fullmatch(fields["instance_id"]) or not REPO.fullmatch(fields["repo"]):
            raise ValueError("Invalid instance ID or GitHub owner/repository")
        if not SHA.fullmatch(fields["base_commit"]):
            raise ValueError("base_commit must be a full lowercase 40-character Git SHA")
        if len(fields["problem_statement"]) > 100000:
            raise ValueError("Problem statement exceeds limit")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", fields["image"]):
            raise ValueError("Invalid container image name")
        checks = value.get("public_checks", [])
        if (not isinstance(checks, list) or len(checks) > 8
                or any(not isinstance(c, str) or not c.strip() or len(c) > 4000 for c in checks)):
            raise ValueError("public_checks must contain up to 8 bounded shell commands")
        return cls(**fields, public_checks=tuple(checks))

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def checkout(task, destination, source=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Checkout must use a fresh directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = str(Path(source).resolve()) if source is not None else "https://github.com/" + task.repo + ".git"
    git(destination.parent, "clone", "--no-checkout", "--no-local", "--", source, str(destination))
    # Git commit arguments are validated before invoking checkout.
    git(destination, "checkout", "--detach", task.base_commit)
    actual = git(destination, "rev-parse", "HEAD").stdout.decode().strip()
    if actual != task.base_commit:
        raise ValueError("Resolved commit differs from frozen task")
    return destination


def capture_patch(work, base_commit, max_bytes=8 * 1024 * 1024):
    if not SHA.fullmatch(base_commit):
        raise ValueError("Invalid base commit")
    actual = git(work, "rev-parse", "HEAD").stdout.decode().strip()
    if actual != base_commit:
        raise ValueError("Agent changed HEAD; patch provenance cannot be guaranteed")
    # Include added files; no user checkout is modified, only the isolated clone.
    git(work, "add", "--intent-to-add", "--all")
    patch = git(work, "diff", "--binary", "--no-ext-diff", "--no-textconv", base_commit, "--").stdout
    if len(patch) > max_bytes:
        raise ValueError("Patch exceeds 8 MiB delivery limit")
    names = git(work, "diff", "--name-only", "-z", base_commit, "--").stdout
    files = [p.decode("utf-8", "strict") for p in names.split(b"\0") if p]
    return {"model_patch": patch.decode("utf-8", "strict"), "changed_files": files,
            "patch_sha256": hashlib.sha256(patch).hexdigest(), "patch_bytes": len(patch)}


def apply_fresh(task, patch, destination, source=None):
    work = checkout(task, destination, source)
    if not patch:
        return work
    blob = patch.encode("utf-8")
    git(work, "apply", "--check", "--whitespace=nowarn", "-", input=blob)
    git(work, "apply", "--whitespace=nowarn", "-", input=blob)
    return work


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
