"""No real Docker, SSH, registry or production files in deployment regression tests."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest
from scripts import deploy_image as cd

OLD = 'sha256:' + '1' * 64
NEW = 'sha256:' + '2' * 64
OTHER = 'sha256:' + '3' * 64
STOPPED = 'sha256:' + '4' * 64
UNUSED = 'sha256:' + '5' * 64
REV = 'a' * 40


def request(**changes):
    return dict(revision=REV, image=cd.REPOSITORY + '@sha256:' + 'b' * 64,
                registry_user='fixture-user', registry_token='fixture-not-a-real-token',
                run_number=10, **changes)


def image(identity, *, project=True, tags=None, revision=REV):
    labels = {cd.PROJECT_LABEL: 'bajiao', 'org.opencontainers.image.revision': revision} if project else {}
    return {'Id': identity, 'Config': {'Labels': labels}, 'RepoTags': tags or [], 'RepoDigests': []}


class FakeDocker:
    def __init__(self, root):
        self.commands = []
        self.old = {
            'Image': OLD, 'State': {'Running': True, 'Status': 'running', 'Health': {'Status': 'healthy'}},
            'Mounts': [{'Type': 'bind', 'Source': str(root / folder), 'Destination': '/app/' + folder}
                       for folder in ('data', 'logs', 'backend')],
            'HostConfig': {'NetworkMode': 'bstocks-bot_default', 'PortBindings': {
                '8000/tcp': [{'HostIp': '', 'HostPort': '8000'}]}},
            'Config': {'Image': cd.LEGACY_REPOSITORY, 'Env': [
                'DB_PATH=/app/data/bstocks.db', 'BINANCE_API_SECRET=fixture-keep-me', 'PATH=/old/base'],
                'Labels': {'com.docker.compose.project': 'bstocks-bot', 'com.docker.compose.service': 'bstocks-bot'}},
        }
        self.containers = {cd.CONTAINER: copy.deepcopy(self.old)}
        self.images = {
            OLD: image(OLD, project=False, tags=[cd.LEGACY_REPOSITORY + ':latest']),
            NEW: image(NEW), OTHER: image(OTHER, tags=['other-project:latest']),
            STOPPED: image(STOPPED), UNUSED: image(UNUSED),
        }
        self.target_ref = request()['image']
        self.failure = None
        self.new_healthy = True
        self.on_new_start = lambda: None
        self.registry_directories = []
        self.stopped_image_users = {STOPPED}
        self.partial_create_failure = False
        self.uncertain_operation = None

    def container(self, name):
        return copy.deepcopy(self.containers.get(name))

    def image(self, ref):
        return copy.deepcopy(self.images.get(NEW if ref == self.target_ref else ref))

    def project_images(self):
        return {key for key, value in self.images.items()
                if value['Config']['Labels'].get(cd.PROJECT_LABEL) == 'bajiao'}

    def used_images(self):
        return {v['Image'] for v in self.containers.values()} | self.stopped_image_users

    def run(self, args, *, input=None, optional=False):
        self.commands.append((list(args), input))
        if args[0] == '--config':
            assert Path(args[1]).stat().st_mode & 0o777 == 0o700
            self.registry_directories.append(args[1])
        if self.failure and self.failure(args):
            raise cd.DeploymentError('fixture Docker failure')
        if args[:2] == ['container', 'stop']:
            self.containers[args[-1]]['State']['Running'] = False
            if self.uncertain_operation == 'stop':
                self.uncertain_operation = None
                raise cd.DeploymentError('fixture lost stop acknowledgement')
        elif args[:2] == ['container', 'rename']:
            self.containers[args[3]] = self.containers.pop(args[2])
            if self.uncertain_operation == 'rename':
                self.uncertain_operation = None
                raise cd.DeploymentError('fixture lost rename acknowledgement')
        elif args[:2] == ['container', 'create']:
            created = copy.deepcopy(self.old)
            created['Image'] = NEW
            created['Config']['Image'] = self.target_ref
            created['Mounts'] = [m for m in created['Mounts'] if m['Destination'] != '/app/backend']
            created['State'] = {'Running': False, 'Status': 'created', 'Health': {'Status': 'starting'}}
            self.containers[cd.CONTAINER] = created
            if self.partial_create_failure:
                raise cd.DeploymentError('fixture partial create')
        elif args[:2] == ['container', 'start']:
            target = self.containers[args[2]]
            target['State'].update(Running=True, Status='running', Health={'Status':
                'healthy' if target['Image'] != NEW or self.new_healthy else 'unhealthy'})
            if target['Image'] == NEW:
                self.on_new_start()
        elif args[:2] == ['container', 'rm']:
            assert not self.containers[args[2]]['State']['Running']
            del self.containers[args[2]]
        elif args[:2] == ['image', 'rm']:
            reference = args[2]
            for key, value in list(self.images.items()):
                if key == reference or reference in value['RepoTags']:
                    assert key not in self.used_images()
                    del self.images[key]
        return ''


@pytest.fixture
def setup(tmp_path):
    for name in ('data', 'logs', 'backend'):
        (tmp_path / name).mkdir()
    (tmp_path / '.env').write_text('OPENAI_API_KEY=fixture-local-env\n')
    with sqlite3.connect(tmp_path / 'data/bstocks.db') as db:
        db.executescript('''CREATE TABLE system_configs(key TEXT,value TEXT);
        INSERT INTO system_configs VALUES('binance_api_secret','fixture-stored-secret');
        CREATE TABLE positions(id INTEGER,symbol TEXT,buy_price REAL,quantity REAL,
        take_profit_price REAL,stop_loss_price REAL,check_interval_hours REAL,
        ladder_alerts TEXT,include_ai_analysis INTEGER,is_active INTEGER);
        INSERT INTO positions VALUES(1,'NVDABUSDT',100,2,120,90,4,'[5,10]',0,1);''')
    docker = FakeDocker(tmp_path)
    runner = cd.Deployment(tmp_path, docker=docker, probe=lambda rev: True, timeout=.02)
    return runner, docker, tmp_path


@pytest.mark.parametrize('field,value', [
    ('revision', 'bad'), ('revision', 'a' * 40 + ';whoami'),
    ('image', 'ghcr.io/other/project@sha256:' + 'b' * 64),
    ('image', cd.REPOSITORY + ':latest'), ('registry_user', '-option'),
    ('registry_token', 'secret\nleak'), ('run_number', True), ('run_number', 0),
])
def test_invalid_request_is_rejected_before_any_docker(setup, field, value):
    runner, docker, _ = setup
    payload = request()
    payload[field] = value
    with pytest.raises(cd.DeploymentError):
        runner.deploy(payload)
    assert docker.commands == []


def test_extra_request_fields_are_not_accepted():
    with pytest.raises(cd.DeploymentError):
        cd.validate_request({**request(), 'command': 'rm -rf /'})


def test_success_keeps_data_and_cleans_only_unused_project_images(setup, capsys):
    runner, docker, root = setup
    before = cd.application_state(root / 'data/bstocks.db')
    env = (root / '.env').read_bytes()
    removed = runner.deploy(request())
    assert set(removed) == {OLD, UNUSED}
    assert docker.containers[cd.CONTAINER]['Image'] == NEW
    assert set(docker.images) == {NEW, OTHER, STOPPED}
    assert list(docker.containers) == [cd.CONTAINER]
    assert (root / '.env').read_bytes() == env
    assert cd.application_state(root / 'data/bstocks.db') == before
    marker = json.loads(runner.marker.read_text())
    assert marker['revision'] == REV and marker['run_number'] == 10
    assert 'registry_token' not in marker
    assert runner.marker.stat().st_mode & 0o777 == 0o600
    backup = next((root / 'backups').glob('cicd-*'))
    assert cd.application_state(backup / 'bstocks.db') == before
    assert (backup / 'bstocks.db').stat().st_mode & 0o777 == 0o600
    runtime = backup / 'runtime.env'
    assert runtime.stat().st_mode & 0o777 == 0o600
    assert 'BINANCE_API_SECRET=fixture-keep-me' in runtime.read_text()
    assert not any(line.startswith('PATH=') for line in runtime.read_text().splitlines())
    create = next(args for args, _ in docker.commands if args[:2] == ['container', 'create'])
    assert not any('/app/backend' in arg for arg in create)
    assert all('prune' not in args and '--force' not in args for args, _ in docker.commands)
    login = next((args, value) for args, value in docker.commands if 'login' in args)
    assert request()['registry_token'] not in ' '.join(login[0])
    assert login[1] == request()['registry_token'] + '\n'
    assert all(not Path(path).exists() for path in docker.registry_directories)
    captured = capsys.readouterr()
    assert request()['registry_token'] not in captured.out + captured.err
    assert 'fixture-keep-me' not in captured.out + captured.err


def test_new_health_failure_restores_old_without_deleting_images(setup):
    runner, docker, root = setup
    before = cd.application_state(root / 'data/bstocks.db')
    docker.new_healthy = False
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    assert docker.containers[cd.CONTAINER]['Image'] == OLD
    assert docker.containers[cd.CONTAINER]['State']['Running']
    assert not runner.marker.exists()
    assert not any(args[:2] == ['image', 'rm'] for args, _ in docker.commands)
    assert cd.application_state(root / 'data/bstocks.db') == before


def test_partial_create_failure_is_also_rolled_back(setup):
    runner, docker, _ = setup
    docker.partial_create_failure = True
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    assert docker.containers[cd.CONTAINER]['Image'] == OLD
    assert docker.containers[cd.CONTAINER]['State']['Running']


def test_rollback_does_not_overwrite_writes_accepted_by_new_app(setup):
    runner, docker, root = setup
    def update():
        with sqlite3.connect(root / 'data/bstocks.db') as db:
            db.execute('UPDATE positions SET quantity=3 WHERE id=1')
    docker.on_new_start = update
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    with sqlite3.connect(root / 'data/bstocks.db') as db:
        assert db.execute('SELECT quantity FROM positions').fetchone()[0] == 3
    assert docker.containers[cd.CONTAINER]['Image'] == OLD


def test_cleanup_failure_does_not_roll_back_healthy_release(setup):
    runner, docker, _ = setup
    docker.failure = lambda args: args[:2] == ['image', 'rm']
    with pytest.raises(cd.DeploymentError, match='healthy.*cleanup failed'):
        runner.deploy(request())
    assert docker.containers[cd.CONTAINER]['Image'] == NEW
    assert runner.marker.exists()
    assert not any(args[:2] == ['container', 'start'] and args[-1] != cd.CONTAINER
                   for args, _ in docker.commands)


def test_image_identity_mismatch_leaves_old_untouched(setup):
    runner, docker, _ = setup
    docker.images[NEW]['Config']['Labels']['org.opencontainers.image.revision'] = 'f' * 40
    with pytest.raises(cd.DeploymentError, match='identity'):
        runner.deploy(request())
    assert not any(args[:2] == ['container', 'stop'] for args, _ in docker.commands)
    assert docker.containers[cd.CONTAINER]['State']['Running']


def test_replayed_old_run_is_rejected(setup):
    runner, docker, _ = setup
    runner.ci.mkdir()
    runner.marker.write_text(json.dumps({'run_number': 11}))
    with pytest.raises(cd.DeploymentError, match='older workflow'):
        runner.deploy(request())
    assert docker.commands == []


def test_source_and_mounts_cannot_escape_application_root(setup):
    runner, docker, _ = setup
    docker.containers[cd.CONTAINER]['Mounts'][0]['Source'] = '/other-project/data'
    with pytest.raises(cd.DeploymentError, match='mount'):
        runner.deploy(request())
    assert docker.commands == []


def test_shared_tagged_image_is_never_deleted(setup):
    runner, docker, _ = setup
    docker.images[UNUSED]['RepoTags'] = ['another-service:keep']
    removed = runner.deploy(request())
    assert UNUSED not in removed and UNUSED in docker.images


def test_legacy_containerd_digest_is_cleaned_after_verified_deploy(setup):
    runner, docker, _ = setup
    docker.images[OLD]['RepoDigests'] = [cd.LEGACY_REPOSITORY + '@' + OLD]
    removed = runner.deploy(request())
    assert OLD in removed and OLD not in docker.images


def test_foreign_digest_on_old_image_prevents_removal(setup):
    runner, docker, _ = setup
    docker.images[OLD]['RepoDigests'] = ['other-service@' + OLD]
    removed = runner.deploy(request())
    assert OLD not in removed and OLD in docker.images


def test_docker_error_does_not_echo_upstream_secrets(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=subprocess.CompletedProcess(
        [], 1, '', 'fixture-upstream-secret')))
    with pytest.raises(cd.DeploymentError) as error:
        cd.Docker().run(['login'], input='fixture-request-secret')
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('operation', ['stop', 'rename'])
def test_lost_operation_acknowledgement_recovers_actual_container_state(setup, operation):
    runner, docker, _ = setup
    docker.uncertain_operation = operation
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    assert docker.containers[cd.CONTAINER]['Image'] == OLD
    assert docker.containers[cd.CONTAINER]['State']['Running']
    assert not any(args[:2] == ['image', 'rm'] for args, _ in docker.commands)


def test_bootstrap_installs_restricted_key_and_preserves_other_authorization(tmp_path, monkeypatch):
    import base64
    import hashlib
    import io
    import os
    import sys
    from scripts.setup_cicd import INSTALL, RETIRE_OLD_KEYS
    root = tmp_path / 'project'
    (root / 'data').mkdir(parents=True)
    (root / 'data/bstocks.db').touch()
    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    authorized = home / '.ssh/authorized_keys'
    existing = 'ssh-ed25519 AAAA-admin administrator\n'
    old_key = 'restrict,command="old" ssh-ed25519 AAAA-old bajiao-github-actions\n'
    authorized.write_text(existing + old_key)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    receiver = b'# fixture receiver\n'
    public = 'ssh-ed25519 AAAA-new bajiao-github-actions'
    payload = {'receiver': base64.b64encode(receiver).decode(),
               'sha256': hashlib.sha256(receiver).hexdigest(), 'public_key': public}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    exec(compile(INSTALL.replace("'/opt/bstocks-bot'", repr(str(root))), 'fixture-install', 'exec'), {})
    assert authorized.read_text().startswith(existing + old_key)
    assert 'restrict,command="/usr/bin/python3 /opt/bstocks-bot/ci/deploy_image.py" ' + public in authorized.read_text()
    assert (root / 'ci/deploy_image.py').read_bytes() == receiver
    assert (root / 'ci/deploy_image.py').stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'public_key': public})))
    exec(compile(RETIRE_OLD_KEYS, 'fixture-key-rotation', 'exec'), {})
    assert authorized.read_text().startswith(existing)
    assert 'AAAA-old' not in authorized.read_text()
    assert public in authorized.read_text()


def test_bootstrap_refuses_symlinked_receiver_directory(tmp_path, monkeypatch):
    import io
    import os
    import sys
    from scripts.setup_cicd import INSTALL
    root = tmp_path / 'project'
    (root / 'data').mkdir(parents=True)
    (root / 'data/bstocks.db').touch()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'ci').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    with pytest.raises(SystemExit, match='Unsafe deployment directory'):
        exec(compile(INSTALL.replace("'/opt/bstocks-bot'", repr(str(root))), 'fixture-install', 'exec'), {})
    assert not list(outside.iterdir())


def test_failed_rollback_container_cleanup_is_reconciled_on_next_deploy(setup):
    runner, docker, _ = setup
    docker.failure = lambda args: args[:2] == ['container', 'rm']
    with pytest.raises(cd.DeploymentError, match='healthy.*cleanup failed'):
        runner.deploy(request())
    assert OLD in docker.images
    assert json.loads(runner.marker.read_text())['pending_cleanup']
    docker.failure = None
    removed = runner.deploy(request())
    assert OLD in removed and OLD not in docker.images
    assert list(docker.containers) == [cd.CONTAINER]
    assert json.loads(runner.marker.read_text())['pending_cleanup'] == []
    assert OTHER in docker.images and STOPPED in docker.images


def test_pending_cleanup_cannot_delete_other_service(setup):
    runner, docker, _ = setup
    runner.ci.mkdir()
    runner.marker.write_text(json.dumps({'run_number': 1, 'pending_cleanup': [
        {'name': 'another-service', 'image_id': OTHER}]}))
    docker.containers['another-service'] = {'Image': OTHER, 'State': {'Running': False}}
    with pytest.raises(cd.DeploymentError, match='healthy.*cleanup failed'):
        runner.deploy(request())
    assert 'another-service' in docker.containers
    assert not any(args == ['container', 'rm', 'another-service'] for args, _ in docker.commands)
