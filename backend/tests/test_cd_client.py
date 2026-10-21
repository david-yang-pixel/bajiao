"""Offline tests for the forced-command SSH deployment client."""

import json
from pathlib import Path
import stat
import subprocess

import pytest

from scripts import cd_client


@pytest.fixture
def env():
    return {
        "DEPLOY_HOST": "deploy.example.test",
        "DEPLOY_SSH_KEY": "fixture-only-private-key\nnot-a-real-private-key",
        "DEPLOY_KNOWN_HOSTS": "deploy.example.test ssh-ed25519 fixture-host-key",
        "IMAGE_DIGEST": "ghcr.io/david-yang-pixel/bajiao@sha256:" + "b" * 64,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_ACTOR": "david-yang-pixel",
        "GHCR_TOKEN": "ghs_private_fixture_token",
        "GITHUB_RUN_NUMBER": "42",
    }


@pytest.fixture(autouse=True)
def never_use_real_ssh(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Test attempted an unmocked subprocess")

    monkeypatch.setattr(cd_client.subprocess, "run", unexpected)


def test_build_request_has_exact_schema(env):
    env["UNRELATED_SECRET"] = "must-not-be-sent"
    before = dict(env)
    assert cd_client.build_request(env) == {
        "revision": "a" * 40,
        "image": "ghcr.io/david-yang-pixel/bajiao@sha256:" + "b" * 64,
        "registry_user": "david-yang-pixel",
        "registry_token": "ghs_private_fixture_token",
        "run_number": 42,
    }
    assert env == before


def test_bot_actor_is_valid(env):
    env["GITHUB_ACTOR"] = "dependabot[bot]"
    assert cd_client.build_request(env)["registry_user"] == "dependabot[bot]"


@pytest.mark.parametrize("name", [
    "DEPLOY_HOST", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "IMAGE_DIGEST",
    "GITHUB_SHA", "GITHUB_ACTOR", "GHCR_TOKEN", "GITHUB_RUN_NUMBER",
])
@pytest.mark.parametrize("missing", [None, "", " \n\t"])
def test_missing_required_values_never_start_ssh(env, name, missing):
    if missing is None:
        del env[name]
    else:
        env[name] = missing
    with pytest.raises(ValueError, match=name):
        cd_client.deploy(env)


@pytest.mark.parametrize("name,value", [
    ("GITHUB_SHA", "a" * 39),
    ("GITHUB_SHA", "a" * 41),
    ("GITHUB_SHA", "A" * 40),
    ("GITHUB_SHA", "g" * 40),
    ("GITHUB_SHA", "a" * 40 + "\n"),
    ("IMAGE_DIGEST", "ghcr.io/david-yang-pixel/bajiao:latest"),
    ("IMAGE_DIGEST", "sha256:" + "b" * 64),
    ("IMAGE_DIGEST", "ghcr.io/other/bajiao@sha256:" + "b" * 64),
    ("IMAGE_DIGEST", "ghcr.io/david-yang-pixel/bajiao@sha256:" + "B" * 64),
    ("IMAGE_DIGEST", "ghcr.io/david-yang-pixel/bajiao@sha256:" + "b" * 63),
    ("IMAGE_DIGEST", "ghcr.io/david-yang-pixel/bajiao@sha256:" + "b" * 65),
    ("IMAGE_DIGEST", "ghcr.io/david-yang-pixel/bajiao@sha256:" + "b" * 64 + "\n"),
    ("GITHUB_ACTOR", "-option"),
    ("GITHUB_ACTOR", "user\nother"),
    ("GITHUB_ACTOR", "user;command"),
    ("GHCR_TOKEN", "token\nsecret"),
    ("GHCR_TOKEN", "token\0secret"),
    ("GHCR_TOKEN", "token secret"),
    ("GITHUB_RUN_NUMBER", "0"),
    ("GITHUB_RUN_NUMBER", "-1"),
    ("GITHUB_RUN_NUMBER", "+1"),
    ("GITHUB_RUN_NUMBER", "01"),
    ("GITHUB_RUN_NUMBER", "1.0"),
    ("GITHUB_RUN_NUMBER", "１"),
    ("GITHUB_RUN_NUMBER", "1\n"),
    ("GITHUB_RUN_NUMBER", "1;command"),
])
def test_malformed_request_values(env, name, value):
    env[name] = value
    with pytest.raises(ValueError, match=name):
        cd_client.build_request(env)
    with pytest.raises(ValueError):
        cd_client.deploy(env)


@pytest.mark.parametrize("name,value", [
    ("DEPLOY_HOST", "-oProxyCommand=command"),
    ("DEPLOY_HOST", "user@host"),
    ("DEPLOY_HOST", "ssh://host"),
    ("DEPLOY_HOST", "host:22"),
    ("DEPLOY_HOST", "host other"),
    ("DEPLOY_HOST", "host\nother"),
    ("DEPLOY_HOST", "host;command"),
    ("DEPLOY_HOST", "$(command)"),
    ("DEPLOY_HOST", "host/other"),
    ("DEPLOY_HOST", "bad..host"),
    ("DEPLOY_HOST", "bad.."),
    ("DEPLOY_HOST", ".host"),
    ("DEPLOY_HOST", "bad-.host"),
    ("DEPLOY_HOST", "999.999.999.999"),
    ("DEPLOY_HOST", "a" * 64 + ".test"),
    ("DEPLOY_HOST", "fe80::1%eth0"),
    ("DEPLOY_USER", ""),
    ("DEPLOY_USER", "-root"),
    ("DEPLOY_USER", "root@host"),
    ("DEPLOY_USER", "root other"),
    ("DEPLOY_USER", "root;command"),
    ("DEPLOY_USER", "root\n"),
    ("DEPLOY_USER", "$(command)"),
    ("DEPLOY_PORT", ""),
    ("DEPLOY_PORT", "0"),
    ("DEPLOY_PORT", "65536"),
    ("DEPLOY_PORT", "99999999999999999999"),
    ("DEPLOY_PORT", "-22"),
    ("DEPLOY_PORT", "+22"),
    ("DEPLOY_PORT", "22 -oProxyCommand=command"),
    ("DEPLOY_PORT", "22;command"),
    ("DEPLOY_PORT", "22\n"),
    ("DEPLOY_PORT", "２２"),
    ("DEPLOY_PORT", "22.0"),
    ("DEPLOY_SSH_KEY", "key\0data"),
    ("DEPLOY_KNOWN_HOSTS", "host\0data"),
])
def test_malformed_connection_values_never_start_ssh(env, name, value):
    env[name] = value
    with pytest.raises(ValueError, match=name):
        cd_client.deploy(env)


@pytest.mark.parametrize("failure", [None, "exit", "timeout", "missing-executable"])
def test_stdin_only_secrets_private_files_and_cleanup(env, monkeypatch, tmp_path, capsys, failure):
    monkeypatch.setattr(cd_client.tempfile, "tempdir", str(tmp_path))
    seen = []

    def fake_ssh(command, **kwargs):
        key = Path(command[command.index("-i") + 1])
        hosts = Path(next(item.split("=", 1)[1] for item in command if item.startswith("UserKnownHostsFile=")))
        seen.extend([key, hosts, key.parent])
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
        assert key.read_text() == env["DEPLOY_SSH_KEY"] + "\n"
        assert hosts.read_text() == env["DEPLOY_KNOWN_HOSTS"] + "\n"
        assert command == [
            "ssh", "-F", "/dev/null", "-i", str(key),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts}",
            "-o", "ConnectTimeout=15", "-p", "22", "root@deploy.example.test", "deploy",
        ]
        assert not kwargs.get("shell", False)
        assert kwargs["text"] is True
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 1500
        assert json.loads(kwargs["input"]) == cd_client.build_request(env)
        assert kwargs["input"].count("\n") == 1
        for name in ("GHCR_TOKEN", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
            assert env[name] not in " ".join(command)
            assert env[name] not in kwargs["env"].values()
            assert name not in kwargs["env"]
        if failure == "exit":
            raise subprocess.CalledProcessError(1, command, output=env["GHCR_TOKEN"], stderr=env["DEPLOY_SSH_KEY"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1500, output=env["GHCR_TOKEN"])
        if failure == "missing-executable":
            raise FileNotFoundError("ssh unavailable")
        return subprocess.CompletedProcess(command, 0, stdout=env["GHCR_TOKEN"], stderr=env["DEPLOY_SSH_KEY"])

    monkeypatch.setattr(cd_client.subprocess, "run", fake_ssh)
    if failure:
        with pytest.raises((subprocess.SubprocessError, OSError)):
            cd_client.deploy(env)
    else:
        assert cd_client.deploy(env) is None
    assert seen
    assert all(not path.exists() for path in seen)
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("host", ["localhost", "example.test.", "192.0.2.1", "2001:db8::1"])
def test_valid_hosts_and_explicit_connection_settings(env, monkeypatch, host):
    env.update(DEPLOY_HOST=host, DEPLOY_USER="deployer", DEPLOY_PORT="65535")
    calls = []
    monkeypatch.setattr(cd_client.subprocess, "run", lambda command, **kwargs: calls.append(command))
    cd_client.deploy(env)
    assert calls[0][-4:] == ["-p", "65535", f"deployer@{host}", "deploy"]


def test_cleanup_when_writing_credentials_fails(env, monkeypatch, tmp_path):
    monkeypatch.setattr(cd_client.tempfile, "tempdir", str(tmp_path))
    original = cd_client._private_file
    written = []

    def fail_on_known_hosts(path, value):
        if written:
            raise OSError("cannot write known hosts")
        original(path, value)
        written.append(path)

    monkeypatch.setattr(cd_client, "_private_file", fail_on_known_hosts)
    with pytest.raises(OSError):
        cd_client.deploy(env)
    assert written and not written[0].exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [False, True])
def test_main_never_logs_receiver_output_or_secrets(env, monkeypatch, capsys, failure):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    def fake_ssh(command, **kwargs):
        if failure:
            raise subprocess.CalledProcessError(
                1, command, output=env["GHCR_TOKEN"], stderr=env["DEPLOY_SSH_KEY"]
            )
        return subprocess.CompletedProcess(command, 0, env["GHCR_TOKEN"], env["DEPLOY_SSH_KEY"])

    monkeypatch.setattr(cd_client.subprocess, "run", fake_ssh)
    assert cd_client.main() == int(failure)
    output = capsys.readouterr()
    assert output.out == ""
    assert bool(output.err) == failure
    for name in ("GHCR_TOKEN", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        assert env[name] not in output.err
