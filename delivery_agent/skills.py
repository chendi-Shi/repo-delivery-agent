"""Read-only Agent Skills with hashed, progressive disclosure.

The dependency-free parser intentionally supports scalar name/description and
literal/folded description blocks, not arbitrary YAML objects or executable tags.
Unknown optional frontmatter fields are ignored. Resources are data; this module
does not execute them or register a shell command supplied by a skill.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

BUILTIN_SKILLS = Path(__file__).parent / "resources" / "skills"
MAX_FILE_BYTES = 65536
MAX_SKILLS = 64
MAX_RESOURCES = 64
MAX_READ_CHARS = 4000


def _scalar(value: str) -> str:
    value = value.strip()
    if not value or value[0] in "!&*[{":
        raise ValueError("required skill metadata must be plain or quoted text")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("double-quoted metadata must use JSON-compatible escapes") from exc
        if not isinstance(parsed, str):
            raise ValueError("metadata must be a string")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ValueError("unterminated quoted skill metadata")
        return value[1:-1].replace("''", "'")
    # YAML comments begin after whitespace. Colons followed by whitespace would
    # introduce a mapping and must be quoted for this deliberately small parser.
    if (": " in value or value.lower() in {"null", "true", "false", "~", "yes", "no", "on", "off"}
            or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value)):
        raise ValueError("quote mapping-like or non-string metadata")
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def parse_frontmatter(text: str) -> tuple[str, str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md requires YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated skill frontmatter") from exc
    fields: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        match = re.fullmatch(r"([A-Za-z][\w-]*):\s*(.*)", line)
        if match and match[1] in {"name", "description"}:
            key, value = match.groups()
            if key in fields:
                raise ValueError(f"duplicate skill metadata: {key}")
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                block = []
                index += 1
                while index < end and (not lines[index] or lines[index][0].isspace()):
                    block.append(lines[index].strip())
                    index += 1
                fields[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
                continue
            fields[key] = _scalar(value)
        elif line.strip() and not line.startswith((" ", "\t", "#")) and not match:
            raise ValueError("unsupported skill frontmatter syntax")
        index += 1
    name, description = fields.get("name", ""), fields.get("description", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("skill name needs lowercase letters, digits and single hyphens; maximum 64")
    if not description.strip() or len(description) > 1024:
        raise ValueError("skill description needs 1 to 1024 characters")
    return name, description


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    manifest_sha256: str
    files: dict[str, str]

    @property
    def source_id(self) -> str:
        return "s" + self.manifest_sha256[:24]


class SkillRegistry:
    def __init__(self, roots: list[Path | str] | None = None):
        self.roots = [Path(root).resolve() for root in (roots if roots is not None else [BUILTIN_SKILLS])]
        self.skills: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                raise ValueError(f"skill root must be an existing directory: {root}")
            candidates = [root] if (root / "SKILL.md").is_file() else sorted(
                child for child in root.iterdir() if child.is_dir())
            for directory in candidates:
                if not (directory / "SKILL.md").is_file():
                    continue
                if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                    raise ValueError("skill directories may not escape their approved root")
                files: dict[str, str] = {}
                total_bytes = 0
                for file in sorted(directory.rglob("*")):
                    if file.is_symlink() or not file.resolve().is_relative_to(directory.resolve()):
                        raise ValueError("skill resources may not contain symlinks or escaping paths")
                    if not file.is_file():
                        continue
                    if len(files) >= MAX_RESOURCES:
                        raise ValueError("a skill may contain at most 64 resource files")
                    size = file.stat().st_size
                    total_bytes += size
                    if size > MAX_FILE_BYTES or total_bytes > 512 * 1024:
                        raise ValueError("skill resource exceeds the file or total byte budget")
                    raw = file.read_bytes()
                    if len(raw) > MAX_FILE_BYTES:
                        raise ValueError("skill resource changed beyond its byte budget")
                    raw.decode("utf-8")  # No binary assets in this text toolbox.
                    files[file.relative_to(directory).as_posix()] = hashlib.sha256(raw).hexdigest()
                name, description = parse_frontmatter((directory / "SKILL.md").read_text(encoding="utf-8"))
                if directory.name != name:
                    raise ValueError("skill name must match its containing directory")
                if name in self.skills or len(self.skills) >= MAX_SKILLS:
                    raise ValueError("skill names must be unique and the registry is limited to 64 skills")
                digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                self.skills[name] = Skill(name, description, directory.resolve(), digest, files)

    def manifest(self) -> dict[str, Any]:
        skills = [{"name": skill.name, "description": skill.description, "source_id": skill.source_id,
                   "manifest_sha256": skill.manifest_sha256, "resources": dict(skill.files)}
                  for skill in sorted(self.skills.values(), key=lambda s: s.name)]
        digest = hashlib.sha256(json.dumps(skills, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"manifest_sha256": digest, "skills": skills}

    def list(self, limit: int = 32, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 32 or offset < 0:
            raise ValueError("skill list uses limit 1..32 and a nonnegative offset")
        skills = self.manifest()["skills"]
        end = min(len(skills), offset + limit)
        return {"skills": [{k: v for k, v in s.items() if k != "resources"}
                           for s in skills[offset:end]], "total": len(skills),
                "next_offset": end if end < len(skills) else None}

    def read(self, name_or_id: str, path: str = "SKILL.md", offset: int = 0, limit: int = 2000) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= MAX_READ_CHARS:
            raise ValueError("skill read uses a nonnegative offset and limit 1..4000")
        skill = self.skills.get(name_or_id) or next((s for s in self.skills.values()
                                                    if s.source_id == name_or_id), None)
        if skill is None:
            raise ValueError("unknown skill name or source ID; run delivery-skill list 32 0 "
                "and use a name or source_id from its results")
        relative = PurePosixPath(path)
        if (not path or "\\" in path or relative.is_absolute() or ".." in relative.parts
                or ":" in path or relative.as_posix() != path or path not in skill.files):
            raise ValueError("resource must be an exact relative path in the frozen skill manifest. "
                "This tool reads Skill reference files. For the resource list, run: "
                f"delivery-skill read {skill.name} SKILL.md 0 2000. "
                "Read repository source using the execution environment's file-reading commands.")
        file = skill.directory.joinpath(*relative.parts)
        if file.is_symlink() or not file.resolve().is_relative_to(skill.directory):
            raise ValueError("skill resource escaped its approved directory")
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("skill resource exceeds the read byte budget")
        raw = file.read_bytes()
        if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != skill.files[path]:
            raise ValueError("skill resource changed after the manifest was frozen")
        text = raw.decode("utf-8")
        end = min(len(text), offset + limit)
        result = {"name": skill.name, "source_id": skill.source_id, "path": path,
                "sha256": skill.files[path], "offset": offset, "total_chars": len(text),
                "text": text[offset:end], "next_offset": end if end < len(text) else None,
                "eof": end == len(text),
                "resources": sorted(skill.files) if path == "SKILL.md" else [],
                "trust": "operator-selected skill instructions; no executable resource is automatically run"}
        if result["eof"]:
            result["guidance"] = ("End of this skill resource; there is no further content. "
                "Do not request a higher offset. Read another listed attachment only if relevant. "
                "Once relevant skill references are exhausted, inspect repository source using the "
                "execution environment's file-reading commands and proceed with the task.")
        return result
