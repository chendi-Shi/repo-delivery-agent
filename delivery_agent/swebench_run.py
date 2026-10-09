"""Real SWE-bench inference through the pinned upstream Docker/model factories.

Only public frozen fields enter the container and model. Grading is an external
step; a submitted patch has no invented applicability or resolution score.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .benchmark import _model_name, _public_task
from .repository import atomic_json
from .swebench_environment import ENVIRONMENT_CLASS
from .upstream import UPSTREAM_COMMIT, UPSTREAM_VERSION, require_upstream, run_agent

_CREDENTIAL = re.compile(r"api.?key|authorization|password|secret|credentials|access.?token|bearer|headers", re.I)
_MODEL_KWARGS = {"temperature", "seed", "max_tokens", "timeout", "num_retries", "drop_params",
                 "parallel_tool_calls", "think", "num_ctx", "api_base"}
_AGENT_FIELDS = {"system_template", "instance_template", "step_limit", "cost_limit",
                 "wall_time_limit_seconds", "max_consecutive_format_errors"}


def _reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _CREDENTIAL.search(key) or key.lower() == "token":
                raise ValueError("credentials must be supplied through provider environment variables")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)


def _validated_config(config):
    if not isinstance(config, dict) or set(config) - {"model", "agent", "environment"}:
        raise ValueError("config accepts model, agent and environment sections only")
    _reject_credentials(config)
    for section in ("model", "agent", "environment"):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"{section} configuration must be an object")
    model = copy.deepcopy(config.get("model", {}))
    if set(model) - {"model_name", "model_class", "model_kwargs", "cost_tracking"}:
        raise ValueError("unknown model configuration fields")
    model["model_name"] = _model_name(model.get("model_name"))
    if model.get("model_class", "litellm") != "litellm":
        raise ValueError("real SWE-bench runs require the litellm provider, not a fixture model")
    model["model_class"] = "litellm"
    kwargs = model.get("model_kwargs", {})
    if not isinstance(kwargs, dict) or set(kwargs) - _MODEL_KWARGS:
        raise ValueError("unsupported model_kwargs; credentials and arbitrary fields are prohibited")
    for key, value in kwargs.items():
        if key == "api_base":
            if not isinstance(value, str):
                raise ValueError("api_base must be an HTTP endpoint")
            parts = urlsplit(value)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                    or parts.password or parts.query or parts.fragment):
                raise ValueError("api_base cannot contain credentials or query strings")
        elif not isinstance(value, (bool, int, float)) or (isinstance(value, float) and not float("-inf") < value < float("inf")):
            raise ValueError("model kwargs must contain only finite numeric or boolean settings")
    if model.get("cost_tracking", "default") not in {"default", "ignore_errors"}:
        raise ValueError("invalid cost_tracking mode")
    agent = copy.deepcopy(config.get("agent", {}))
    if set(agent) - _AGENT_FIELDS:
        raise ValueError("unsupported agent configuration fields")
    for key, value in agent.items():
        if key.endswith("_template"):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("agent templates must be nonempty strings")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not value < float("inf"):
            raise ValueError("agent limits must be finite nonnegative numbers")
    supplied_env = config.get("environment", {})
    if set(supplied_env) - {"timeout", "pull_timeout"}:
        raise ValueError("SWE-bench environment accepts timeouts only; image/workdir are derived")
    for key, value in supplied_env.items():
        if type(value) is not int or not 0 < value <= 3600:
            raise ValueError("environment timeouts must be positive integers up to 3600")
    environment = {"environment_class": ENVIRONMENT_CLASS, "cwd": "/testbed", "timeout": 60, "pull_timeout": 120,
                   "interpreter": ["bash", "-c"], "executable": "docker", "forward_env": [],
                   "env": {"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off",
                           "TQDM_DISABLE": "1", "BASH_ENV": "/root/.bashrc"},
                   "run_args": ["--rm", "--network", "none", "--memory", "4g", "--cpus", "2",
                                "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"],
                   **supplied_env}
    return {"model": model, "agent": agent, "environment": environment, "run": {}}


def _load_factories():
    require_upstream()
    from minisweagent.run.benchmarks.swebench import get_sb_environment, get_model, get_swebench_docker_image_name

    def controlled_environment(config, instance):
        effective = copy.deepcopy(config)
        # The upstream helper injects images only for its shortcut class names.
        # Derive the same official image when selecting our full class path.
        effective["environment"]["environment_class"] = ENVIRONMENT_CLASS
        effective["environment"]["image"] = get_swebench_docker_image_name(instance)
        return get_sb_environment(effective, instance)

    return controlled_environment, get_model


def _attach_cleanup(env):
    container_id = getattr(env, "container_id", None)
    valid = isinstance(container_id, str) and re.fullmatch(r"[a-f0-9]{12,64}", container_id)

    if getattr(env, "synchronous_cleanup", False):
        if not valid:
            raise RuntimeError("controlled environment did not return a valid task container identity")
        return

    def cleanup():
        if env.container_id is None:
            return
        if not valid:
            raise RuntimeError("upstream did not return a valid task container identity")
        result = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("task container cleanup could not be confirmed")
        env.container_id = None

    # Replace the upstream shell/background cleanup, including destructor calls.
    env.cleanup = cleanup
    if not valid:
        raise RuntimeError("upstream did not return a valid task container identity")


def _execute(env, command):
    result = env.execute({"command": command}, cwd="/testbed")
    if (not isinstance(result, dict) or type(result.get("returncode")) is not int
            or result["returncode"] != 0 or not isinstance(result.get("output"), str)):
        raise RuntimeError("task repository inspection failed")
    return result["output"]


def run_swebench_instance(instance, config, output_dir, profile="retrieval", context_tokens=16384,
                          reserve_tokens=3048):
    """Run one public instance and persist an immutable result, even on failure.

    The original private dataset file is neither loaded nor mounted. No hidden
    tests are run here; official grading must consume the exported prediction.
    """
    public = _public_task(instance, strict=True)
    effective = _validated_config(config)
    if profile not in {"baseline", "recent", "retrieval"}:
        raise ValueError("unknown context profile")
    if (type(context_tokens) is not int or type(reserve_tokens) is not int
            or not 0 < reserve_tokens < context_tokens):
        raise ValueError("context budget must exceed a positive reserved budget")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError("trial output must be a fresh directory")
    output_dir.mkdir(parents=True)
    config_bytes = json.dumps(config, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    safe_config = {"model": effective["model"],
                   "agent_limits": {key: value for key, value in effective["agent"].items() if not key.endswith("_template")},
                   "agent_template_sha256": {key: hashlib.sha256(value.encode()).hexdigest()
                                             for key, value in effective["agent"].items() if key.endswith("_template")},
                   "environment": effective["environment"]}
    provenance = {"repo": public["repo"], "base_commit": public["base_commit"],
                  "profile": profile, "context_tokens": context_tokens, "reserve_tokens": reserve_tokens,
                  "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
                  "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "config": safe_config,
                  "task_sha256": hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest(),
                  "resolved_commit": None, "resolved_image": None}
    atomic_json(output_dir / "task.json", public)
    atomic_json(output_dir / "spec.json", provenance)
    result = {"instance_id": public["instance_id"], "model_name_or_path": effective["model"]["model_name"],
              "provider": "litellm", "fixture": False, "status": "failed", "exit_status": None,
              "model_patch": "", "patch_valid": None, "official_resolved": None,
              "changed_files": [], "public_checks": [], "provenance": provenance}
    env, started = None, time.monotonic()
    try:
        get_environment, get_model = _load_factories()
        env = get_environment(effective, public)
        _attach_cleanup(env)
        image = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", env.container_id],
                               capture_output=True, check=True, timeout=20).stdout.decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("task image identity could not be frozen")
        provenance["resolved_image"] = image
        provenance["image"] = getattr(getattr(env, "config", None), "image", None)
        before = _execute(env, "git rev-parse HEAD").strip()
        if before != public["base_commit"]:
            raise RuntimeError("task image HEAD differs from the frozen base commit")
        provenance["resolved_commit"] = before
        if _execute(env, "git status --porcelain").strip():
            raise RuntimeError("task image contains preexisting working tree changes")
        model = get_model(config=effective["model"])
        if getattr(getattr(model, "config", None), "model_name", None) != result["model_name_or_path"]:
            raise RuntimeError("resolved model differs from frozen configuration")
        outcome = run_agent(model, env, task=public["problem_statement"], agent_config=effective["agent"],
                            output_dir=output_dir / "agent", profile=profile,
                            context_tokens=context_tokens, reserve_tokens=reserve_tokens)
        if not isinstance(outcome, dict) or not isinstance(outcome.get("exit_status"), str):
            raise RuntimeError("agent returned an invalid outcome")
        result["exit_status"] = outcome["exit_status"]
        atomic_json(output_dir / "outcome.json", {"exit_status": outcome["exit_status"]})
        after = _execute(env, "git rev-parse HEAD").strip()
        provenance["final_commit"] = after
        if after != public["base_commit"]:
            raise RuntimeError("agent changed HEAD; patch provenance is invalid")
        _execute(env, "git add -N .")
        patch = _execute(env, "git diff --binary --no-ext-diff --no-textconv HEAD --")
        if len(patch.encode()) > 8 * 1024 * 1024:
            raise RuntimeError("delivery patch exceeds 8 MiB")
        names = _execute(env, "git diff --name-only -z HEAD --")
        result.update(model_patch=patch, changed_files=[name for name in names.split("\x00") if name],
                      patch_sha256=hashlib.sha256(patch.encode()).hexdigest(), patch_bytes=len(patch.encode()),
                      status="submitted" if outcome["exit_status"] == "Submitted" else "stopped")
        (output_dir / "model.patch").write_text(patch, encoding="utf-8", newline="\n")
    except Exception as exc:
        result["status"], result["error_type"] = "failed", type(exc).__name__
        if result["exit_status"] is None:
            result["exit_status"] = type(exc).__name__
        atomic_json(output_dir / "outcome.json", {"exit_status": result["exit_status"], "error_type": type(exc).__name__})
    finally:
        if env is not None:
            try:
                env.cleanup()
                result["cleanup_confirmed"] = getattr(env, "container_id", None) is None
            except Exception as exc:
                result["cleanup_confirmed"] = False
                result["cleanup_error_type"] = type(exc).__name__
                result["status"] = "failed"
        result["seconds"] = time.monotonic() - started
        atomic_json(output_dir / "spec.json", provenance)
        atomic_json(output_dir / "result.json", result)
    return result
