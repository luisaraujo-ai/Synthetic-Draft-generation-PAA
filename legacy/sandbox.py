"""Containers for the executable evaluation: the domain's solver/grader image, kforge mounted read-only at
/forge/kforge, no network.

A Sandbox binds a host work directory as /app with the task layout the solver sees (starter/, input_assets/,
output/) and, for grading, /tests (requirements.yaml, catalog/, models/). The evaluator's tools run through exec();
grade() runs `python -m kforge grade` and returns the parsed checks.json.
"""
import json
import shlex
import subprocess
import uuid
from pathlib import Path

from domain import KForge

STARTER = r'''
import json, sys
from kforge.kicad.schbuild import SchematicBuilder
spec_ports = json.load(open(sys.argv[1]))
b = SchematicBuilder("design", ["/usr/share/kicad/symbols"])
for i, (port, p) in enumerate(sorted(spec_ports.items())):
    ref = f"J{i + 1}"
    b.add(p.get("lib_id", "Connector:Conn_01x02_Pin"), ref, port, (50.8, 50.8 + 25.4 * i),
          footprint=p.get("footprint", ""), fields={"KFORGE_PORT": port})
    for role, pin in sorted(p["pins"].items()):
        b.connect(ref, str(pin), f"{port}_{role}")
b.save(sys.argv[2])
'''


class Sandbox:
    def __init__(self, domain, workdir, tests_dir=None, network=False):
        self.image = domain['images']['solver']
        self.kforge = REPO / domain['kforge']
        self.workdir = Path(workdir).resolve()
        self.tests = Path(tests_dir).resolve() if tests_dir else None
        self.name = f'taskgen-{uuid.uuid4().hex[:10]}'
        self.network = network

    def __enter__(self):
        for sub in ('starter', 'input_assets', 'output'):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        cmd = ['docker', 'run', '-d', '--rm', '--name', self.name, '--platform', 'linux/amd64',
               '-v', f'{self.kforge}:/forge/kforge:ro', '-e', 'PYTHONPATH=/forge',
               '-v', f'{self.workdir}:/app', '-w', '/app']
        if self.tests:
            cmd += ['-v', f'{self.tests}:/tests:ro']
        if not self.network:
            cmd += ['--network', 'none']
        subprocess.run(cmd + [self.image, 'sleep', 'infinity'], check=True, capture_output=True, text=True)
        self.exec('chmod -R a+rwX /app/output 2>/dev/null; true', timeout=60)
        return self

    def __exit__(self, *exc):
        subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True)

    def exec(self, command, timeout=600):
        """Run a shell command in the container; returns (exit code, combined output)."""
        if self.tests and (self.tests / 'run_kforge.py').exists():
            command = command.replace('python -m kforge ', 'python /tests/run_kforge.py ')
        try:
            r = subprocess.run(['docker', 'exec', self.name, 'bash', '-lc', command], capture_output=True, text=True,
                               timeout=timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
            return 124, out + f'\n[timed out after {timeout} s]'

    def write(self, path, content):
        """Write a file at a container path under /app (through the host bind mount)."""
        p = Path(path)
        if not str(p).startswith('/app/'):
            raise ValueError('only paths under /app can be written')
        host = self.workdir / p.relative_to('/app')
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(content)

    def grade(self, out='/tmp/kgrade', timeout=1800):
        """kforge grade of /app/output against /tests/requirements.yaml; returns (exit code, checks dict or None,
        log tail)."""
        code, log = self.exec(f'rm -rf {out} && python -m kforge grade --spec /tests/requirements.yaml '
                              f'--deliverable /app/output --out {out} --scratch /tmp/kscratch', timeout=timeout)
        _, raw = self.exec(f'cat {out}/checks.json 2>/dev/null', timeout=60)
        try:
            checks = json.loads(raw)
        except ValueError:
            checks = None
        return code, checks, log[-4000:]

    def make_starter(self, ports):
        """Build /app/starter/design/design.kicad_sch with one connector per published port."""
        self.write('/app/starter/.ports.json', json.dumps(ports))
        self.write('/app/starter/.make_starter.py', STARTER)
        code, log = self.exec('mkdir -p /app/starter/design && python /app/starter/.make_starter.py '
                              '/app/starter/.ports.json /app/starter/design/design.kicad_sch', timeout=120)
        self.exec('rm -f /app/starter/.ports.json /app/starter/.make_starter.py', timeout=30)
        return code, log


def summarize(checks, width=220):
    """Compact per-check view for prompts: id, status, measured, limit, unit, margin, detail."""
    if not checks:
        return 'no checks.json'
    rows = [f"reward {checks.get('reward')} counts {checks.get('counts')}"]
    for c in checks.get('checks', []):
        rows.append(f"- {c.get('id')} [{c.get('status')}] measured={c.get('measured')} limit={c.get('limit')} "
                    f"{c.get('unit') or ''} margin={c.get('margin')} {' '.join(str(c.get('detail') or '').split())[:width]}")
    return '\n'.join(rows)


def available(domain):
    r = subprocess.run(['docker', 'image', 'inspect', domain['images']['solver']], capture_output=True)
    return r.returncode == 0


def quote(s):
    return shlex.quote(s)
