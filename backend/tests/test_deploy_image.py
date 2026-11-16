"""No real Docker, SSH, registry or production files in deployment regression tests."""
import copy
import json
from pathlib import Path
import re
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
AGENT_NEW = 'sha256:' + '6' * 64
AGENT_OLD = 'sha256:' + '7' * 64
REV = 'a' * 40


def request(**changes):
    return dict(revision=REV, image=cd.REPOSITORY + '@sha256:' + 'b' * 64,
                agent_image=cd.AGENT_REPOSITORY + '@sha256:' + 'c' * 64,
                registry_user='fixture-user', registry_token='fixture-not-a-real-token',
                **{'run_number': 10, **changes})


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
            'NetworkSettings': {'Networks': {'bstocks-bot_default': {}}},
            'Config': {'Image': cd.LEGACY_REPOSITORY, 'Env': [
                'DB_PATH=/app/data/bstocks.db', 'BINANCE_API_SECRET=fixture-keep-me', 'PATH=/old/base'],
                'Labels': {'com.docker.compose.project': 'bstocks-bot', 'com.docker.compose.service': 'bstocks-bot'}},
        }
        self.containers = {cd.CONTAINER: copy.deepcopy(self.old)}
        self.images = {
            OLD: image(OLD, project=False, tags=[cd.LEGACY_REPOSITORY + ':latest']),
            NEW: image(NEW), OTHER: image(OTHER, tags=['other-project:latest']),
            STOPPED: image(STOPPED), UNUSED: image(UNUSED), AGENT_NEW: image(AGENT_NEW),
        }
        self.networks = {}
        self.target_ref = request()['image']
        self.agent_ref = request()['agent_image']
        self.failure = None
        self.new_healthy = True
        self.agent_new_healthy = True
        self.on_new_start = lambda: None
        self.registry_directories = []
        self.stopped_image_users = {STOPPED}
        self.partial_create_failure = False
        self.uncertain_operation = None

    def container(self, name):
        return copy.deepcopy(self.containers.get(name))

    def image(self, ref):
        ref = {self.target_ref: NEW, self.agent_ref: AGENT_NEW}.get(ref, ref)
        return copy.deepcopy(self.images.get(ref))

    def network(self, name):
        return copy.deepcopy(self.networks.get(name))

    def add_previous_agent(self, *, project=True):
        labels = {cd.PROJECT_LABEL: 'bajiao'} if project else {}
        self.images[AGENT_OLD] = dict(image(AGENT_OLD, revision='e' * 40),
                                      RepoDigests=[cd.AGENT_REPOSITORY + '@' + AGENT_OLD])
        self.containers[cd.AGENT_CONTAINER] = {
            'Image': AGENT_OLD, 'Config': {'Image': cd.AGENT_REPOSITORY + '@' + AGENT_OLD, 'Labels': labels},
            'State': {'Running': True, 'Status': 'running', 'Health': {'Status': 'healthy'}},
            'NetworkSettings': {'Networks': {cd.SHARED_NETWORK: {}}},
        }
        self.networks[cd.SHARED_NETWORK] = {'Name': cd.SHARED_NETWORK, 'Labels': {cd.PROJECT_LABEL: 'bajiao'}}

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
        elif args[:2] == ['container', 'create'] and args[args.index('--name') + 1] == cd.AGENT_CONTAINER:
            assert cd.AGENT_CONTAINER not in self.containers
            self.containers[cd.AGENT_CONTAINER] = {
                'Image': AGENT_NEW, 'Config': {'Image': self.agent_ref, 'Labels': {cd.PROJECT_LABEL: 'bajiao'}},
                'State': {'Running': False, 'Status': 'created', 'Health': {'Status': 'starting'}},
                'NetworkSettings': {'Networks': {cd.SHARED_NETWORK: {}}},
            }
        elif args[:2] == ['container', 'create']:
            assert cd.CONTAINER not in self.containers
            created = copy.deepcopy(self.old)
            created['Image'] = NEW
            created['Config']['Image'] = self.target_ref
            created['Mounts'] = [m for m in created['Mounts'] if m['Destination'] != '/app/backend']
            created['State'] = {'Running': False, 'Status': 'created', 'Health': {'Status': 'starting'}}
            network = args[args.index('--network') + 1]
            created['HostConfig']['NetworkMode'] = network
            created['NetworkSettings'] = {'Networks': {network: {}}}
            self.containers[cd.CONTAINER] = created
            if self.partial_create_failure:
                raise cd.DeploymentError('fixture partial create')
        elif args[:2] == ['container', 'start']:
            target = self.containers[args[2]]
            healthy = ((target['Image'] != NEW or self.new_healthy)
                       and (target['Image'] != AGENT_NEW or self.agent_new_healthy))
            target['State'].update(Running=True, Status='running',
                                   Health={'Status': 'healthy' if healthy else 'unhealthy'})
            if target['Image'] == NEW:
                self.on_new_start()
        elif args[:2] == ['network', 'create']:
            assert args[-1] not in self.networks
            self.networks[args[-1]] = {'Name': args[-1], 'Labels': {cd.PROJECT_LABEL: 'bajiao'}}
        elif args[:2] == ['network', 'connect']:
            networks = self.containers[args[3]]['NetworkSettings']['Networks']
            assert args[2] in self.networks and args[2] not in networks  # Docker rejects duplicates
            networks[args[2]] = {}
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
    ('agent_image', cd.REPOSITORY + '@sha256:' + 'c' * 64), ('agent_image', cd.AGENT_REPOSITORY + ':latest'),
    ('agent_image', None),
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
    assert set(docker.images) == {NEW, OTHER, STOPPED, AGENT_NEW}
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
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
    assert not any(args[:2] == ['container', 'start'] and args[-1] not in {cd.CONTAINER, cd.AGENT_CONTAINER}
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


def test_digest_reference_in_repotags_is_cleaned_for_owned_unused_image(setup):
    runner, docker, _ = setup
    reference = cd.REPOSITORY + '@' + UNUSED
    docker.images[UNUSED]['RepoTags'] = [reference]
    docker.images[UNUSED]['RepoDigests'] = [reference]
    removed = runner.deploy(request())
    assert UNUSED in removed and UNUSED not in docker.images


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
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
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


def test_request_without_agent_image_is_rejected():
    payload = request()
    del payload['agent_image']
    with pytest.raises(cd.DeploymentError, match='Invalid deployment request fields'):
        cd.validate_request(payload)


def commands(docker, *prefix):
    return [args for args, _ in docker.commands if args[:len(prefix)] == list(prefix)]


def test_first_deploy_creates_network_agent_dir_and_agent(setup):
    runner, docker, root = setup
    runner.deploy(request())
    assert commands(docker, 'network', 'create') == [
        ['network', 'create', '--label', 'io.bajiao.project=bajiao', 'bajiao-net']]
    agent_dir = root / 'data/agent'
    assert agent_dir.is_dir() and agent_dir.stat().st_mode & 0o777 == 0o700
    creates = commands(docker, 'container', 'create')
    assert creates[0] == [
        'container', 'create', '--name', 'bajiao-agent', '--restart', 'unless-stopped',
        '--network', 'bajiao-net', '--label', 'io.bajiao.project=bajiao',
        '--label', 'org.opencontainers.image.revision=' + REV, '--env', 'APP_REVISION=' + REV,
        '--env', 'MAIN_URL=http://binance-bstocks-analysis-bot:8000',
        '--mount', f'type=bind,src={agent_dir},dst=/agent-data', request()['agent_image']]
    app_create = creates[1]
    assert 'AGENT_URL=http://bajiao-agent:8080' in app_create
    assert app_create[app_create.index('--network') + 1] == 'bstocks-bot_default'
    order = [args[:2] + args[-1:] for args, _ in docker.commands if args[0] in {'container', 'network'}
             and args[1] in {'create', 'start', 'connect'}]
    assert order == [
        ['network', 'create', 'bajiao-net'],
        ['container', 'create', request()['agent_image']], ['container', 'start', cd.AGENT_CONTAINER],
        ['container', 'create', request()['image']],
        ['network', 'connect', cd.CONTAINER], ['container', 'start', cd.CONTAINER]]
    assert commands(docker, 'network', 'connect') == [['network', 'connect', 'bajiao-net', cd.CONTAINER]]
    assert docker.containers[cd.AGENT_CONTAINER]['Image'] == AGENT_NEW
    assert docker.containers[cd.AGENT_CONTAINER]['State']['Running']
    assert cd.SHARED_NETWORK in docker.containers[cd.CONTAINER]['NetworkSettings']['Networks']
    marker = json.loads(runner.marker.read_text())
    assert marker['agent_image'] == request()['agent_image']
    assert marker['agent_image_id'] == AGENT_NEW
    assert marker['managed_agent_images'] == [AGENT_NEW]
    assert marker['pending_cleanup'] == []


def test_agent_container_receives_no_env_file_or_published_port(setup):
    runner, docker, _ = setup
    runner.deploy(request())
    agent = commands(docker, 'container', 'create')[0]
    assert '--env-file' not in agent and '--publish' not in agent and '-p' not in agent


def test_redeploy_replaces_previous_agent_and_cleans_its_image(setup):
    runner, docker, _ = setup
    docker.add_previous_agent()
    removed = runner.deploy(request())
    assert AGENT_OLD in removed and AGENT_OLD not in docker.images
    assert AGENT_NEW in docker.images and NEW in docker.images
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
    assert docker.containers[cd.AGENT_CONTAINER]['Image'] == AGENT_NEW
    assert commands(docker, 'network', 'create') == []
    renames = commands(docker, 'container', 'rename')
    assert re.fullmatch(r'bajiao-agent\.rollback-10-[0-9]+', renames[0][3])
    assert renames[0][2] == cd.AGENT_CONTAINER
    stop_agent = docker.commands.index((['container', 'stop', '--time', '30', cd.AGENT_CONTAINER], None))
    assert stop_agent < docker.commands.index((renames[0], None))
    marker = json.loads(runner.marker.read_text())
    assert marker['managed_agent_images'] == [AGENT_NEW]


def test_agent_health_failure_restores_previous_agent_without_touching_app(setup):
    runner, docker, root = setup
    docker.add_previous_agent()
    docker.agent_new_healthy = False
    with pytest.raises(cd.DeploymentError, match='previous agent'):
        runner.deploy(request())
    assert not any(args[:2] == ['container', 'stop'] and args[-1] == cd.CONTAINER
                   for args, _ in docker.commands)
    assert docker.containers[cd.CONTAINER] == docker.old
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
    agent = docker.containers[cd.AGENT_CONTAINER]
    assert agent['Image'] == AGENT_OLD and agent['State']['Running']
    assert not runner.marker.exists()
    assert not any(args[:2] == ['image', 'rm'] for args, _ in docker.commands)


def test_first_deploy_agent_failure_removes_new_agent(setup):
    runner, docker, _ = setup
    docker.agent_new_healthy = False
    with pytest.raises(cd.DeploymentError, match='previous agent'):
        runner.deploy(request())
    assert list(docker.containers) == [cd.CONTAINER]
    assert docker.containers[cd.CONTAINER] == docker.old


def test_app_health_failure_rolls_back_app_and_agent(setup):
    runner, docker, root = setup
    docker.add_previous_agent()
    docker.new_healthy = False
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
    assert docker.containers[cd.CONTAINER]['Image'] == OLD
    assert docker.containers[cd.CONTAINER]['State']['Running']
    agent = docker.containers[cd.AGENT_CONTAINER]
    assert agent['Image'] == AGENT_OLD and agent['State']['Running']
    assert not runner.marker.exists()
    assert not any(args[:2] == ['image', 'rm'] for args, _ in docker.commands)


def test_first_deploy_app_failure_removes_new_agent_and_restores_old_app(setup):
    runner, docker, _ = setup
    docker.new_healthy = False
    with pytest.raises(cd.DeploymentError, match='previous container restored'):
        runner.deploy(request())
    assert list(docker.containers) == [cd.CONTAINER]
    assert docker.containers[cd.CONTAINER]['Image'] == OLD
    assert docker.containers[cd.CONTAINER]['State']['Running']


def test_partial_agent_create_is_removed_and_previous_agent_restored(setup):
    runner, docker, _ = setup
    docker.add_previous_agent()
    original = docker.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[:2] == ['container', 'create'] and cd.AGENT_CONTAINER in args:
            raise cd.DeploymentError('fixture partial agent create')
        return result
    docker.run = run
    with pytest.raises(cd.DeploymentError, match='previous agent'):
        runner.deploy(request())
    assert docker.containers[cd.AGENT_CONTAINER]['Image'] == AGENT_OLD
    assert docker.containers[cd.AGENT_CONTAINER]['State']['Running']
    assert docker.containers[cd.CONTAINER] == docker.old


def test_unlabeled_shared_network_is_refused(setup):
    runner, docker, _ = setup
    docker.networks[cd.SHARED_NETWORK] = {'Name': cd.SHARED_NETWORK, 'Labels': None}
    with pytest.raises(cd.DeploymentError, match='network'):
        runner.deploy(request())
    assert not commands(docker, 'container', 'stop')
    assert not commands(docker, 'container', 'create')
    assert not commands(docker, 'network', 'create')
    assert docker.containers[cd.CONTAINER]['State']['Running']


def test_foreign_agent_container_is_never_touched(setup):
    runner, docker, _ = setup
    docker.add_previous_agent(project=False)
    with pytest.raises(cd.DeploymentError, match='agent'):
        runner.deploy(request())
    assert not any(args[:2] in (['container', 'stop'], ['container', 'rename'], ['container', 'rm'],
                                ['container', 'create']) for args, _ in docker.commands)
    assert docker.containers[cd.AGENT_CONTAINER]['Image'] == AGENT_OLD


def test_agent_image_identity_mismatch_leaves_everything_untouched(setup):
    runner, docker, _ = setup
    docker.images[AGENT_NEW]['Config']['Labels']['org.opencontainers.image.revision'] = 'f' * 40
    with pytest.raises(cd.DeploymentError, match='identity'):
        runner.deploy(request())
    assert not commands(docker, 'container', 'stop')
    assert not commands(docker, 'network', 'create')


def test_symlinked_agent_data_directory_is_refused(setup, tmp_path_factory):
    runner, docker, root = setup
    outside = tmp_path_factory.mktemp('outside')
    (root / 'data/agent').symlink_to(outside, target_is_directory=True)
    with pytest.raises(cd.DeploymentError, match='symlink'):
        runner.deploy(request())
    assert not commands(docker, 'container', 'stop')
    assert not commands(docker, 'container', 'create')


def test_existing_agent_dir_is_kept(setup):
    runner, docker, root = setup
    (root / 'data/agent').mkdir(mode=0o700)
    (root / 'data/agent/internal_token').write_text('fixture-token')
    runner.deploy(request())
    assert (root / 'data/agent/internal_token').read_text() == 'fixture-token'


def test_network_connect_is_skipped_when_app_already_on_shared_network(setup):
    runner, docker, _ = setup
    docker.add_previous_agent()
    for container in (docker.old, docker.containers[cd.CONTAINER]):
        container['NetworkSettings']['Networks'][cd.SHARED_NETWORK] = {}
    docker.old['HostConfig']['NetworkMode'] = cd.SHARED_NETWORK
    docker.containers[cd.CONTAINER]['HostConfig']['NetworkMode'] = cd.SHARED_NETWORK
    runner.deploy(request())
    assert commands(docker, 'network', 'connect') == []
    assert docker.containers[cd.CONTAINER]['Image'] == NEW


def test_second_deploy_reconnects_new_app_container(setup):
    runner, docker, _ = setup
    runner.deploy(request())
    docker.old = copy.deepcopy(docker.containers[cd.CONTAINER])
    docker.commands.clear()
    runner.deploy(request(run_number=11))
    assert commands(docker, 'network', 'connect') == [['network', 'connect', 'bajiao-net', cd.CONTAINER]]
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}


def test_failed_agent_rollback_container_cleanup_is_reconciled(setup):
    runner, docker, _ = setup
    docker.add_previous_agent()
    docker.failure = lambda args: args[:2] == ['container', 'rm']
    with pytest.raises(cd.DeploymentError, match='healthy.*cleanup failed'):
        runner.deploy(request())
    pending = json.loads(runner.marker.read_text())['pending_cleanup']
    assert {entry['image_id'] for entry in pending} == {OLD, AGENT_OLD}
    assert any(re.fullmatch(r'bajiao-agent\.rollback-10-[0-9]+', entry['name']) for entry in pending)
    docker.failure = None
    removed = runner.deploy(request(run_number=11))
    assert AGENT_OLD in removed and AGENT_OLD not in docker.images
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}


@pytest.mark.parametrize('name', ['bajiao-agent', 'bajiao-agent.rollback-0-1', 'bajiao-agentx.rollback-1-1',
                                  'bajiao-agent.rollback-1-1;rm'])
def test_pending_agent_cleanup_names_are_strict(setup, name):
    runner, docker, _ = setup
    with pytest.raises(cd.DeploymentError, match='Invalid pending'):
        runner.cleanup_containers([{'name': name, 'image_id': AGENT_OLD}])


def test_pending_agent_rollback_with_changed_image_is_not_removed(setup):
    runner, docker, _ = setup
    name = cd.AGENT_CONTAINER + '.rollback-3-1'
    docker.containers[name] = {'Image': OTHER, 'State': {'Running': False}}
    with pytest.raises(cd.DeploymentError, match='identity'):
        runner.cleanup_containers([{'name': name, 'image_id': AGENT_OLD}])
    assert name in docker.containers


def test_cleanup_removes_old_agent_images_but_keeps_current(setup):
    runner, docker, _ = setup
    stale = 'sha256:' + '8' * 64
    docker.images[stale] = dict(image(stale), RepoTags=[cd.AGENT_REPOSITORY + ':old'],
                                RepoDigests=[cd.AGENT_REPOSITORY + '@' + stale])
    docker.images[AGENT_NEW]['RepoDigests'] = [cd.AGENT_REPOSITORY + '@' + AGENT_NEW]
    removed = runner.deploy(request())
    assert stale in removed and stale not in docker.images
    assert AGENT_NEW not in removed and AGENT_NEW in docker.images
    assert NEW in docker.images


@pytest.mark.parametrize('operation', ['stop', 'rename'])
def test_lost_agent_acknowledgement_recovers_previous_agent(setup, operation):
    runner, docker, _ = setup
    docker.add_previous_agent()
    docker.uncertain_operation = operation  # first stop/rename of the run is the agent's
    with pytest.raises(cd.DeploymentError, match='previous agent'):
        runner.deploy(request())
    assert set(docker.containers) == {cd.CONTAINER, cd.AGENT_CONTAINER}
    agent = docker.containers[cd.AGENT_CONTAINER]
    assert agent['Image'] == AGENT_OLD and agent['State']['Running']
    assert docker.containers[cd.CONTAINER] == docker.old


def test_unconfirmed_agent_rollback_is_reported(setup):
    runner, docker, _ = setup
    docker.add_previous_agent()
    docker.new_healthy = False
    docker.failure = lambda args: args[:2] == ['container', 'rename'] and args[-1] == cd.AGENT_CONTAINER
    with pytest.raises(cd.DeploymentError, match='rollback could not be confirmed'):
        runner.deploy(request())
    assert docker.containers[cd.CONTAINER]['Image'] == OLD  # application still restored first
    assert docker.containers[cd.CONTAINER]['State']['Running']
