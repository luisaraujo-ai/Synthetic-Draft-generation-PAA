"""Once per domain: mine past evaluated trials (domains/<d>/trials.json) into limitations.json."""
import argparse

from taskgen.domain import Domain
from taskgen.mining.miner import mine


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--domain', default='electrical')
    ap.add_argument('--force', action='store_true', help='re-analyse trials that already have a cached analysis')
    a = ap.parse_args(argv)
    mine(Domain(a.domain), force=a.force)
