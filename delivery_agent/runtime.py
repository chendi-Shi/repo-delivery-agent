"""Persistent per-trial Docker session. Generated commands never run on the host."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


class DockerSession:
    def __init__(self, workspace, image, timeout=30, memory="1g", cpus="2"):
        self.workspace = Path(workspace).resolve()
        self.image, self.timeout = image, timeout
        self.container_id = None
        self.closed = False
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                       check=True, capture_output=True, timeout=60)
        self.image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                       check=True, capture_output=True, timeout=60).stdout.decode().strip()
        if not self.image_id.startswith("sha256:"):
            raise ValueError("Docker image could not be resolved to immutable identity")
        name = "delivery-agent-" + uuid.uuid4().hex[:16]
        argv = ["docker", "run", "-d", "--name", name, "--network", "none", "--memory", memory,
                "--cpus", cpus, "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt",
                "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,size=128m",
                "--mount", f"type=bind,source={self.workspace},target=/workspace", "-w", "/workspace"]
        metadata = self.workspace / ".git"
        if not metadata.is_dir() or metadata.is_symlink():
            raise ValueError("Session requires an isolated checkout with real Git metadata")
        # Agent can edit source but cannot inject host Git fsmonitor/clean filters,
        # replace repository identity, or point its metadata at another checkout.
        argv += ["--mount", f"type=bind,source={metadata},target=/workspace/.git,readonly",
                 "-e", "PYTHONDONTWRITEBYTECODE=1", self.image_id, "sleep", "3600"]
        try:
            self.container_id = subprocess.run(argv, check=True, capture_output=True, timeout=120).stdout.decode().strip()
        except BaseException as original:
            # Docker may create the named container before its client times out.
            cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise RuntimeError("Startup container termination could not be confirmed") from original
            raise

    def get_template_vars(self, **kwargs):
        return {"cwd": "/workspace", "system": "Linux", "image": self.image_id, **kwargs}

    def serialize(self):
        return {"info": {"config": {"environment": {"image": self.image_id, "cwd": "/workspace",
                "network": "none", "timeout": self.timeout}, "environment_type": "delivery_agent.runtime.DockerSession"}}}

    def execute(self, action, cwd="", *, timeout=None):
        if self.closed or getattr(self, "execution_blocked", False):
            raise RuntimeError("Container session is closed")
        command = action.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 100000:
            raise ValueError("Expected a bounded command string")
        started = time.monotonic()
        argv = ["docker", "exec", "-w", cwd or getattr(self, "cwd", "/workspace")]
        for key, value in getattr(self, "command_env", {}).items():
            argv.extend(["-e", f"{key}={value}"])
        argv.extend([self.container_id, *getattr(self, "interpreter", ["sh", "-c"]), command])
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT)
            deadline = started + (timeout or self.timeout)
            overflow = False
            timed_out = False
            while process.poll() is None:
                if output.tell() > 8 * 1024 * 1024:
                    overflow = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.05)
            if timed_out or overflow:
                # Killing docker exec alone does not stop its container process.
                # Terminate the entire session, then refuse further execution.
                self.execution_blocked = True
                try:
                    process.kill()
                    process.wait(timeout=5)
                finally:
                    self.close()
                raise RuntimeError("Command timeout; session stopped" if timed_out else "Output limit exceeded; session stopped")
            output.seek(0, os.SEEK_END)
            length = output.tell()
            if length > 8 * 1024 * 1024:
                self.execution_blocked = True
                self.close()
                raise RuntimeError("Output limit exceeded; session stopped")
            output.seek(0)
            raw = output.read().decode("utf-8", "replace")
        result = {"output": raw, "returncode": process.returncode, "exception_info": ""}
        lines = raw.lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and process.returncode == 0:
            from minisweagent.exceptions import Submitted
            raise Submitted({"role": "exit", "content": "".join(lines[1:]),
                "extra": {"exit_status": "Submitted", "submission": "".join(lines[1:])}})
        return result

    def close(self):
        # A failed removal must remain visibly unconfirmed, while no further
        # generated command can be launched through this session.
        self.execution_blocked = True
        if not self.closed and self.container_id:
            result = subprocess.run(["docker", "rm", "-f", self.container_id],
                                    capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError("Container termination could not be confirmed")
            self.closed = True

    cleanup = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
