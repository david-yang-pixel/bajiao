#!/usr/bin/env python3
"""Deploy application code without bundling or overwriting production secrets/data."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="zendesk-agent-server")
    parser.add_argument("--root", default="/opt/bstocks-bot")
    parser.add_argument("--container", default="binance-bstocks-analysis-bot")
    parser.add_argument("--baseline", help="Initial server manifest to verify before deployment")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    release = "credential-guard-" + uuid.uuid4().hex[:12]
    stage = args.root.rstrip("/") + "/.deploy/" + release
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host]
    source = sorted(p for p in (repo / "backend/app").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                    and (p.suffix == ".py" or "static" in p.parts))
    manifest = {"release": release, "files": [
        {"path": str(p.relative_to(repo)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in source
    ]}
    with tempfile.TemporaryDirectory(prefix="bstocks-deploy-") as temporary:
        bundle = Path(temporary) / "code.tar.gz"
        manifest_path = Path(temporary) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        with tarfile.open(bundle, "w:gz") as tar:
            files = source + sorted((repo / "backend/tests").glob("*.py")) + [
                repo / "backend/requirements.txt", repo / "scripts/deploy_remote.py",
            ]
            for p in files:
                if p.is_symlink():
                    raise RuntimeError("Deployment refuses symlinked source files")
                tar.add(p, arcname=str(p.relative_to(repo)), recursive=False)
            tar.add(manifest_path, arcname="manifest.json")
        subprocess.run(ssh + ["mkdir -p " + shlex.quote(stage) + " && chmod 700 " + shlex.quote(stage)], check=True)
        subprocess.run(["scp", "-q", str(bundle), args.host + ":" + stage + "/code.tar.gz"], check=True)
        extractor = """import tarfile,pathlib,sys
root=pathlib.Path(sys.argv[1])
with tarfile.open(root/'code.tar.gz') as archive:
    for member in archive:
        path=pathlib.PurePosixPath(member.name)
        if not member.isfile() or path.is_absolute() or '..' in path.parts:
            raise RuntimeError('Unsafe deployment bundle')
        target=root.joinpath(*path.parts)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(archive.extractfile(member).read())
"""
        subprocess.run(ssh + ["python3 -c " + shlex.quote(extractor) + " " + shlex.quote(stage)], check=True)
        command = ["python3", stage + "/scripts/deploy_remote.py", args.root, stage, args.container]
        if args.baseline:
            command.append(args.baseline)
        subprocess.run(ssh + [shlex.join(command)], check=True)


if __name__ == "__main__":
    main()
