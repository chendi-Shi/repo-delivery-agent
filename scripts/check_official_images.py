"""Resolve public Docker Hub manifests without downloading layers or exposing tokens."""
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"))


def request_json(url, token=None):
    headers = {"Accept": ACCEPT}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as reply:
        raw = reply.read(1024 * 1024 + 1)
        digest = reply.headers.get("Docker-Content-Digest")
    if len(raw) > 1024 * 1024:
        raise ValueError("manifest exceeded size budget")
    return json.loads(raw), digest, raw


def main():
    evidence = json.loads((ROOT / "examples/official-smoke/source.json").read_text(encoding="utf-8"))
    items = []
    for task in evidence["grading_metadata"]:
        item = {"instance_id": task["instance_id"], "image": task["image"], "available": False}
        try:
            match = re.fullmatch(r"(swebench/[a-z0-9_.-]+):([a-zA-Z0-9_.-]+)", task["image"])
            if not match:
                raise ValueError("expected an official public Docker Hub image")
            name, tag = match.groups()
            scope = urllib.parse.urlencode({"service": "registry.docker.io", "scope": f"repository:{name}:pull"})
            auth, _, _ = request_json("https://auth.docker.io/token?" + scope)
            token = auth["token"]  # ephemeral, public pull only; never saved or printed
            base = f"https://registry-1.docker.io/v2/{name}/manifests/"
            manifest, digest, raw = request_json(base + tag, token)
            item["tag_manifest_digest"] = digest
            if "manifests" in manifest:
                platform = next(m for m in manifest["manifests"] if m.get("platform", {}).get("os") == "linux"
                                and m.get("platform", {}).get("architecture") == "amd64")
                manifest, digest, raw = request_json(base + platform["digest"], token)
            if digest != "sha256:" + hashlib.sha256(raw).hexdigest():
                raise ValueError("registry digest did not match downloaded manifest bytes")
            item.update(available=True, platform="linux/amd64", manifest_digest=digest,
                        image_config_digest=manifest.get("config", {}).get("digest"),
                        compressed_layer_bytes=sum(layer["size"] for layer in manifest["layers"]))
        except Exception as exc:
            item["error_type"] = type(exc).__name__
        items.append(item)
        print(json.dumps(item), flush=True)
    outcome = {"kind": "public_oci_registry_probe", "layers_downloaded": False,
               "official_grading_executed": False, "images": items, "all_available": all(i["available"] for i in items)}
    output = ROOT / "experiments/official-images.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return 0 if outcome["all_available"] else 1


if __name__ == "__main__":
    sys.exit(main())
