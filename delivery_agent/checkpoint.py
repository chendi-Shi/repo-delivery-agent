"""Hash-bound, atomic recovery records and a crash-released local writer lock.

The digest detects corruption and accidental editing; it is not a signature or
an authentication mechanism. Checkpoints must remain in the trusted host trial
directory, outside the agent's writable container mount.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


_IS_WINDOWS = os.name == "nt"
_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)


def _replace_checkpoint(temporary: Path, target: Path) -> None:
    """Retry only the same durable rename after bounded Windows sharing errors."""
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, target)
            return
        except OSError as exc:
            if (not _IS_WINDOWS or getattr(exc, "winerror", None) not in {5, 32, 33}
                    or attempt == len(_REPLACE_RETRY_DELAYS)):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


class CheckpointMismatch(ValueError):
    pass


class RecoveryBusy(RuntimeError):
    pass


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def inspect_checkpoint(path: str | Path, expected_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise CheckpointMismatch("unsupported recovery checkpoint schema")
    claimed = value.pop("sha256", None)
    if claimed != fingerprint(value):
        raise CheckpointMismatch("recovery checkpoint digest mismatch")
    if expected_binding is not None and value.get("binding") != expected_binding:
        raise CheckpointMismatch("task/config/code/model/runtime binding differs from checkpoint")
    if not isinstance(value.get("state"), dict) or not isinstance(value.get("binding"), dict):
        raise CheckpointMismatch("checkpoint lacks a binding or agent state")
    value["sha256"] = claimed
    return value


def get_pending(path: str | Path) -> dict[str, Any] | None:
    """Return the durable pending operation, without attaching any environment."""
    transition = inspect_checkpoint(path).get("transition")
    return copy.deepcopy(transition.get("pending")) if transition else None


class CheckpointStore:
    def __init__(self, path: str | Path, binding: dict[str, Any], *, resume: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = (self.path.parent / (self.path.name + ".lock")).open("a+b")
        self._lock.seek(0, os.SEEK_END)
        if self._lock.tell() == 0:
            self._lock.write(b"0")
            self._lock.flush()
        self._lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._lock.close()
            raise RecoveryBusy("another local process owns this trial checkpoint") from exc
        try:
            if resume:
                self.data = inspect_checkpoint(self.path, binding)
            elif self.path.exists():
                raise CheckpointMismatch("checkpoint exists; use resume or a fresh trial")
            else:
                self.data = {"schema": 1, "binding": copy.deepcopy(binding), "revision": 0,
                             "state": {}, "transition": None, "recovery_decisions": []}
        except BaseException:
            self.close()
            raise

    def write(self, *, state: dict[str, Any], transition: dict[str, Any] | None) -> None:
        candidate = {key: copy.deepcopy(value) for key, value in self.data.items() if key != "sha256"}
        candidate.update(state=copy.deepcopy(state), transition=copy.deepcopy(transition),
                         revision=candidate["revision"] + 1)
        candidate["sha256"] = fingerprint(candidate)
        content = json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode()
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_checkpoint(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except BaseException:
            # Preserve the original write/replace error even if a scanner also
            # holds the temporary file. A successful rename already consumes it.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.data = candidate

    def close(self) -> None:
        if getattr(self, "_lock", None) is not None:
            self._lock.close()
            self._lock = None
