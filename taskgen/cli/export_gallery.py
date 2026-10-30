"""Export selected, reviewed tasks into the private Vercel gallery bundle."""
import argparse
from pathlib import Path

from taskgen.pipeline.locks import coordinator_lock
from taskgen.publishing.gallery import export


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs', type=Path, nargs='+')
    ap.add_argument('--output', type=Path, default=Path('task-gallery/gallery_data'))
    args = ap.parse_args(argv)
    with coordinator_lock(Path('.gallery-export.lock')):
        export(args.runs, args.output)
