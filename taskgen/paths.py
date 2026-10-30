"""Repository locations. ROOT is the checkout; guidelines/, domains/ and runs/ are local, gitignored directories."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / 'runs'
DOMAINS = ROOT / 'domains'
GUIDELINES = ROOT / 'guidelines'
SKILL_FILE = ROOT / 'SKILL.md'
