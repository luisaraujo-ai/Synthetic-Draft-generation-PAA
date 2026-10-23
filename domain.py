"""Domain configuration: domains/<name>/{domain.json, axes.json, seeds.json, trials.json, limitations.json}.

Every stage reads its domain-specific text, files and lists from here, so adding aero or mechanical means adding a
folder, not editing code. limitations.json is written by mine.py.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
Kforge = HERE.parent / 'forge-ee-verifiers-library'
DOMAINS = HERE / 'domains'
GUIDELINES = HERE / 'guidelines'
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
        self.seeds_file = self.dir / 'seeds.json'
        self.trials_file = self.dir / 'trials.json'
        self.limitations_file = self.dir / 'limitations.json'

    def __getitem__(self, key):
        return self.cfg[key]

    def get(self, key, default=None):
        return self.cfg.get(key, default)

    def limitations(self):
        if not self.limitations_file.exists():
            raise SystemExit(f'no {self.limitations_file.relative_to(HERE)}: run `uv run mine.py --domain {self.name}` first')
        return json.loads(self.limitations_file.read_text())
