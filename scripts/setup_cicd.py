#!/usr/bin/env python3
"""Bootstrap a dedicated forced-command deployment key; never upload an existing SSH key."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPO = 'david-yang-pixel/bajiao'
KEY_COMMENT = 'bajiao-github-actions'

INSTALL = r'''
import base64,hashlib,json,os,pathlib,sys,tempfile
p=json.load(sys.stdin)
root=pathlib.Path('/opt/bstocks-bot')
if os.geteuid()!=0 or not (root/'data/bstocks.db').is_file():
    raise SystemExit('Expected existing root-managed production deployment')
ci=root/'ci'
if ci.is_symlink(): raise SystemExit('Unsafe deployment directory')
ci.mkdir(mode=0o700,exist_ok=True)
os.chmod(ci,0o700)
content=base64.b64decode(p['receiver'],validate=True)
if hashlib.sha256(content).hexdigest()!=p['sha256']: raise SystemExit('Receiver checksum mismatch')
compile(content,'deploy_image.py','exec')
with tempfile.NamedTemporaryFile(dir=ci,delete=False) as stream:
    os.chmod(stream.name,0o700)
    stream.write(content)
    target=stream.name
os.replace(target,ci/'deploy_image.py')
ssh=pathlib.Path.home()/'.ssh'
ssh.mkdir(mode=0o700,exist_ok=True)
authorized=ssh/'authorized_keys'
if authorized.is_symlink(): raise SystemExit('Unsafe authorized_keys')
existing=authorized.read_text() if authorized.exists() else ''
key=p['public_key'].strip()
if not key.startswith('ssh-ed25519 ') or '\n' in key: raise SystemExit('Invalid dedicated public key')
line='restrict,command="/usr/bin/python3 /opt/bstocks-bot/ci/deploy_image.py" '+key
if line not in existing.splitlines():
    with authorized.open('a') as stream:
        if existing and not existing.endswith('\n'): stream.write('\n')
        stream.write(line+'\n')
os.chmod(authorized,0o600)
print('Receiver installed; dedicated restricted key authorized')
'''

RETIRE_OLD_KEYS = r'''
import json,pathlib,sys
key=json.load(sys.stdin)['public_key'].strip()
p=pathlib.Path.home()/'.ssh/authorized_keys'
rows=p.read_text().splitlines(keepends=True)
# Only rotate this tool's own keys; preserve unrelated SSH authorization bytes.
rows=[row for row in rows if not row.rstrip().endswith(' bajiao-github-actions') or key in row]
p.write_text(''.join(rows))
p.chmod(0o600)
'''


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', default='zendesk-agent-server')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ssh_host):
        parser.error('Invalid SSH host alias')
    current = json.loads(subprocess.check_output(['gh', 'repo', 'view', '--json', 'nameWithOwner'], text=True))
    if current['nameWithOwner'] != REPO:
        raise SystemExit('Run from the intended GitHub repository')
    config = {}
    for line in subprocess.check_output(['ssh', '-G', args.ssh_host], text=True, stderr=subprocess.DEVNULL).splitlines():
        key, _, value = line.partition(' ')
        config[key] = value
    host, user, port = config['hostname'], config['user'], config['port']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host) or user != 'root' or not port.isdigit():
        raise SystemExit('Expected an existing direct root SSH deployment target')
    run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', args.ssh_host, 'true'])
    key_host = config.get('hostkeyalias', host)
    lookup = key_host if port == '22' else f'[{key_host}]:{port}'
    known = []
    for filename in config.get('userknownhostsfile', '~/.ssh/known_hosts').split():
        path = Path(filename).expanduser()
        if path.is_file():
            probe = subprocess.run(['ssh-keygen', '-F', lookup, '-f', str(path)], capture_output=True, text=True)
            if probe.returncode == 0:
                known.extend(row for row in probe.stdout.splitlines() if row and not row.startswith('#'))
    if not known:
        raise SystemExit('No previously trusted server host key found; verify and trust it manually first')
    receiver = Path(__file__).with_name('deploy_image.py').read_bytes()
    scratch = os.environ.get('PI_SCRATCH_DIR')
    with tempfile.TemporaryDirectory(prefix='bajiao-deploy-key-', dir=scratch) as temp:
        directory = Path(temp)
        os.chmod(directory, 0o700)
        key = directory / 'id_ed25519'
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', KEY_COMMENT, '-f', str(key)])
        public = key.with_suffix('.pub').read_text().strip()
        payload = {'receiver': base64.b64encode(receiver).decode(), 'sha256': hashlib.sha256(receiver).hexdigest(), 'public_key': public}
        run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', args.ssh_host,
             'python3 -c ' + __import__('shlex').quote(INSTALL)], input=json.dumps(payload), text=True)
        # A private key is sent over stdin to gh, never as a command argument or log.
        for name, body in [('DEPLOY_KNOWN_HOSTS', '\n'.join(known) + '\n'), ('DEPLOY_SSH_KEY', key.read_text())]:
            run(['gh', 'secret', 'set', name, '--repo', REPO], input=body, text=True)
        for name, value in [('DEPLOY_HOST', host), ('DEPLOY_USER', user), ('DEPLOY_PORT', port)]:
            run(['gh', 'variable', 'set', name, '--repo', REPO, '--body', value])
        hosts = directory / 'known_hosts'
        hosts.write_text('\n'.join(known) + '\n')
        hosts.chmod(0o600)
        verification = subprocess.run([
            'ssh', '-F', '/dev/null', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={hosts}', '-o', 'ConnectTimeout=15',
            '-p', port, f'{user}@{host}', 'not-a-deployment-command',
        ], input='{}', text=True, capture_output=True)
        if verification.returncode == 0 or 'only accepts the deployment protocol' not in verification.stderr:
            raise SystemExit('Dedicated key restriction check failed; inspect server before enabling workflow')
        run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', args.ssh_host,
             'python3 -c ' + __import__('shlex').quote(RETIRE_OLD_KEYS)], input=json.dumps({'public_key': public}), text=True)
    print('CI/CD configured; forced-command key verified; local private-key temporary files removed')


if __name__ == '__main__':
    main()
