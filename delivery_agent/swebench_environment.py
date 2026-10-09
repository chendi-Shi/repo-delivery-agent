"""Bounded pure-container execution selected by the upstream SWE-bench factory.

No repository or credential directory is mounted. Source edits and Git metadata
remain inside this task's container; patch inspection uses container commands.
"""
from __future__ import annotations

import copy
import re
import subprocess
import uuid
from types import SimpleNamespace

from .runtime import DockerSession


ENVIRONMENT_CLASS = "delivery_agent.swebench_environment.SWEbenchDockerEnvironment"
SAFE_RUN_ARGS = ["--rm", "--network", "none", "--memory", "4g", "--cpus", "2",
                 "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
SAFE_ENV = {"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off",
            "TQDM_DISABLE": "1", "BASH_ENV": "/root/.bashrc"}


class SWEbenchDockerEnvironment(DockerSession):
    """Pinned upstream Environment protocol with synchronous failure cleanup."""

    synchronous_cleanup = True

    def __init__(self, *, image, cwd="/testbed", timeout=60, pull_timeout=120,
                 interpreter=None, executable="docker", forward_env=None, env=None, run_args=None):
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", image):
            raise ValueError("invalid derived SWE-bench image")
        if (cwd != "/testbed" or executable != "docker" or (interpreter is not None and interpreter != ["bash", "-c"])
                or (forward_env is not None and forward_env != [])
                or (env is not None and env != SAFE_ENV) or (run_args is not None and run_args != SAFE_RUN_ARGS)):
            raise ValueError("SWE-bench container security settings cannot be overridden")
        if type(timeout) is not int or type(pull_timeout) is not int or not 0 < timeout <= 3600 or not 0 < pull_timeout <= 3600:
            raise ValueError("container timeouts must be positive integers up to 3600")
        self.cwd, self.timeout = cwd, timeout
        self.interpreter = ["bash", "-c"]
        self.command_env = copy.deepcopy(SAFE_ENV)
        self.image = image
        self.container_id = None
        self.closed = False
        self.config = SimpleNamespace(image=image, cwd=cwd, timeout=timeout, pull_timeout=pull_timeout,
            executable="docker", interpreter=list(self.interpreter), forward_env=[],
            env=copy.deepcopy(SAFE_ENV), run_args=list(SAFE_RUN_ARGS))

        # Freeze the image before starting a task. Pulling happens through the
        # Docker daemon before inference, never from the task container.
        inspection = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                    capture_output=True, timeout=60)
        if inspection.returncode:
            subprocess.run(["docker", "pull", image], capture_output=True, check=True, timeout=pull_timeout)
            inspection = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                        capture_output=True, check=True, timeout=60)
        self.image_id = inspection.stdout.decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id):
            raise RuntimeError("task image identity could not be frozen")
        name = "delivery-swebench-" + uuid.uuid4().hex[:16]
        argv = ["docker", "run", "-d", "--name", name, "-w", self.cwd, *SAFE_RUN_ARGS,
                self.image_id, "sleep", "2h"]
        try:
            identity = subprocess.run(argv, capture_output=True, check=True, timeout=pull_timeout).stdout.decode().strip()
            if not re.fullmatch(r"[a-f0-9]{12,64}", identity):
                raise RuntimeError("Docker did not return a valid container identity")
            self.container_id = identity
        except BaseException as original:
            # Even a killed client may already have created the named container.
            # Keep the name until this cleanup has confirmed termination.
            try:
                cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
            except Exception as cleanup_error:
                raise RuntimeError("Startup container termination could not be confirmed") from cleanup_error
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise RuntimeError("Startup container termination could not be confirmed") from original
            self.closed = True
            raise

    def get_template_vars(self, **kwargs):
        return {"cwd": self.cwd, "system": "Linux", "image": self.image_id, **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment": {
            "image": self.image_id, "image_name": self.image, "cwd": self.cwd,
            "timeout": self.timeout, "network": "none", "forward_env": [],
            "interpreter": list(self.interpreter)}, "environment_type": ENVIRONMENT_CLASS}}}

    def close(self):
        super().close()
        if self.closed:
            self.container_id = None

    cleanup = close
