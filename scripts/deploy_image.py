#!/usr/bin/env python3
"""Restricted SSH receiver for the bajiao image pipeline (Python stdlib only).

Install root-owned at /opt/bstocks-bot/ci/deploy_image.py. The dedicated SSH
key must force this command and disable forwarding/PTY. Never accept paths,
shell commands, or an arbitrary image repository from the caller.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path('/opt/bstocks-bot')
CONTAINER = 'binance-bstocks-analysis-bot'
REPOSITORY = 'ghcr.io/david-yang-pixel/bajiao'
PROJECT_LABEL = 'io.bajiao.project'
LEGACY_REPOSITORY = 'bstocks-bot-bstocks-bot'
AGENT_CONTAINER = 'bajiao-agent'
AGENT_REPOSITORY = 'ghcr.io/david-yang-pixel/bajiao-agent'
SHARED_NETWORK = 'bajiao-net'
MAIN_URL = 'http://' + CONTAINER + ':8000'
AGENT_URL = 'http://' + AGENT_CONTAINER + ':8080'
ROLLBACK_NAME = r'\.rollback-[1-9][0-9]*-[0-9]+'
# FMP_API_KEY is no longer used by the app; it stays listed only so an existing
# value in a running container's runtime env file is migrated, not dropped.
APP_ENV = {
    'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'BINANCE_MCP_URL',
    'FEISHU_WEBHOOK_URL', 'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
    'WECOM_WEBHOOK_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL',
    'FMP_API_KEY', 'ADMIN_PASSWORD', 'DB_PATH',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
}


class DeploymentError(RuntimeError):
    pass


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) != {
        'revision', 'image', 'agent_image', 'registry_user', 'registry_token', 'run_number'
    }:
        raise DeploymentError('Invalid deployment request fields')
    rules = {
        'revision': r'[0-9a-f]{40}',
        'image': re.escape(REPOSITORY) + r'@sha256:[0-9a-f]{64}',
        'agent_image': re.escape(AGENT_REPOSITORY) + r'@sha256:[0-9a-f]{64}',
        'registry_user': r'[A-Za-z0-9][A-Za-z0-9_\[\]-]{0,99}',
        'registry_token': r'[A-Za-z0-9_.-]{8,2048}',
    }
    for key, pattern in rules.items():
        if not isinstance(payload[key], str) or not re.fullmatch(pattern, payload[key]):
            raise DeploymentError(f'Invalid deployment field: {key}')
    if type(payload['run_number']) is not int or payload['run_number'] < 1:
        raise DeploymentError('Invalid deployment run number')
    return payload


class Docker:
    def run(self, args, *, input=None, optional=False):
        try:
            result = subprocess.run(
                ['docker', *args], input=input, text=True, capture_output=True, timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentError('Docker operation unavailable or timed out') from None
        if result.returncode:
            if optional and ('No such' in result.stderr or 'not found' in result.stderr):
                return None
            # Never echo provider responses, inspection payloads, or registry credentials.
            raise DeploymentError('Docker operation failed; inspect server daemon logs')
        return result.stdout

    def container(self, name):
        data = self.run(['container', 'inspect', name], optional=True)
        return json.loads(data)[0] if data is not None else None

    def image(self, image):
        data = self.run(['image', 'inspect', image], optional=True)
        return json.loads(data)[0] if data is not None else None

    def network(self, name):
        data = self.run(['network', 'inspect', name], optional=True)
        return json.loads(data)[0] if data is not None else None

    def project_images(self):
        return set(self.run(['image', 'ls', '--quiet', '--no-trunc', '--filter',
                             f'label={PROJECT_LABEL}=bajiao']).split())

    def used_images(self):
        ids = self.run(['container', 'ls', '--all', '--quiet']).split()
        if not ids:
            return set()
        return {row['Image'] for row in json.loads(self.run(['container', 'inspect', *ids]))}


def image_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'sha256:[0-9a-f]{64}', value))


def application_state(path):
    """Compare protected values, not WAL bytes or notification logs. No value is logged."""
    if not path.is_file() or path.is_symlink():
        raise DeploymentError('Production database missing or symlinked')
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=15) as db:
        configs = db.execute('SELECT key,value FROM system_configs ORDER BY key').fetchall()
        positions = db.execute('''SELECT id,symbol,buy_price,quantity,take_profit_price,
            stop_loss_price,check_interval_hours,ladder_alerts,include_ai_analysis,is_active
            FROM positions ORDER BY id''').fetchall()
    return hashlib.sha256(json.dumps([configs, positions], ensure_ascii=False).encode()).hexdigest()


def database_backup(source, destination):
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=15) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    destination.chmod(0o600)


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def health_probe(revision=None):
    try:
        # Health is deliberately exempt from console authentication.
        with urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5) as response:
            data = json.load(response)
        return (data.get('status') == 'ok' and data.get('service') == 'binance-bstocks-bot'
                and (revision is None or data.get('revision') == revision))
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False


class Deployment:
    def __init__(self, root=ROOT, docker=None, probe=health_probe, timeout=180):
        self.root = Path(root)
        self.docker = docker or Docker()
        self.probe = probe
        self.timeout = timeout
        self.ci = self.root / 'ci'
        self.marker = self.ci / 'current.json'

    def validate_current(self, old):
        if not old or not old['State'].get('Running') or not image_id(old['Image']):
            raise DeploymentError('Expected a running production application container')
        mounts = {m['Destination']: m for m in old.get('Mounts', [])}
        allowed = {'/app/data': 'data', '/app/logs': 'logs', '/app/backend': 'backend'}
        if '/app/data' not in mounts:
            raise DeploymentError('Production data mount is missing')
        for destination, mount in mounts.items():
            if (destination not in allowed or mount['Type'] != 'bind'
                    or Path(mount['Source']) != self.root / allowed[destination]):
                raise DeploymentError('Unexpected production mount; refusing migration')
        network = old['HostConfig']['NetworkMode']
        if network in {'host', 'none'} or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', network):
            raise DeploymentError('Unsupported production network')
        ports = old['HostConfig'].get('PortBindings')
        if not isinstance(ports, dict) or set(ports) != {'8000/tcp'} or not ports['8000/tcp']:
            raise DeploymentError('Unexpected application port mapping')
        for binding in ports['8000/tcp']:
            if (binding.get('HostPort') != '8000'
                    or binding.get('HostIp', '') not in {'', '0.0.0.0', '127.0.0.1', '::', '::1'}):
                raise DeploymentError('Unexpected application host port')
        old_image = self.docker.image(old['Image'])
        if not old_image:
            raise DeploymentError('Previous image unavailable for rollback')
        labels = old_image.get('Config', {}).get('Labels') or {}
        container_labels = old.get('Config', {}).get('Labels') or {}
        legacy = (old['Config']['Image'] in {LEGACY_REPOSITORY, LEGACY_REPOSITORY + ':latest'}
                  and container_labels.get('com.docker.compose.project') == 'bstocks-bot'
                  and container_labels.get('com.docker.compose.service') == 'bstocks-bot')
        if labels.get(PROJECT_LABEL) != 'bajiao' and not legacy:
            raise DeploymentError('Current image is not owned by this project')

    def wait_ready(self, expected_image, revision=None):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            current = self.docker.container(CONTAINER)
            if (not current or current['Image'] != expected_image
                    or current['State'].get('Status') in {'exited', 'dead'}):
                raise DeploymentError('Application exited or image identity changed')
            health = current['State'].get('Health', {}).get('Status')
            if health == 'unhealthy':
                raise DeploymentError('Application container is unhealthy')
            if health == 'healthy' and self.probe(revision):
                return
            time.sleep(2)
        raise DeploymentError('Application health verification timed out')

    def wait_agent_ready(self, expected_image):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            current = self.docker.container(AGENT_CONTAINER)
            if (not current or current['Image'] != expected_image
                    or current['State'].get('Status') in {'exited', 'dead'}):
                raise DeploymentError('Agent exited or image identity changed')
            health = current['State'].get('Health', {}).get('Status')
            if health == 'unhealthy':
                raise DeploymentError('Agent container is unhealthy')
            if health == 'healthy':
                return
            time.sleep(2)
        raise DeploymentError('Agent health verification timed out')

    def validate_agent(self, agent):
        # First deployment has no agent; an existing one must belong to this project.
        if agent is None:
            return
        labels = agent.get('Config', {}).get('Labels') or {}
        if labels.get(PROJECT_LABEL) != 'bajiao' or not image_id(agent.get('Image')):
            raise DeploymentError('Existing agent container is not owned by this project')

    def ensure_network(self):
        network = self.docker.network(SHARED_NETWORK)
        if network is None:
            self.docker.run(['network', 'create', '--label', f'{PROJECT_LABEL}=bajiao', SHARED_NETWORK])
        elif (network.get('Labels') or {}).get(PROJECT_LABEL) != 'bajiao':
            raise DeploymentError('Shared network exists but is not owned by this project')

    def agent_dir(self):
        return self.root / 'data' / 'agent'

    def ensure_agent_dir(self):
        directory = self.agent_dir()
        if not directory.parent.is_dir() or directory.parent.is_symlink() or directory.is_symlink():
            raise DeploymentError('Agent data directory or its parent cannot be missing or a symlink')
        if not directory.exists():
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        elif not directory.is_dir():
            raise DeploymentError('Agent data path is not a directory')

    def pull(self, request):
        with tempfile.TemporaryDirectory(prefix='registry-', dir=self.ci) as directory:
            os.chmod(directory, 0o700)
            self.docker.run(['--config', directory, 'login', 'ghcr.io', '--username',
                             request['registry_user'], '--password-stdin'],
                            input=request['registry_token'] + '\n')
            self.docker.run(['--config', directory, 'pull', request['image']])
            self.docker.run(['--config', directory, 'pull', request['agent_image']])
        identities = []
        for reference in (request['image'], request['agent_image']):
            image = self.docker.image(reference)
            labels = (image or {}).get('Config', {}).get('Labels') or {}
            if (not image or not image_id(image['Id']) or labels.get(PROJECT_LABEL) != 'bajiao'
                    or labels.get('org.opencontainers.image.revision') != request['revision']):
                raise DeploymentError('Image project or revision identity does not match')
            identities.append(image['Id'])
        return identities[0], identities[1]

    def write_runtime(self, old, target):
        values = {}
        for entry in old['Config'].get('Env', []):
            key, separator, value = entry.partition('=')
            if separator and key in APP_ENV:
                if any(c in value for c in '\r\n\0'):
                    raise DeploymentError('Multiline runtime configuration cannot be migrated safely')
                values[key] = value
        if values.get('DB_PATH') != '/app/data/bstocks.db':
            raise DeploymentError('Unexpected runtime database path')
        with target.open('w', encoding='utf-8') as stream:
            os.chmod(target, 0o600)
            for key, value in sorted(values.items()):
                stream.write(f'{key}={value}\n')

    def create(self, request, old, runtime):
        args = ['container', 'create', '--name', CONTAINER, '--restart', 'unless-stopped',
                '--network', old['HostConfig']['NetworkMode'],
                '--label', f'{PROJECT_LABEL}=bajiao', '--label',
                'org.opencontainers.image.revision=' + request['revision']]
        env_file = self.root / '.env'
        if env_file.exists():
            if not env_file.is_file() or env_file.is_symlink():
                raise DeploymentError('Unexpected production environment file')
            args.extend(['--env-file', str(env_file)])
        # Effective existing application env remains authoritative. Never rewrite .env.
        args.extend(['--env-file', str(runtime), '--env', 'DB_PATH=/app/data/bstocks.db',
                     '--env', 'APP_REVISION=' + request['revision'], '--env', 'AGENT_URL=' + AGENT_URL])
        for directory in ('data', 'logs'):
            target = self.root / directory
            if not target.is_dir() or target.is_symlink():
                raise DeploymentError('Missing or symlinked persistent directory')
            args.extend(['--mount', f'type=bind,src={target},dst=/app/{directory}'])
        for binding in old['HostConfig']['PortBindings']['8000/tcp']:
            address = binding.get('HostIp', '')
            if ':' in address:
                address = '[' + address + ']'
            mapping = (address + ':' if address else '') + '8000:8000/tcp'
            args.extend(['--publish', mapping])
        args.append(request['image'])
        # No backend source mount: application bytes come from the tested image.
        self.docker.run(args)

    def connect_shared_network(self):
        current = self.docker.container(CONTAINER)
        if not current:
            raise DeploymentError('Created application container could not be located')
        if SHARED_NETWORK not in ((current.get('NetworkSettings') or {}).get('Networks') or {}):
            self.docker.run(['network', 'connect', SHARED_NETWORK, CONTAINER])

    def create_agent(self, request):
        directory = self.agent_dir()
        if not directory.is_dir() or directory.is_symlink():
            raise DeploymentError('Missing or symlinked agent data directory')
        # Internal only: no published port and no production .env (agent reads /agent-data).
        self.docker.run(['container', 'create', '--name', AGENT_CONTAINER, '--restart', 'unless-stopped',
                         '--network', SHARED_NETWORK, '--label', f'{PROJECT_LABEL}=bajiao',
                         '--label', 'org.opencontainers.image.revision=' + request['revision'],
                         '--env', 'APP_REVISION=' + request['revision'], '--env', 'MAIN_URL=' + MAIN_URL,
                         '--mount', f'type=bind,src={directory},dst=/agent-data', request['agent_image']])

    def rollback(self, old, backup_name, renamed, candidate):
        # Reconcile actual state: a successful stop/rename may lose its CLI acknowledgement.
        backup = self.docker.container(backup_name)
        current = self.docker.container(CONTAINER)
        if backup:
            if backup['Image'] != old['Image']:
                raise DeploymentError('Rollback container identity mismatch')
            if current:
                if not candidate:
                    raise DeploymentError('Unexpected container appeared during rollback')
                if current['State'].get('Running'):
                    self.docker.run(['container', 'stop', '--time', '30', CONTAINER])
                self.docker.run(['container', 'rm', CONTAINER])
            self.docker.run(['container', 'rename', backup_name, CONTAINER])
        elif not current or current['Image'] != old['Image']:
            raise DeploymentError('Previous container could not be located for rollback')
        self.docker.run(['container', 'start', CONTAINER])
        self.wait_ready(old['Image'])
        # Do not restore SQLite automatically: preserve writes accepted by the new app.

    def rollback_agent(self, previous, backup_name, candidate):
        # Same reconciliation as the application: trust inspected state, not CLI acknowledgements.
        backup = self.docker.container(backup_name) if previous else None
        current = self.docker.container(AGENT_CONTAINER)
        if previous and not backup and current and current['Image'] == previous['Image']:
            current = None  # never renamed: the inspected container is still the previous agent
            restore = False
        else:
            restore = bool(previous)
        if current:
            if not candidate:
                raise DeploymentError('Unexpected agent container appeared during rollback')
            if current['State'].get('Running'):
                self.docker.run(['container', 'stop', '--time', '30', AGENT_CONTAINER])
            self.docker.run(['container', 'rm', AGENT_CONTAINER])
        if not previous:
            return  # first agent deployment: nothing to restore, old app does not need it
        if restore:
            if not backup or backup['Image'] != previous['Image']:
                raise DeploymentError('Previous agent could not be located for rollback')
            self.docker.run(['container', 'rename', backup_name, AGENT_CONTAINER])
        self.docker.run(['container', 'start', AGENT_CONTAINER])
        self.wait_agent_ready(previous['Image'])

    def cleanup_images(self, current_images, managed, managed_agent=()):
        candidates = self.docker.project_images() | set(managed) | set(managed_agent)
        used = self.docker.used_images()  # includes stopped containers, not just running ones
        removed = []
        for candidate in sorted(candidates):
            if not image_id(candidate) or candidate in current_images or candidate in used:
                continue
            image = self.docker.image(candidate)
            if not image:
                continue
            tags = image.get('RepoTags') or []
            digests = image.get('RepoDigests') or []
            def allowed_digest(reference):
                return (reference.startswith((REPOSITORY + '@sha256:', AGENT_REPOSITORY + '@sha256:'))
                        or (candidate in managed and reference.startswith(LEGACY_REPOSITORY + '@sha256:')))
            def allowed_tag(reference):
                return (reference.startswith((REPOSITORY + ':', AGENT_REPOSITORY + ':'))
                        or reference == LEGACY_REPOSITORY + ':latest' or allowed_digest(reference))
            if (any(not allowed_tag(tag) for tag in tags)
                    or any(not allowed_digest(digest) for digest in digests)):
                print('Skipping old image with references outside this project', flush=True)
                continue
            labels = image.get('Config', {}).get('Labels') or {}
            if labels.get(PROJECT_LABEL) != 'bajiao' and candidate not in managed:
                continue  # unlabeled images are only ever the recorded legacy application image
            for tag in tags:
                self.docker.run(['image', 'rm', tag])
            if self.docker.image(candidate):
                self.docker.run(['image', 'rm', candidate])  # deliberately no --force / prune
            if self.docker.image(candidate):
                raise DeploymentError('Old project image removal could not be confirmed')
            removed.append(candidate)
        return removed

    def cleanup_containers(self, pending):
        for entry in pending:
            if (not isinstance(entry, dict) or set(entry) != {'name', 'image_id'}
                    or not isinstance(entry['name'], str)
                    or not re.fullmatch('(?:' + re.escape(CONTAINER) + '|' + re.escape(AGENT_CONTAINER) + ')'
                                        + ROLLBACK_NAME, entry['name'])
                    or not image_id(entry['image_id'])):
                raise DeploymentError('Invalid pending project cleanup record')
            container = self.docker.container(entry['name'])
            if container is None:
                continue
            if container['Image'] != entry['image_id'] or container['State'].get('Running'):
                raise DeploymentError('Pending rollback container identity or stopped state changed')
            self.docker.run(['container', 'rm', entry['name']])

    def deploy(self, payload):
        request = validate_request(payload)
        self.ci.mkdir(mode=0o700, exist_ok=True)
        if self.ci.is_symlink():
            raise DeploymentError('Deployment directory cannot be a symlink')
        with (self.root / '.deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = json.loads(self.marker.read_text()) if self.marker.exists() else {}
            if request['run_number'] < marker.get('run_number', 0):
                raise DeploymentError('Refusing an older workflow run')
            old = self.docker.container(CONTAINER)
            self.validate_current(old)
            previous_agent = self.docker.container(AGENT_CONTAINER)
            self.validate_agent(previous_agent)
            managed = set(marker.get('managed_images', [])) | {old['Image']}
            managed_agent = set(marker.get('managed_agent_images', []))
            if previous_agent:
                managed_agent.add(previous_agent['Image'])
            target_image, target_agent_image = self.pull(request)
            managed.add(target_image)
            managed_agent.add(target_agent_image)
            self.ensure_network()
            self.ensure_agent_dir()
            backup_name = CONTAINER + '.rollback-' + str(request['run_number']) + '-' + str(time.time_ns())
            agent_backup_name = AGENT_CONTAINER + '.rollback-' + str(request['run_number']) + '-' + str(time.time_ns())
            backup = self.root / 'backups' / ('cicd-' + request['revision'][:12] + '-' + str(time.time_ns()))
            backup.mkdir(mode=0o700, parents=True)
            runtime = backup / 'runtime.env'
            self.write_runtime(old, runtime)
            database = self.root / 'data' / 'bstocks.db'
            env_file = self.root / '.env'
            env_hash = hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
            pending = list(marker.get('pending_cleanup', [])) + [{'name': backup_name, 'image_id': old['Image']}]
            if previous_agent:
                pending.append({'name': agent_backup_name, 'image_id': previous_agent['Image']})
            stopped = renamed = candidate = agent_started = agent_candidate = False
            try:
                # Agent first: if it cannot become healthy the application is never touched.
                agent_started = True  # attempt agent recovery even if an acknowledgement is lost
                if previous_agent:
                    self.docker.run(['container', 'stop', '--time', '30', AGENT_CONTAINER])
                    self.docker.run(['container', 'rename', AGENT_CONTAINER, agent_backup_name])
                agent_candidate = True  # also remove a partially created agent if Docker errors
                self.create_agent(request)
                self.docker.run(['container', 'start', AGENT_CONTAINER])
                self.wait_agent_ready(target_agent_image)
                stopped = True  # attempt recovery even if the stop acknowledgement is lost
                self.docker.run(['container', 'stop', '--time', '30', CONTAINER])
                before = application_state(database)
                database_backup(database, backup / 'bstocks.db')
                self.docker.run(['container', 'rename', CONTAINER, backup_name])
                renamed = True
                candidate = True  # also remove a partially created container if Docker errors
                self.create(request, old, runtime)
                self.connect_shared_network()
                self.docker.run(['container', 'start', CONTAINER])
                self.wait_ready(target_image, request['revision'])
                if application_state(database) != before:
                    raise DeploymentError('Protected database configuration or holdings changed')
                after_env = hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
                if after_env != env_hash:
                    raise DeploymentError('Production environment file changed during deployment')
                current_marker = {
                    'revision': request['revision'], 'image': request['image'],
                    'image_id': target_image, 'run_number': request['run_number'],
                    'managed_images': sorted(managed), 'deployed_at': int(time.time()),
                    'agent_image': request['agent_image'], 'agent_image_id': target_agent_image,
                    'managed_agent_images': sorted(managed_agent), 'pending_cleanup': pending,
                }
                atomic_json(self.marker, current_marker)
            except Exception:
                confirmed = True
                if stopped:  # old application first: it is healthy without any agent
                    try:
                        self.rollback(old, backup_name, renamed, candidate)
                    except Exception:
                        confirmed = False
                if agent_started:
                    try:
                        self.rollback_agent(previous_agent, agent_backup_name, agent_candidate)
                    except Exception:
                        confirmed = False
                if not confirmed:
                    raise DeploymentError('Deployment failed and rollback could not be confirmed; inspect server') from None
                if stopped:
                    raise DeploymentError('Deployment failed; previous container restored and database kept') from None
                if agent_started:
                    raise DeploymentError('Deployment failed before application switch; previous agent state restored') from None
                raise DeploymentError('Deployment failed before container switch') from None
            # Commit succeeded: never roll a healthy application back because cleanup failed.
            try:
                self.cleanup_containers(pending)
                removed = self.cleanup_images({target_image, target_agent_image}, managed, managed_agent)
                current_marker['pending_cleanup'] = []
                current_marker['managed_images'] = sorted(identity for identity in managed if self.docker.image(identity))
                current_marker['managed_agent_images'] = sorted(
                    identity for identity in managed_agent if self.docker.image(identity))
                atomic_json(self.marker, current_marker)
            except Exception:
                raise DeploymentError('New version is healthy, but old-container/image cleanup failed; retry cleanup') from None
            print(json.dumps({'status': 'deployed', 'revision': request['revision'],
                              'removed_old_images': len(removed)}, ensure_ascii=False), flush=True)
            return removed


def main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # finish the locked transaction if SSH disconnects
    try:
        if os.environ.get('SSH_ORIGINAL_COMMAND', '') not in {'', 'deploy'}:
            raise DeploymentError('This SSH key only accepts the deployment protocol')
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise DeploymentError('Deployment request too large')
        Deployment().deploy(json.loads(raw))
    except Exception as exc:
        print(str(exc) if isinstance(exc, DeploymentError) else 'Deployment failed; inspect protected server logs', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
