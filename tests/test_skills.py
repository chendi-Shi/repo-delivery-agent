import tempfile
import unittest
from pathlib import Path

from delivery_agent.skills import SkillRegistry, parse_frontmatter


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "iterator-fix"
        self.directory.mkdir()
        (self.directory / "SKILL.md").write_text("---\nname: iterator-fix\ndescription: >-\n  Fix iterator bugs\n  with single-pass checks.\n---\nDetailed body not in discovery.\n", encoding="utf-8")
        (self.directory / "references").mkdir()
        (self.directory / "references" / "check.md").write_text("evidence" * 1000, encoding="utf-8")

    def test_discovery_is_summary_only_and_read_is_bounded_with_hashes(self):
        registry = SkillRegistry([self.root])
        summary = registry.list()["skills"][0]
        self.assertEqual(summary["description"], "Fix iterator bugs with single-pass checks.")
        self.assertNotIn("Detailed body", str(summary))
        first = registry.read(summary["source_id"], "references/check.md", 0, 101)
        next_page = registry.read("iterator-fix", "references/check.md", first["next_offset"], 101)
        self.assertEqual(first["text"] + next_page["text"], ("evidence" * 1000)[:202])
        self.assertEqual(len(first["sha256"]), 64)

    def test_changes_after_freeze_and_path_escape_are_rejected(self):
        registry = SkillRegistry([self.root])
        for path in ("../secret", "/etc/passwd", "references\\check.md", "C:/secret", "./SKILL.md", "missing.py"):
            with self.assertRaises(ValueError):
                registry.read("iterator-fix", path)
        (self.directory / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            registry.read("iterator-fix")

    def test_duplicate_names_bad_metadata_and_size_limits(self):
        for metadata in ("name: UPPER\ndescription: text", "name: x\ndescription: !tag value",
                         "name: x\nname: y\ndescription: text", "name: x\ndescription: {object: value}"):
            with self.assertRaises(ValueError):
                parse_frontmatter("---\n" + metadata + "\n---\nbody")
        (self.directory / "huge.txt").write_text("x" * 65537, encoding="utf-8")
        with self.assertRaises(ValueError):
            SkillRegistry([self.root])

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        link = self.directory / "escape.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            return  # Windows may require developer-mode/privilege for symlinks.
        with self.assertRaises(ValueError):
            SkillRegistry([self.root])

    def test_builtin_skills_are_valid_and_supporting_resource_is_discoverable(self):
        registry = SkillRegistry()
        self.assertEqual(set(registry.skills), {"python-regression", "patch-delivery"})
        read = registry.read("python-regression", limit=4000)
        self.assertIn("references/iterators.md", read["resources"])
        self.assertIn("one-shot", registry.read("python-regression", "references/iterators.md")["text"].lower())


if __name__ == "__main__":
    unittest.main()
