#!/usr/bin/env python3
"""Bring explicitly saved N1 clips home; compute local diagnostics and publish report.

Called by the existing n1-lab-pull job. No credentials or audio go into git.
No runtime voice policy is changed. Failures propagate to the caller's heartbeat.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

HOME = Path.home()
OH = HOME / '.local/bin/oh'
ROOT = HOME / '.local/state/hey/voice/lab'
EVALUATOR = HOME / 'CODELocalProjects/hey/voice_lab_eval.py'
PYTHON = HOME / '.local/venvs/carl-spk/bin/python'
REMOTE = '/data/app_data/n1/voice'
NAME = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(json|wav)$')
LIMIT = 128 * 1024 * 1024


def remote(command, payload=None):
    result = subprocess.run([str(OH), 'app', 'ssh', 'n1', command], input=payload,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if result.returncode:
        raise RuntimeError(f'N1 voice sync command failed ({result.returncode}): '
                           + result.stderr.decode(errors='replace')[-600:])
    return result.stdout


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'wb') as f:
        os.chmod(tmp, 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def unpack(archive, target):
    """Never extract arbitrary paths/links from a remote tar; copy validated bytes only."""
    if len(archive) > LIMIT:
        raise ValueError('voice archive too large')
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for member in tar:
            if member.isdir() and member.name in ('samples', 'samples/'):
                continue
            parts = member.name.split('/')
            if (not member.isfile() or len(parts) != 2 or parts[0] != 'samples'
                    or not NAME.fullmatch(parts[1]) or member.size > 600000):
                raise ValueError('unexpected archive member')
            total += member.size
            if total > LIMIT:
                raise ValueError('expanded voice archive too large')
            data = tar.extractfile(member).read()
            dest = target / parts[1]
            if dest.exists():
                if dest.read_bytes() != data:
                    raise ValueError(f'immutable saved clip changed: {parts[1]}')
            else:
                atomic(dest, data)


def main():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT / 'samples').mkdir(mode=0o700, exist_ok=True)
    # A missing voice dir means no clips saved yet, not a failed SSH request.
    manifest = remote("python3 -c \"import pathlib,json,hashlib; "
                      f"p=pathlib.Path('{REMOTE}/samples'); "
                      "print(json.dumps({f.name:hashlib.sha256(f.read_bytes()).hexdigest() "
                      "for f in sorted(p.glob('*.json'))}))\"")
    inventory = json.loads(manifest)
    if (not isinstance(inventory, dict) or len(inventory) > 200
            or any(not NAME.fullmatch(k) or not k.endswith('.json') for k in inventory)):
        raise ValueError('bad voice manifest or collection exceeds 200-clip sync limit')
    key = hashlib.sha256(manifest + EVALUATOR.read_bytes()).hexdigest()
    marker = ROOT / 'synced.sha256'
    if marker.exists() and marker.read_text().strip() == key:
        print(f'voice lab: {len(inventory)} saved clips already processed; no change')
        return
    if inventory:
        # Metadata is the server's commit marker. Include only committed pairs, not
        # in-flight temporary files or unrelated private files in the directory.
        names = ' '.join('samples/' + name for key in sorted(inventory)
                         for name in (key, key[:-5] + '.wav'))
        archive = remote(f'cd {REMOTE} && tar czf - {names}')
        unpack(archive, ROOT / 'samples')
    report = ROOT / 'report.json'
    subprocess.run([str(PYTHON), str(EVALUATOR), '--samples', str(ROOT / 'samples'),
                    '--report', str(report)], check=True, timeout=600)
    data = report.read_bytes()
    json.loads(data)  # refuse to publish a malformed report
    remote("python3 -c \"import os,sys,json,pathlib; "
           f"p=pathlib.Path('{REMOTE}'); p.mkdir(parents=True,exist_ok=True,mode=448); "
           "b=sys.stdin.buffer.read(); json.loads(b); t=p/'report.json.tmp'; "
           "f=open(t,'wb'); os.chmod(t,384); f.write(b); f.flush(); os.fsync(f.fileno()); f.close(); "
           "os.replace(t,p/'report.json')\"", data)
    atomic(marker, (key + '\n').encode())
    print(f'voice lab: {len(inventory)} labelled clips synced; local diagnostic report published; shadow unchanged')


if __name__ == '__main__':
    main()
