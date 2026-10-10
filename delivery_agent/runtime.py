"""Persistent per-trial Docker session. Generated commands never run on the host."""
from __future__ import annotations

import json
import os
import re
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
        self.preserved = False
        self.memory, self.cpus = memory, cpus
        if not 16 * 1024 ** 2 <= _memory_bytes(memory) <= 16 * 1024 ** 3 or not 0 < _nano_cpus(cpus) <= 32_000_000_000:
            raise ValueError("Docker memory/CPU limits must be bounded")
        self.user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else ""
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
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            # With capabilities dropped, container root cannot write a checkout
            # owned by the Linux host user. Use that identity for bind mounts.
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        metadata = self.workspace / ".git"
        if not metadata.is_dir() or metadata.is_symlink():
            raise ValueError("Session requires an isolated checkout with real Git metadata")
        # Agent can edit source but cannot inject host Git fsmonitor/clean filters,
        # replace repository identity, or point its metadata at another checkout.
        # The owned Linux session lasts until explicit cleanup. A fixed sleep
        # would also charge pre-agent setup time against the agent's wall budget.
        argv += ["--mount", f"type=bind,source={metadata},target=/workspace/.git,readonly",
                 "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp", self.image_id, "sleep", "infinity"]
        try:
            self.container_id = subprocess.run(argv, check=True, capture_output=True, timeout=120).stdout.decode().strip()
        except BaseException as original:
            # Docker may create the named container before its client times out.
            cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise RuntimeError("Startup container termination could not be confirmed") from original
            raise

    def identity(self, base_commit):
        """Persist this in a trusted checkpoint before pausing the session."""
        if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            raise ValueError("base_commit must be a full commit identity")
        return {"schema": 1, "container_id": self.container_id, "image_id": self.image_id,
                "workspace": str(self.workspace), "base_commit": base_commit, "timeout": self.timeout,
                "memory": self.memory, "cpus": self.cpus, "user": self.user}

    @classmethod
    def attach(cls, workspace, identity, *, timeout=None, inspect_timeout=30):
        """Reattach only to the same running, isolated container and checkout.

        No Docker run or model/tool command is issued here. A failed validation
        does not delete a container whose identity has not been confirmed.
        """
        if not isinstance(identity, dict) or identity.get("schema") != 1:
            raise ValueError("invalid saved Docker identity")
        container_id, image_id = identity.get("container_id", ""), identity.get("image_id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", container_id) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("resume requires full immutable container and image identities")
        if not re.fullmatch(r"[0-9a-f]{40}", identity.get("base_commit", "")):
            raise ValueError("resume requires a full base commit identity")
        if not 1 <= inspect_timeout <= 60:
            raise ValueError("inspect_timeout must be between 1 and 60 seconds")
        if not 16 * 1024 ** 2 <= _memory_bytes(identity.get("memory")) <= 16 * 1024 ** 3 or not 0 < _nano_cpus(identity.get("cpus")) <= 32_000_000_000:
            raise ValueError("saved Docker resource limits are not bounded")
        directory = Path(workspace).resolve()
        if str(directory) != identity.get("workspace"):
            raise ValueError("resume workspace differs from the saved identity")
        metadata = directory / ".git"
        if not metadata.is_dir() or metadata.is_symlink():
            raise ValueError("resume requires real Git metadata in the same checkout")
        from .repository import git
        if git(directory, "rev-parse", "HEAD", timeout=inspect_timeout).stdout.decode().strip() != identity.get("base_commit"):
            raise ValueError("resume checkout HEAD differs from the frozen base commit")
        raw = subprocess.run(["docker", "inspect", container_id], check=True,
                             capture_output=True, timeout=inspect_timeout).stdout
        records = json.loads(raw)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("Docker inspect did not identify exactly one container")
        record = records[0]
        host, config = record.get("HostConfig", {}), record.get("Config", {})
        state = record.get("State", {})
        if record.get("Id") != container_id or record.get("Image") != image_id:
            raise ValueError("Docker container or image identity changed")
        if state.get("Running") is not True or state.get("Paused") or state.get("Restarting") or state.get("Dead"):
            raise ValueError("saved Docker container is not running and available")
        if (host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True or
                host.get("Privileged") is not False or host.get("CapAdd") or
                set(host.get("CapDrop") or []) != {"ALL"} or host.get("PidsLimit") != 128):
            raise ValueError("Docker isolation settings differ from the required session")
        if set((record.get("NetworkSettings") or {}).get("Networks") or {}) != {"none"}:
            raise ValueError("Docker has unexpected attached networks")
        security = host.get("SecurityOpt") or []
        if not any(value in {"no-new-privileges", "no-new-privileges:true"} for value in security):
            raise ValueError("Docker no-new-privileges setting is absent")
        if (host.get("Memory") != _memory_bytes(identity.get("memory")) or
                host.get("NanoCpus") != _nano_cpus(identity.get("cpus"))):
            raise ValueError("Docker resource limits differ from the saved session")
        if config.get("User", "") != identity.get("user") or config.get("WorkingDir") != "/workspace":
            raise ValueError("Docker user or working directory differs from the saved session")
        tmpfs = host.get("Tmpfs") or {}
        if set(tmpfs) != {"/tmp"} or "rw" not in tmpfs["/tmp"].split(",") or "size=128m" not in tmpfs["/tmp"].split(","):
            raise ValueError("Docker temporary filesystem settings changed")
        environment = set(config.get("Env") or [])
        if not {"HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"} <= environment:
            raise ValueError("Docker required environment settings changed")
        mounts = record.get("Mounts") or []
        binds = {mount.get("Destination"): mount for mount in mounts if mount.get("Type") == "bind"}
        if set(binds) != {"/workspace", "/workspace/.git"} or len([mount for mount in mounts if mount.get("Type") == "bind"]) != 2:
            raise ValueError("Docker bind mount set differs from the required session")
        if any(mount.get("Type") not in {"bind", "tmpfs"} or
               (mount.get("Type") == "tmpfs" and mount.get("Destination") != "/tmp") for mount in mounts):
            raise ValueError("Docker has an unexpected additional mount")
        work, meta = binds["/workspace"], binds["/workspace/.git"]
        if (work.get("RW") is not True or meta.get("RW") is not False or
                not _mount_source_matches(work.get("Source", ""), directory) or
                not _mount_source_matches(meta.get("Source", ""), metadata)):
            raise ValueError("Docker checkout or read-only metadata mount changed")
        obj = cls.__new__(cls)
        obj.workspace, obj.image, obj.image_id = directory, image_id, image_id
        obj.container_id, obj.timeout = container_id, identity["timeout"] if timeout is None else timeout
        if not isinstance(obj.timeout, (int, float)) or not 0 < obj.timeout <= 3600:
            raise ValueError("invalid resume command timeout")
        obj.memory, obj.cpus, obj.user = identity["memory"], identity["cpus"], identity["user"]
        obj.closed, obj.preserved, obj.execution_blocked = False, False, False
        return obj

    def preserve(self):
        """Relinquish execution/cleanup ownership so a new process can resume."""
        if self.closed or getattr(self, "execution_blocked", False):
            raise RuntimeError("cannot preserve a closed or failed session")
        self.preserved, self.execution_blocked = True, True

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

    def close(self, *, force=False):
        # A failed removal must remain visibly unconfirmed, while no further
        # generated command can be launched through this session.
        self.execution_blocked = True
        if getattr(self, "preserved", False) and not force:
            return
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


def _memory_bytes(value):
    if not isinstance(value, str):
        raise ValueError("saved Docker memory limit must be a string")
    match = re.fullmatch(r"([0-9]+)([bkmg]?)", value.lower())
    if not match:
        raise ValueError("unsupported saved Docker memory limit")
    return int(match[1]) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match[2]]


def _nano_cpus(value):
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value)) * 1_000_000_000
        if number <= 0 or number != number.to_integral_value():
            raise ValueError("invalid saved Docker CPU limit")
        return int(number)
    except (InvalidOperation, TypeError, OverflowError) as exc:
        raise ValueError("invalid saved Docker CPU limit") from exc


def _mount_source_matches(source, target):
    if not isinstance(source, str):
        return False
    expected = str(target.resolve())
    if os.name != "nt":
        return source == expected
    source = source.replace("\\", "/").rstrip("/").lower()
    expected = expected.replace("\\", "/").rstrip("/").lower()
    if source == expected:
        return True
    # Docker Desktop may expose the Linux VM's mapped Windows bind source.
    drive = re.fullmatch(r"([a-z]):/(.*)", expected)
    return bool(drive and source in {f"/run/desktop/mnt/host/{drive[1]}/{drive[2]}",
                                     f"/host_mnt/{drive[1]}/{drive[2]}"})
