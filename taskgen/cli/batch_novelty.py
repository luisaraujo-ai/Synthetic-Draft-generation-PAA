"""Rank-first novelty selection across Stage 1-reviewed runs; never generates new drafts."""
import argparse
from pathlib import Path

from taskgen.pipeline.batch_novelty import select
from taskgen.pipeline.locks import coordinator_lock


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs', type=Path, nargs='+')
    ap.add_argument('--model', default=None)
    args = ap.parse_args(argv)
    with coordinator_lock(Path('.batch-novelty.lock')):
        select(args.runs, args.model)
