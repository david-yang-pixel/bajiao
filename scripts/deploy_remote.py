"""Server-side driver; invoked by deploy.py, never receives plaintext credentials."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def credentials(path):
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        return dict(db.execute("SELECT key,value FROM system_configs WHERE key IN ('binance_api_key','binance_api_secret')"))


def request(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("http://127.0.0.1:8000" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    root, stage, container = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    baseline = Path(sys.argv[4]) if len(sys.argv) > 4 else root / ".deploy-current.json"
    if not root.is_absolute() or not stage.is_relative_to(root / ".deploy"):
        raise RuntimeError("Unexpected deployment path")
    with (root / ".deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((stage / "manifest.json").read_text())
        files = manifest["files"]
        for item in files:
            p = Path(item["path"])
            if p.is_absolute() or ".." in p.parts or p.parts[:2] != ("backend", "app"):
                raise RuntimeError("Bundle may only update application source")
            if file_hash(stage / p) != item["sha256"]:
                raise RuntimeError("Staged file checksum mismatch")
        inspection = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
        mounts = {m["Destination"]: m["Source"] for m in inspection["Mounts"]}
        if Path(mounts.get("/app/data", "")) != root / "data" or Path(mounts.get("/app/backend", "")) != root / "backend":
            raise RuntimeError("Production data/source mounts do not match this deployment")
        if file_hash(stage / "backend/requirements.txt") != file_hash(root / "backend/requirements.txt"):
            raise RuntimeError("Dependency changes require a separately built image; code-only deployment cancelled")
        if not baseline.is_file():
            raise RuntimeError("A baseline manifest is required for the first managed deployment")
        expected = json.loads(baseline.read_text())["files"]
        if isinstance(expected, dict):
            expected = [{"path": k, **v} if isinstance(v, dict) else {"path": k, "sha256": v} for k, v in expected.items()]
        for item in expected:
            if file_hash(root / item["path"]) != item["sha256"]:
                raise RuntimeError("Production code changed since the baseline; inspect before deploying")

        subprocess.run([
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
            "-v", str(stage) + ":/app:ro", "-e", "DB_PATH=/tmp/deploy-test.db",
            inspection["Image"], "-m", "pytest", "backend/tests", "-q", "-p", "no:cacheprovider",
        ], check=True)
        db_path = root / "data/bstocks.db"
        backup = root / "backups" / manifest["release"]
        backup.mkdir(parents=True, mode=0o700)
        old_credentials = credentials(db_path)
        if not all(old_credentials.get(k) for k in ("binance_api_key", "binance_api_secret")):
            raise RuntimeError("Save verified Binance credentials before deploying")
        with sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True) as src:
            with sqlite3.connect(backup / "bstocks.db") as dst:
                src.backup(dst)
        (backup / "bstocks.db").chmod(0o600)
        original = {}
        for item in files:
            path = root / item["path"]
            original[item["path"]] = path.is_file()
            if path.is_file():
                dst = backup / item["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)
        (backup / "manifest.json").write_text(json.dumps({**manifest, "original_files": original}))
        # Recheck immediately before replacement, including credentials and baseline files.
        if credentials(db_path) != old_credentials:
            raise RuntimeError("Credentials changed during preparation; deployment cancelled")
        for item in expected:
            if file_hash(root / item["path"]) != item["sha256"]:
                raise RuntimeError("Production source changed during preparation")
        try:
            subprocess.run(["docker", "stop", "-t", "20", container], check=True, stdout=subprocess.DEVNULL)
            for item in files:
                target = root / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".deploy-tmp")
                shutil.copy2(stage / item["path"], temporary)
                os.replace(temporary, target)
            subprocess.run(["docker", "start", container], check=True, stdout=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while True:
                try:
                    if request("/api/health").get("status") == "ok":
                        break
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Application health verification timed out")
                time.sleep(1)
            if credentials(db_path) != old_credentials:
                raise RuntimeError("Deployment changed stored credentials")
            if not request("/api/config/test", {"service": "binance"}).get("success"):
                raise RuntimeError("Signed Binance account verification failed after restart")
            for item in files:
                if file_hash(root / item["path"]) != item["sha256"]:
                    raise RuntimeError("Production file verification failed")
            current = root / ".deploy-current.json"
            temporary = current.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest))
            os.replace(temporary, current)
        except Exception:
            subprocess.run(["docker", "stop", "-t", "20", container], stdout=subprocess.DEVNULL, check=False)
            for path, existed in original.items():
                if existed:
                    shutil.copy2(backup / path, root / path)
                else:
                    (root / path).unlink(missing_ok=True)
            subprocess.run(["docker", "start", container], stdout=subprocess.DEVNULL, check=False)
            # Never roll back the database; it may contain newer holdings/settings.
            raise
        print(json.dumps({"release": manifest["release"], "file_count": len(files),
                          "health": "ok", "credentials_preserved": True, "signed_account_check": True}))


if __name__ == "__main__":
    main()
