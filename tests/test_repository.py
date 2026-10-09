import json
import subprocess
import tempfile
import unittest
import os
from unittest.mock import patch
from pathlib import Path

from delivery_agent.repository import RepoTask, apply_fresh, capture_patch, checkout, git


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init")
        (self.source / "module.py").write_text("value = 1\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
        self.commit = git(self.source, "rev-parse", "HEAD").stdout.decode().strip()
        self.task = RepoTask.parse({"instance_id": "owner__repo-1", "repo": "owner/repo",
            "base_commit": self.commit, "problem_statement": "Update the module", "image": "python:3.12-slim"})

    def test_patch_contains_modified_added_and_deleted_files_and_applies_cleanly(self):
        work = checkout(self.task, self.root / "agent", self.source)
        (work / "module.py").write_text("value = 2\n", encoding="utf-8")
        (work / "new.py").write_text("from module import value\n", encoding="utf-8")
        patch = capture_patch(work, self.commit)
        self.assertEqual(set(patch["changed_files"]), {"module.py", "new.py"})
        fresh = apply_fresh(self.task, patch["model_patch"], self.root / "verify", self.source)
        self.assertEqual((fresh / "module.py").read_text(), "value = 2\n")
        self.assertTrue((fresh / "new.py").exists())
        self.assertEqual((self.source / "module.py").read_text(), "value = 1\n")
        (work / "module.py").unlink()
        deleted = capture_patch(work, self.commit)
        again = apply_fresh(self.task, deleted["model_patch"], self.root / "verify-delete", self.source)
        self.assertFalse((again / "module.py").exists())

    def test_rejects_symbolic_ref_and_answer_fields(self):
        from dataclasses import asdict
        value = asdict(self.task)
        value["public_checks"] = []
        for update in ({"base_commit": "main"}, {"repo": "owner/../private"}, {"patch": "answer"},
                       {"image": "python --privileged"}, {"instance_id": "../escape"}):
            with self.assertRaises(ValueError):
                RepoTask.parse(value | update)

    def test_does_not_overwrite_existing_checkout_and_rejects_moved_head(self):
        work = checkout(self.task, self.root / "agent", self.source)
        with self.assertRaises(ValueError):
            checkout(self.task, work, self.source)
        git(work, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "agent moved HEAD")
        with self.assertRaises(ValueError):
            capture_patch(work, self.commit)

    def test_invalid_patch_is_rejected(self):
        with self.assertRaises(RuntimeError):
            apply_fresh(self.task, "not a patch", self.root / "verify", self.source)

    def test_host_global_clean_filter_cannot_execute_on_agent_attributes(self):
        work = checkout(self.task, self.root / "agent", self.source)
        marker = self.root / "filter-executed"
        config = self.root / "unsafe-global-config"
        config.write_text('[filter "danger"]\n    clean = echo unsafe > ' + marker.as_posix() + '\n', encoding="utf-8")
        (work / ".gitattributes").write_text("*.py filter=danger\n", encoding="utf-8")
        (work / "module.py").write_text("value = 2\n", encoding="utf-8")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            delivered = capture_patch(work, self.commit)
        self.assertFalse(marker.exists())
        self.assertIn("value = 2", delivered["model_patch"])


if __name__ == "__main__":
    unittest.main()
