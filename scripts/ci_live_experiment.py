"""Own a job-local Ollama process and execute one genuine model experiment."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery_agent.repository import atomic_json

OLLAMA_VERSION = "0.40.2"
OLLAMA_ARCHIVE_SHA256 = "726bee78706c281b0eeef00746efe51a044d71c592c3f0b195820707f31fdf04"
MODEL = "qwen2.5-coder:1.5b"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--profile", choices=["baseline", "recent", "retrieval"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    binary = Path(args.ollama).resolve()
    public = Path(args.evidence).resolve()
    public.mkdir(parents=True, exist_ok=False)
    opener = build_opener(ProxyHandler({}))

    def request(path, data=None, timeout=10):
        body = json.dumps(data).encode() if data is not None else None
        req = Request("http://127.0.0.1:11434" + path, data=body,
                      headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Bounded setup response exceeded")
            return json.loads(raw)

    # Never borrow or terminate an unrelated service on this machine.
    environment = {**os.environ, "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1",
                   "OLLAMA_MODELS": str(Path(os.environ.get("RUNNER_TEMP", str(Path(args.output).resolve().parent))) / "ollama-models"),
                   "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
                   "OLLAMA_KEEP_ALIVE": "20m", "OLLAMA_CONTEXT_LENGTH": "12000",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    for name in ("HTTP_PROXY", "http_proxy"):
        environment.pop(name, None)
    private_logs = Path(args.output).resolve().parent / "ollama-private.log"
    private_logs.parent.mkdir(parents=True, exist_ok=True)
    daemon = None
    with private_logs.open("wb") as log:
        try:
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", 11434))
            finally:
                probe.close()
            daemon = subprocess.Popen([str(binary), "serve"], stdout=log, stderr=subprocess.STDOUT,
                                      env=environment, start_new_session=True)
            deadline = time.monotonic() + 120
            ready = False
            while time.monotonic() < deadline:
                if daemon.poll() is not None:
                    raise RuntimeError("Job-local Ollama exited during startup")
                try:
                    version = request("/api/version", timeout=2)
                    if version.get("version") != OLLAMA_VERSION:
                        raise ValueError("Ollama server does not match pinned release")
                    ready = True
                    break
                except (OSError, URLError):
                    time.sleep(0.2)
            if not ready:
                raise TimeoutError("Job-local Ollama readiness timeout")
            subprocess.run([str(binary), "pull", MODEL], check=True, capture_output=True,
                           env=environment, timeout=900)
            # Empty generate preloads weights without producing task actions.
            request("/api/generate", {"model": MODEL, "stream": False, "keep_alive": "20m"}, timeout=180)
            models = request("/api/tags").get("models", [])
            matches = [model for model in models if model.get("name") == MODEL]
            if len(matches) != 1 or not matches[0].get("digest"):
                raise ValueError("Pulled model identity is unresolved")
            identity = matches[0]
            atomic_json(public / "ci-environment.json", {
                "schema": 1, "kind": "live_local_model_environment", "fixture": False,
                "ollama_version": OLLAMA_VERSION, "ollama_archive_sha256": OLLAMA_ARCHIVE_SHA256,
                "release_url": "https://github.com/ollama/ollama/releases/tag/v0.40.2",
                "model": MODEL, "model_digest": identity["digest"], "model_bytes": identity.get("size"),
                "details": identity.get("details", {}), "cpu_count": os.cpu_count(),
                "model_source": "https://ollama.com/library/qwen2.5-coder:1.5b",
                "provider": "ollama_native", "backend": "files", "profile": args.profile,
                "task": Path(args.task).name, "preload": "empty generate, no task prompt or fixed actions",
            })
            command = [sys.executable, str(ROOT / "scripts/run_experiments.py"), "--task", args.task,
                "--output", args.output, "--evidence", str(public), "--model", "ollama_chat/" + MODEL,
                "--profiles", args.profile, "--label", "ci-v1", "--max-steps", "10",
                "--context-tokens", "12000", "--wall-seconds", "1200"]
            subprocess.run(command, check=True, env=environment, cwd=ROOT, timeout=2400)
            latest = [model for model in request("/api/tags").get("models", []) if model.get("name") == MODEL]
            if len(latest) != 1 or latest[0].get("digest") != identity["digest"]:
                raise ValueError("Model identity changed during experiment")
        except Exception as exc:
            atomic_json(public / "ci-infrastructure-failure.json", {"schema": 1,
                "stage": "live_model_execution", "error_type": type(exc).__name__})
            raise
        finally:
            if daemon is not None and daemon.poll() is None:
                # This process created this session; the group contains only its
                # daemon/model children. Never use pkill or stop a system service.
                try:
                    os.killpg(daemon.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    daemon.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(daemon.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    daemon.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
