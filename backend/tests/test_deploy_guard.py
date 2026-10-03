import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from scripts import deploy_remote


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "production"
    stage = root / ".deploy/test-release"
    data = root / "data"
    data.mkdir(parents=True)
    path = "backend/app/example.py"
    for directory, content in [(root, "old source"), (stage, "new source")]:
        target = directory / path
        target.parent.mkdir(parents=True)
        target.write_text(content)
        (directory / "backend/requirements.txt").write_text("same dependencies")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    baseline = root / ".deploy-current.json"
    baseline.write_text(json.dumps({"files": [{"path": path, "sha256": digest(root / path)}]}))
    (stage / "manifest.json").write_text(json.dumps({
        "release": "test-release", "files": [{"path": path, "sha256": digest(stage / path)}],
    }))
    with sqlite3.connect(data / "bstocks.db") as db:
        db.execute("CREATE TABLE system_configs (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO system_configs VALUES (?, ?)", [
            ("binance_api_key", "test key"), ("binance_api_secret", "test secret"),
            ("default_check_interval_hours", "4"),
        ])
    calls = []
    monkeypatch.setattr(sys, "argv", ["deploy_remote", str(root), str(stage), "fake-container"])
    monkeypatch.setattr(deploy_remote.subprocess, "check_output", lambda *a, **k: json.dumps([{
        "Image": "fake-image", "Mounts": [
            {"Destination": "/app/data", "Source": str(data)},
            {"Destination": "/app/backend", "Source": str(root / "backend")},
        ],
    }]).encode())
    monkeypatch.setattr(deploy_remote.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(deploy_remote, "request", lambda path, payload=None: {"status": "ok", "success": True})
    return root, stage, path, calls


def test_deployment_preserves_database_and_checks_signed_accounts(deployment):
    root, stage, path, calls = deployment
    before = deploy_remote.credentials(root / "data/bstocks.db")
    deploy_remote.main()
    assert (root / path).read_text() == "new source"
    assert deploy_remote.credentials(root / "data/bstocks.db") == before
    backup = root / "backups/test-release/bstocks.db"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert deploy_remote.credentials(backup) == before
    assert calls[0][:2] == ["docker", "run"]
    assert "none" in calls[0]
    assert json.loads((root / ".deploy-current.json").read_text())["release"] == "test-release"


@pytest.mark.parametrize("changed", ["source", "dependencies"])
def test_source_or_dependency_drift_stops_before_production_changes(deployment, changed):
    root, stage, path, calls = deployment
    target = root / path if changed == "source" else stage / "backend/requirements.txt"
    target.write_text("unexpected change")
    with pytest.raises(RuntimeError):
        deploy_remote.main()
    assert calls == []
    assert not (root / "backups/test-release").exists()


def test_failed_account_check_restores_code_without_rolling_back_new_settings(deployment, monkeypatch):
    root, stage, path, calls = deployment
    def request(path, payload=None):
        if payload is not None:
            with sqlite3.connect(root / "data/bstocks.db") as db:
                db.execute("UPDATE system_configs SET value='8' WHERE key='default_check_interval_hours'")
            return {"success": False}
        return {"status": "ok"}
    monkeypatch.setattr(deploy_remote, "request", request)
    with pytest.raises(RuntimeError, match="Signed Binance"):
        deploy_remote.main()
    assert (root / path).read_text() == "old source"
    with sqlite3.connect(root / "data/bstocks.db") as db:
        assert db.execute("SELECT value FROM system_configs WHERE key='default_check_interval_hours'").fetchone()[0] == "8"
    assert "release" not in json.loads((root / ".deploy-current.json").read_text())
