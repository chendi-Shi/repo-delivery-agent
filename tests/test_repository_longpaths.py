"""Real local Git regression for deep Windows trial roots; no model or network."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from delivery_agent.repository import RepoTask, apply_fresh, capture_patch, checkout, git


class RepositoryLongPathTests(unittest.TestCase):
    def test_deep_init_fetch_checkout_capture_and_fresh_apply(self):
        with tempfile.TemporaryDirectory(prefix="delivery-longpaths-") as temporary, ExitStack() as cleanup:
            root = Path(temporary).resolve()
            repositories = []

            def deep(label):
                path = root / label
                # Python need not support unprefixed paths above MAX_PATH. Git
                # itself appends a loose-object filename to this reachable root.
                while len(str(path)) < 225:
                    remaining = 225 - len(str(path)) - 1
                    if remaining <= 0:
                        break
                    path = path / ("x" * min(48, remaining))
                path.mkdir(parents=True)
                self.assertLess(len(str(path / "workspace" / "module.py")), 260)
                self.assertGreater(len(str(path / ".git" / "objects" / "aa" / ("b" * 38))), 260)
                return path

            def cleanup_git_metadata():
                # Generated Git internals may exceed what this Python build can
                # remove by an ordinary path. Move only these owned .git roots
                # (each itself below MAX_PATH) to the shallow temporary root.
                for index, repository in enumerate(repositories):
                    metadata = repository / ".git"
                    target = root / ("git-cleanup-" + str(index))
                    self.assertTrue(metadata.resolve().is_relative_to(root))
                    self.assertTrue(target.resolve().is_relative_to(root))
                    if metadata.exists():
                        metadata.rename(target)

            # Run before TemporaryDirectory.__exit__, including on test errors.
            cleanup.callback(cleanup_git_metadata)

            source = deep("source")
            repositories.append(source)
            git(source, "init")
            (source / "module.py").write_text("value = 1\n", encoding="utf-8")
            git(source, "add", "module.py")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "deep source")
            commit = git(source, "rev-parse", "HEAD").stdout.decode().strip()
            task = RepoTask.parse({"instance_id": "fixture__deep-path", "repo": "fixture/deep-path",
                "base_commit": commit, "problem_statement": "Change value and add a file", "image": "python:3.12-slim"})

            # Reproduce the failed production init/remote/fetch sequence with a
            # local immutable source. No credentials or external transport.
            fetched = deep("fetched")
            repositories.append(fetched)
            git(fetched, "init")
            git(fetched, "remote", "add", "origin", str(source))
            git(fetched, "fetch", "--upload-pack=git -c core.longpaths=true upload-pack", "--depth=1", "origin", commit)
            git(fetched, "checkout", "--detach", commit)
            self.assertEqual(git(fetched, "rev-parse", "HEAD").stdout.decode().strip(), commit)
            self.assertEqual((fetched / "module.py").read_text(encoding="utf-8"), "value = 1\n")

            # The actual checkout helper independently fetches the frozen SHA
            # from a trusted local source, as fresh patch grading does.
            workspace = deep("agent-parent") / "workspace"
            repositories.append(workspace)
            work = checkout(task, workspace, source=source)
            (work / "module.py").write_text("value = 2\n", encoding="utf-8")
            (work / "added.py").write_text("from module import value\n", encoding="utf-8")
            delivered = capture_patch(work, commit)
            self.assertEqual(set(delivered["changed_files"]), {"module.py", "added.py"})
            validation = deep("validation-parent") / "workspace"
            repositories.append(validation)
            verified = apply_fresh(task, delivered["model_patch"], validation, source=source)
            self.assertEqual((verified / "module.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((verified / "added.py").read_text(encoding="utf-8"), "from module import value\n")
            self.assertEqual((source / "module.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(git(source, "status", "--porcelain").stdout, b"")


if __name__ == "__main__":
    unittest.main()
