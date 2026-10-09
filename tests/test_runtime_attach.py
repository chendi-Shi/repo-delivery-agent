"""Resume identity checks using controlled Docker inspect records."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery_agent.runtime import DockerSession

CONTAINER = "c" * 64
IMAGE = "sha256:" + "a" * 64
COMMIT = "b" * 40


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = (Path(self.temporary.name) / "workspace").resolve()
        (self.workspace / ".git").mkdir(parents=True)
        user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") and hasattr(os, "getgid") else ""
        self.identity = {"schema": 1, "container_id": CONTAINER, "image_id": IMAGE,
                         "workspace": str(self.workspace), "base_commit": COMMIT, "timeout": 30,
                         "memory": "1g", "cpus": "2", "user": user}
        self.record = {"Id": CONTAINER, "Image": IMAGE,
                       "NetworkSettings": {"Networks": {"none": {}}},
                       "State": {"Running": True, "Paused": False, "Restarting": False, "Dead": False},
                       "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                                      "CapAdd": None, "CapDrop": ["ALL"], "PidsLimit": 128,
                                      "SecurityOpt": ["no-new-privileges"], "Memory": 1073741824,
                                      "NanoCpus": 2000000000, "Tmpfs": {"/tmp": "rw,size=128m"}},
                       "Config": {"User": user, "WorkingDir": "/workspace",
                                  "Env": ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"]},
                       "Mounts": [{"Type": "bind", "Source": str(self.workspace), "Destination": "/workspace", "RW": True},
                                  {"Type": "bind", "Source": str(self.workspace / ".git"), "Destination": "/workspace/.git", "RW": False}]}
        self.calls = []

        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([self.record]).encode(), b"")
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            raise AssertionError(argv)

        self.patch_run = patch("delivery_agent.runtime.subprocess.run", side_effect=run)
        self.patch_run.start()
        self.addCleanup(self.patch_run.stop)
        self.patch_git = patch("delivery_agent.repository.git", return_value=subprocess.CompletedProcess([], 0, (COMMIT + "\n").encode(), b""))
        self.git = self.patch_git.start()
        self.addCleanup(self.patch_git.stop)

    def test_attach_uses_existing_identity_and_owns_cleanup_without_starting_container(self):
        session = DockerSession.attach(self.workspace, self.identity)
        self.assertEqual(session.identity(COMMIT), self.identity)
        self.assertEqual(self.calls[0][0], ["docker", "inspect", CONTAINER])
        self.assertEqual(self.calls[0][1]["timeout"], 30)
        session.close()
        self.assertEqual(self.calls[-1][0], ["docker", "rm", "-f", CONTAINER])
        self.assertTrue(session.closed)

    def test_preserve_relinquishes_execution_and_cleanup_then_attach_takes_ownership(self):
        first = DockerSession.attach(self.workspace, self.identity)
        first.preserve()
        first.close()
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(RuntimeError):
            first.execute({"command": "echo unsafe"})
        second = DockerSession.attach(self.workspace, self.identity)
        second.close()
        self.assertTrue(second.closed)
        self.assertEqual(sum(argv[:3] == ["docker", "rm", "-f"] for argv, _ in self.calls), 1)

    def test_moved_head_or_workspace_blocks_before_docker_inspection(self):
        for changed in ("workspace", "head"):
            with self.subTest(changed=changed):
                self.calls.clear()
                if changed == "workspace":
                    identity = {**self.identity, "workspace": str(self.workspace.parent)}
                else:
                    identity = self.identity
                    self.git.return_value = subprocess.CompletedProcess([], 0, ("d" * 40).encode(), b"")
                with self.assertRaises(ValueError):
                    DockerSession.attach(self.workspace, identity)
                self.assertEqual(self.calls, [])

    def test_changed_container_isolation_or_mounts_are_rejected_without_cleanup(self):
        original = copy.deepcopy(self.record)
        changes = [
            ("Id", "d" * 64), ("Image", "sha256:" + "e" * 64),
            ("NetworkSettings.Networks", {"none": {}, "bridge": {}}),
            ("State.Running", False), ("State.Paused", True),
            ("HostConfig.NetworkMode", "bridge"), ("HostConfig.ReadonlyRootfs", False),
            ("HostConfig.Privileged", True), ("HostConfig.CapDrop", []),
            ("HostConfig.CapAdd", ["SYS_ADMIN"]), ("HostConfig.PidsLimit", 0),
            ("HostConfig.SecurityOpt", []), ("HostConfig.Memory", 0), ("HostConfig.NanoCpus", 0),
            ("HostConfig.Tmpfs", {"/tmp": "rw,size=4g"}),
            ("Config.WorkingDir", "/"), ("Config.User", "changed-user"), ("Config.Env", []),
        ]
        for key, value in changes:
            with self.subTest(key=key):
                self.record = copy.deepcopy(original)
                target = self.record
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                with self.assertRaises(ValueError):
                    DockerSession.attach(self.workspace, self.identity)
        for altered in ("writable_metadata", "readonly_source", "different_source", "extra_volume", "duplicate_bind"):
            with self.subTest(altered=altered):
                self.record = copy.deepcopy(original)
                if altered == "writable_metadata":
                    self.record["Mounts"][1]["RW"] = True
                elif altered == "readonly_source":
                    self.record["Mounts"][0]["RW"] = False
                elif altered == "different_source":
                    self.record["Mounts"][0]["Source"] = str(self.workspace.parent)
                elif altered == "extra_volume":
                    self.record["Mounts"].append({"Type": "volume", "Destination": "/data", "RW": True})
                else:
                    self.record["Mounts"].append(copy.deepcopy(self.record["Mounts"][0]))
                with self.assertRaises(ValueError):
                    DockerSession.attach(self.workspace, self.identity)
        self.assertFalse(any(argv[:3] == ["docker", "rm", "-f"] for argv, _ in self.calls))

    def test_mutable_names_unbounded_resources_and_invalid_timeout_are_rejected(self):
        for key, value in (("container_id", "named-container"), ("image_id", "python:tag"),
                           ("base_commit", "main"), ("memory", "0"), ("cpus", "0")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                DockerSession.attach(self.workspace, {**self.identity, key: value})
        with self.assertRaises(ValueError):
            DockerSession.attach(self.workspace, self.identity, inspect_timeout=0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
