"""Judge saved unjudged drafts without generation; package those accepted."""
import argparse
from pathlib import Path

from taskgen.pipeline.rejudge import recover


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    recover(parser.parse_args(argv).run)
