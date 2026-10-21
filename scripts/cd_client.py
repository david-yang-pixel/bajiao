#!/usr/bin/env python3
"""Send a digest-pinned deployment request to the forced SSH receiver."""

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Mapping


IMAGE_PATTERN = r"ghcr\.io/david-yang-pixel/bajiao@sha256:[0-9a-f]{64}"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"Missing or invalid {name}")
    return value


def _matches(value: str, pattern: str, name: str) -> str:
    if re.fullmatch(pattern, value, flags=re.ASCII) is None:
        raise ValueError(f"Invalid {name}")
    return value


def build_request(env: Mapping[str, str]) -> dict[str, str | int]:
    """Validate and return exactly the receiver's five-field JSON schema."""
    revision = _matches(_required(env, "GITHUB_SHA"), r"[0-9a-f]{40}", "GITHUB_SHA")
    image = _matches(_required(env, "IMAGE_DIGEST"), IMAGE_PATTERN, "IMAGE_DIGEST")
    actor = _matches(
        _required(env, "GITHUB_ACTOR"),
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}(?:\[bot\])?",
        "GITHUB_ACTOR",
    )
    token = _required(env, "GHCR_TOKEN")
    if not token.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in token):
        raise ValueError("Invalid GHCR_TOKEN")
    run_number = _matches(
        _required(env, "GITHUB_RUN_NUMBER"), r"[1-9][0-9]*", "GITHUB_RUN_NUMBER"
    )
    return {
        "revision": revision,
        "image": image,
        "registry_user": actor,
        "registry_token": token,
        "run_number": int(run_number),
    }


def _host(value: str) -> str:
    # Accept literal IPs (including IPv6), or DNS labels, never SSH URI/options.
    if "%" not in value:
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
    labels = value.rstrip(".").split(".")
    if (
        len(value) > 253
        or value.endswith("..")
        or re.fullmatch(r"[0-9.]+", value)
        or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
               is None for label in labels)
    ):
        raise ValueError("Invalid DEPLOY_HOST")
    return value


def _private_file(path: Path, value: str) -> None:
    # Set permissions at creation, not after writing secret material.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value.rstrip("\n") + "\n")


def deploy(env: Mapping[str, str]) -> None:
    """Send secrets only on SSH stdin; always remove temporary credentials."""
    request = build_request(env)
    host = _host(_required(env, "DEPLOY_HOST"))
    user = _matches(env.get("DEPLOY_USER", "root"), r"[A-Za-z_][A-Za-z0-9_-]{0,31}", "DEPLOY_USER")
    port = _matches(env.get("DEPLOY_PORT", "22"), r"[1-9][0-9]{0,4}", "DEPLOY_PORT")
    if int(port) > 65535:
        raise ValueError("Invalid DEPLOY_PORT")
    key = _required(env, "DEPLOY_SSH_KEY")
    known_hosts = _required(env, "DEPLOY_KNOWN_HOSTS")

    with tempfile.TemporaryDirectory(prefix="bajiao-cd-") as directory:
        key_path = Path(directory) / "identity"
        hosts_path = Path(directory) / "known_hosts"
        _private_file(key_path, key)
        _private_file(hosts_path, known_hosts)
        command = [
            "ssh", "-F", "/dev/null", "-i", str(key_path),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={hosts_path}",
            "-o", "ConnectTimeout=15",
            "-p", port, f"{user}@{host}", "deploy",
        ]
        subprocess.run(
            command,
            input=json.dumps(request, separators=(",", ":")) + "\n",
            text=True,
            check=True,
            capture_output=True,
            timeout=1500,
            # Do not also inherit the GitHub token or SSH secrets into ssh's env.
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )


def main() -> int:
    try:
        deploy(os.environ)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Never relay receiver output or exception details: they may contain secrets.
        print("Deployment failed; check configuration and server logs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
