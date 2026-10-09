"""Trusted Docker/Git integration check; no LLM and no performance claim."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_agent.repository import RepoTask, apply_fresh, atomic_json, capture_patch, checkout, git
from delivery_agent.runtime import DockerSession

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("Use a fresh output file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        git(source, "init")
        (source / "module.py").write_text("value = 1\n", encoding="utf-8")
        git(source, "add", ".")
        git(source, "-c", "user.name=Integration Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
        commit = git(source, "rev-parse", "HEAD").stdout.decode().strip()
        task = RepoTask.parse({"instance_id": "trusted__docker-integration", "repo": "trusted/integration",
            "base_commit": commit, "problem_statement": "Trusted runtime integration check", "image": args.image})
        work = checkout(task, root / "agent", source)
        with DockerSession(work, args.image) as env:
            image = env.image_id
            first = env.execute({"command": "python -c \"from pathlib import Path; Path('module.py').write_text('value = 2\\n'); Path('added.py').write_text('from module import value\\n')\""})
            second = env.execute({"command": "python -c \"from module import value; assert value == 2; print('persistent files checked')\""})
            blocked = env.execute({"command": "printf malicious >> .git/config"})
            if first["returncode"] or second["returncode"]:
                raise RuntimeError("Persistent workspace check failed: " + json.dumps({"write": first, "read": second}))
            if blocked["returncode"] == 0:
                raise RuntimeError("Agent metadata should be read-only")
            network = env.execute({"command": "python -c \"import socket; s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1',443))\""})
            if network["returncode"] == 0:
                raise RuntimeError("Container network should be disabled")
        patch = capture_patch(work, commit)
        fresh = apply_fresh(task, patch["model_patch"], root / "validation", source)
        with DockerSession(fresh, image) as verifier:
            checked = verifier.execute({"command": "python -c \"from added import value; assert value == 2; print('fresh patch verified')\""})
            if checked["returncode"]:
                raise RuntimeError("Fresh patch verification failed: " + json.dumps(checked))
        result = {"fixture": True, "label": "TRUSTED INTEGRATION CHECK; NOT MODEL PERFORMANCE",
            "image": image, "passed": True, "checks": ["persistent session", "read-only Git metadata",
            "disabled container network", "new-file patch export", "fresh patch application", "independent container check"],
            "changed_files": patch["changed_files"], "patch_sha256": patch["patch_sha256"]}
        atomic_json(output, result)
        print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
