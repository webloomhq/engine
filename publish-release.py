#!/usr/bin/env python3
"""One-command release publisher for WebLoom.

Builds the .mcpb bundle from current source, uploads it to the GitHub release
(creating the release if missing), updates server.json with the URL + SHA,
and publishes to the official MCP Registry via mcp-publisher.

Usage:
    python publish-release.py                 # uses version from server.json
    python publish-release.py --version 0.4.0 # bumps + publishes new version

Requirements:
    - mcp-publisher.exe in the current directory (auto-downloaded if missing)
    - GH_TOKEN env var with public_repo + workflow scope, OR vault credentials
    - server.json + manifest.json + LICENSE + README.md + SECURITY.md present
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


HERE = pathlib.Path(__file__).resolve().parent
SERVER_JSON = HERE / "server.json"
MANIFEST_JSON = HERE / "manifest.json"
PUBLISHER_EXE = HERE / ("mcp-publisher.exe" if sys.platform == "win32" else "mcp-publisher")


def download_publisher_if_missing() -> None:
    if PUBLISHER_EXE.exists():
        return
    asset = {
        "win32": "mcp-publisher_windows_amd64.tar.gz",
        "darwin": "mcp-publisher_darwin_amd64.tar.gz",
        "linux": "mcp-publisher_linux_amd64.tar.gz",
    }.get(sys.platform, "mcp-publisher_linux_amd64.tar.gz")
    url = f"https://github.com/modelcontextprotocol/registry/releases/latest/download/{asset}"
    print(f"[publish] downloading mcp-publisher from {url}")
    tmp = HERE / "_pub.tar.gz"
    urllib.request.urlretrieve(url, tmp)
    with tarfile.open(tmp, "r:gz") as t:
        t.extractall(HERE)
    tmp.unlink(missing_ok=True)
    if sys.platform != "win32":
        os.chmod(PUBLISHER_EXE, 0o755)


def load_version() -> str:
    return json.loads(SERVER_JSON.read_text())["version"]


def bump_version(new_ver: str) -> None:
    for path in (SERVER_JSON, MANIFEST_JSON):
        d = json.loads(path.read_text())
        d["version"] = new_ver
        if "packages" in d and d["packages"]:
            d["packages"][0]["version"] = new_ver
        path.write_text(json.dumps(d, indent=2) + "\n")
    print(f"[publish] bumped server.json + manifest.json to {new_ver}")


def build_mcpb(version: str) -> tuple[pathlib.Path, str]:
    out = HERE / f"webloom-{version}.mcpb"
    out.unlink(missing_ok=True)
    include_files = [
        "manifest.json", "server.json", "server.py", "recording.py",
        "webloom_marketplace.py",
        "requirements.txt", "README.md", "LICENSE", "SECURITY.md",
        "thread-schema.json", "launch.ps1",
    ]
    include_dirs = ["vendor", "webloom_sdk"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in include_files:
            p = HERE / f
            if p.exists():
                z.write(p, p.name)
        for d in include_dirs:
            for p in (HERE / d).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                    z.write(p, p.relative_to(HERE).as_posix())
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"[publish] built {out.name} ({out.stat().st_size:,} bytes, sha {sha[:16]}...)")
    return out, sha


def gh_request(method: str, path: str, body=None) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN (or GITHUB_TOKEN) env var required — needs public_repo scope")
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "webloom-publish-release/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:500]}


def get_or_create_release(version: str) -> int:
    tag = f"v{version}"
    rel = gh_request("GET", f"/repos/webloomhq/engine/releases/tags/{tag}")
    if isinstance(rel, dict) and rel.get("id"):
        return rel["id"]
    print(f"[publish] creating release {tag}")
    created = gh_request("POST", "/repos/webloomhq/engine/releases", {
        "tag_name": tag,
        "name": f"{tag}",
        "draft": False,
        "prerelease": False,
    })
    if not created.get("id"):
        raise SystemExit(f"failed to create release: {created}")
    return created["id"]


def upload_mcpb(release_id: int, mcpb: pathlib.Path) -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    # Delete any existing asset with the same name (idempotent re-runs)
    existing = gh_request("GET", f"/repos/webloomhq/engine/releases/{release_id}/assets")
    if isinstance(existing, list):
        for a in existing:
            if a.get("name") == mcpb.name:
                gh_request("DELETE", f"/repos/webloomhq/engine/releases/assets/{a['id']}")
    upload_url = f"https://uploads.github.com/repos/webloomhq/engine/releases/{release_id}/assets?name={mcpb.name}"
    req = urllib.request.Request(
        upload_url,
        data=mcpb.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "webloom-publish-release/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode())
        return body["browser_download_url"]


def update_server_json(mcpb_url: str, sha: str) -> None:
    d = json.loads(SERVER_JSON.read_text())
    d["packages"][0]["identifier"] = mcpb_url
    d["packages"][0]["fileSha256"] = sha
    SERVER_JSON.write_text(json.dumps(d, indent=2) + "\n")
    print(f"[publish] server.json points at {mcpb_url}")


def run_publisher(cmd: list[str]) -> None:
    print(f"[publish] running: {PUBLISHER_EXE.name} {' '.join(cmd)}")
    r = subprocess.run([str(PUBLISHER_EXE)] + cmd, cwd=HERE)
    if r.returncode != 0:
        raise SystemExit(f"mcp-publisher {cmd[0]} failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", help="Bump server.json + manifest.json to this version before publishing")
    ap.add_argument("--skip-login", action="store_true", help="Skip GitHub OAuth (assume already logged in)")
    args = ap.parse_args()

    if args.version:
        bump_version(args.version)
    version = load_version()
    print(f"[publish] target version: {version}")

    download_publisher_if_missing()
    mcpb, sha = build_mcpb(version)
    release_id = get_or_create_release(version)
    mcpb_url = upload_mcpb(release_id, mcpb)
    update_server_json(mcpb_url, sha)

    run_publisher(["validate"])
    if not args.skip_login:
        run_publisher(["login", "github"])
    run_publisher(["publish"])

    print()
    print(f"✓ Published io.github.webloomhq/engine v{version} to registry.modelcontextprotocol.io")
    print(f"  Verify: curl 'https://registry.modelcontextprotocol.io/v0/servers?search=webloom'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
