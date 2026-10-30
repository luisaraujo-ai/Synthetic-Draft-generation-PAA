"""Domain configuration: domains/<name>/{domain.json, axes.json, trials.json, limitations.json, ledger.json}.

Every stage reads its domain-specific text and lists from here, so adding aero or mechanical means adding a folder,
not editing code. limitations.json is written by the miner (taskgen/mining); ledger.json by the pipeline coordinators.
"""
import json

from taskgen.paths import DOMAINS, GUIDELINES, ROOT

CANARY = 'FAIR-FORGE-CANARY 3326b524-149d-466b-b660-7e68d982ee97'


def guidelines():
    """Every document in guidelines/, in file-name order, for the generator, judge and miner to follow."""
    return '\n\n'.join(f'<document name="{p.name}">\n{p.read_text()}\n</document>'
                       for p in sorted(GUIDELINES.glob('*.md')))


class Domain:
    def __init__(self, name):
        self.dir = DOMAINS / name
        if not (self.dir / 'domain.json').exists():
            have = ', '.join(sorted(p.name for p in DOMAINS.iterdir() if (p / 'domain.json').exists()))
            raise SystemExit(f'no domain "{name}" (have: {have})')
        self.cfg = json.loads((self.dir / 'domain.json').read_text())
        self.name = self.cfg['name']
        self.axes_file = self.dir / 'axes.json'
        self.trials_file = self.dir / 'trials.json'
        self.limitations_file = self.dir / 'limitations.json'
        self.ledger_file = self.dir / 'ledger.json'

    def __getitem__(self, key):
        return self.cfg[key]

    def get(self, key, default=None):
        return self.cfg.get(key, default)

    def limitations(self):
        if not self.limitations_file.exists():
            raise SystemExit(f'no {self.limitations_file.relative_to(ROOT)}: run `uv run mine.py --domain {self.name}` first')
        return json.loads(self.limitations_file.read_text())

    def axes(self):
        return json.loads(self.axes_file.read_text())

    def subdomains(self):
        return list(self.axes()['subdomain'])

    def families(self, subdomain):
        return self.axes()['subdomain'][subdomain]

    def vocab(self):
        a = self.axes()
        return {dim: a[key] for dim, key in self['descriptors'].items()}

    def ledger(self):
        return json.loads(self.ledger_file.read_text()) if self.ledger_file.exists() else []

    def save_ledger(self, entries):
        self.ledger_file.write_text(json.dumps(entries, indent=1, ensure_ascii=False))
