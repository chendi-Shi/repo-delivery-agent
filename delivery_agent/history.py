"""Durable, bounded history access for an upstream coding agent.

The database contains complete source messages. Context views never mutate it.
The shell-looking history command is interpreted here and never executed by a
host shell or the wrapped execution environment.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path
from typing import Any


class HistoryStore:
    MAX_NOTES = 8
    MAX_NOTE_CHARS = 500
    MAX_SEARCH_RESULTS = 8
    MAX_READ_CHARS = 4000

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY, message TEXT NOT NULL, evidence TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS notes ("
            "name TEXT PRIMARY KEY, text TEXT NOT NULL);"
        )
        self.db.commit()

    @staticmethod
    def source_id(index: int) -> str:
        return f"e{index:06d}"

    @staticmethod
    def _index(source_id: str) -> int:
        if not source_id.startswith("e") or not source_id[1:].isdigit():
            raise ValueError("source ID must have the form e000001")
        index = int(source_id[1:])
        if index < 1:
            raise ValueError("source ID must identify an existing event")
        return index

    @staticmethod
    def evidence(message: dict[str, Any]) -> str:
        extra = message.get("extra") or {}
        raw = extra.get("raw_output")
        if isinstance(raw, str):
            return raw
        content = message.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    def append(self, message: dict[str, Any]) -> str:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(message, evidence) VALUES (?, ?)",
                (json.dumps(message, ensure_ascii=False), self.evidence(message)),
            )
        return self.source_id(cursor.lastrowid)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("search query must contain 1 to 200 characters")
        if not 1 <= limit <= self.MAX_SEARCH_RESULTS:
            raise ValueError("search limit must be between 1 and 8")
        # Literal substring matching avoids SQL wildcard surprises and preserves
        # code identifiers, file paths and Unicode exactly as written.
        rows = self.db.execute(
            "SELECT id, evidence FROM events WHERE instr(lower(evidence), lower(?)) > 0 "
            "ORDER BY id DESC LIMIT ?", (query, limit),
        ).fetchall()
        hits = []
        for index, evidence in rows:
            offset = evidence.lower().find(query.lower())
            if offset < 0:  # SQLite lower() only folds ASCII in default builds.
                offset = 0
            start = max(0, offset - 120)
            hits.append({"source_id": self.source_id(index), "offset": start,
                         "total_chars": len(evidence), "snippet": evidence[start:start + 500]})
        return hits

    def read(self, source_id: str, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= self.MAX_READ_CHARS:
            raise ValueError("read needs a nonnegative offset and a limit between 1 and 4000")
        row = self.db.execute("SELECT evidence FROM events WHERE id = ?",
                              (self._index(source_id),)).fetchone()
        if row is None:
            raise ValueError(f"unknown source ID: {source_id}")
        evidence = row[0]
        end = min(len(evidence), offset + limit)
        return {"source_id": source_id, "offset": offset, "total_chars": len(evidence),
                "text": evidence[offset:end], "next_offset": end if end < len(evidence) else None}

    def note(self, name: str, text: str) -> dict[str, str]:
        if not name or len(name) > 40 or any(char.isspace() for char in name):
            raise ValueError("note name must contain 1 to 40 non-whitespace characters")
        if not text.strip() or len(text) > self.MAX_NOTE_CHARS:
            raise ValueError("note text must contain 1 to 500 characters")
        with self.db:
            exists = self.db.execute("SELECT 1 FROM notes WHERE name = ?", (name,)).fetchone()
            count = self.db.execute("SELECT count(*) FROM notes").fetchone()[0]
            if not exists and count >= self.MAX_NOTES:
                raise ValueError("at most 8 named notes are permitted; update an existing name")
            self.db.execute("INSERT INTO notes(name, text) VALUES (?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET text = excluded.text", (name, text))
        return {"name": name, "text": text}

    def notes(self) -> list[dict[str, str]]:
        return [{"name": name, "text": text}
                for name, text in self.db.execute("SELECT name, text FROM notes ORDER BY name")]

    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self.db.close()


def _result(output: Any, returncode: int = 0) -> dict[str, Any]:
    return {"output": json.dumps(output, ensure_ascii=False), "returncode": returncode,
            "exception_info": ""}


class HistoryEnvironment:
    """Transparent v2 Environment proxy with a strictly interpreted toolbox."""

    def __init__(self, environment: Any, history: HistoryStore):
        self.environment = environment
        self.history = history
        self.config = getattr(environment, "config", None)

    def execute(self, action: dict[str, Any], cwd: str = "", **kwargs: Any) -> dict[str, Any]:
        command = action.get("command", "")
        if not isinstance(command, str):
            return _result({"error": "command must be a string"}, 2)
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            first_word = command.lstrip().split(maxsplit=1)[0] if command.strip() else ""
            if first_word == "delivery-history":
                return _result({"error": f"history command parse error: {exc}"}, 2)
            return self.environment.execute(action, cwd=cwd, **kwargs)
        if not tokens or tokens[0] != "delivery-history":
            return self.environment.execute(action, cwd=cwd, **kwargs)
        # Reject shell operators even when they are quoted. The toolbox is a
        # single command with positional arguments, never a shell expression.
        if any(token and all(character in ";&|<>" for character in token) for token in tokens):
            return _result({"error": "history commands cannot contain shell operators"}, 2)
        try:
            if len(tokens) >= 2 and tokens[1] == "search" and len(tokens) in (3, 4):
                return _result(self.history.search(tokens[2], int(tokens[3]) if len(tokens) == 4 else 5))
            if len(tokens) >= 2 and tokens[1] == "read" and len(tokens) in (3, 4, 5):
                return _result(self.history.read(tokens[2], int(tokens[3]) if len(tokens) >= 4 else 0,
                                                 int(tokens[4]) if len(tokens) == 5 else 2000))
            if len(tokens) == 4 and tokens[1] == "note":
                return _result(self.history.note(tokens[2], tokens[3]))
            return _result({"error": "usage: delivery-history search QUERY [LIMIT] | "
                            "read SOURCE_ID [OFFSET] [LIMIT] | note KEY TEXT"}, 2)
        except (ValueError, TypeError) as exc:
            return _result({"error": str(exc)}, 2)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return self.environment.get_template_vars(**kwargs)

    def serialize(self) -> dict[str, Any]:
        return self.environment.serialize()

    def cleanup(self) -> None:
        cleanup = getattr(self.environment, "cleanup", None)
        if cleanup:
            cleanup()
