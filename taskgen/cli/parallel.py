"""Persistent parallel task generation: concept -> novelty -> draft -> split judge -> novelty -> package.

Workers make isolated API calls; the coordinator alone owns SQLite, entries, summaries and the domain ledger.
--n counts candidate slots, not accepted tasks. No task simulation or Harbor run is performed.
"""
import argparse
import json
import math
import os
import time
import uuid
from pathlib import Path

from taskgen import llm
from taskgen.domain import Domain
from taskgen.paths import RUNS
from taskgen.pipeline.parallel.coordinator import run_parallel

CONFIG_KEYS = ('domain', 'n', 'subdomain', 'seed', 'retries', 'transport_retries', 'generator_model', 'judge_model',
               'novelty_model')


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--domain', default='electrical')
    ap.add_argument('--n', type=int, default=50, help='candidate slots, not accepted tasks')
    ap.add_argument('--subdomain')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--retries', type=int, default=2, help='draft/concept revisions per slot')
    ap.add_argument('--transport-retries', type=int, default=1, help='additional stage attempts before preserving as unjudged')
    ap.add_argument('--generation-workers', type=int, default=3)
    ap.add_argument('--judge-workers', type=int, default=2)
    ap.add_argument('--generator-model', default=llm.settings.GENERATOR_MODEL)
    ap.add_argument('--judge-model', default=llm.settings.REVIEW_MODEL)
    ap.add_argument('--novelty-model', default=llm.settings.NOVELTY_MODEL)
    ap.add_argument('--budget-usd', type=float, default=None, help='cumulative local spending budget including this run’s prior costs')
    ap.add_argument('--skip-novelty', action='store_true', default=None, help='defer novelty checks to a later batch review')
    ap.add_argument('--resume', type=Path, help='resume a run created by this script')
    return ap, ap.parse_args(argv)


def check_credentials(ap, config):
    models = [config[k] for k in ('generator_model', 'judge_model', 'adapter_model')]
    if not config.get('skip_novelty'):
        models.append(config['novelty_model'])
    if any(m.startswith('claude-') for m in models) and not os.environ.get('ANTHROPIC_API_KEY'):
        ap.error('export ANTHROPIC_API_KEY for the configured Claude models')
    if any(not m.startswith('claude-') for m in models) and not (os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_API_KEY')):
        ap.error('export OPENROUTER_API_KEY (or OR_API_KEY) for the configured OpenRouter models')


def main(argv=None):
    ap, a = parse_args(argv)
    if a.budget_usd is not None and (not math.isfinite(a.budget_usd) or a.budget_usd <= 0):
        ap.error('--budget-usd must be finite and positive')
    if min(a.n, a.generation_workers, a.judge_workers) < 1 or min(a.retries, a.transport_retries) < 0:
        ap.error('counts must be positive; retry counts must be nonnegative')
    if a.resume:
        run = a.resume.resolve()
        if not (run / 'queue.sqlite').exists():
            ap.error('--resume requires a parallel_run.py run with queue.sqlite')
        config = json.loads((run / 'run.json').read_text())
    else:
        run = RUNS / f'{time.strftime("%Y%m%d-%H%M%S")}-parallel-{uuid.uuid4().hex[:6]}'
        config = {k: getattr(a, k) for k in CONFIG_KEYS}
        config.update(adapter_model=llm.settings.ADAPTER_MODEL, parallel=True)
    if a.budget_usd is not None:
        config['budget_usd'] = a.budget_usd
    if a.skip_novelty is not None:
        config['skip_novelty'] = a.skip_novelty
    domain = Domain(config['domain'])
    if config['subdomain'] and config['subdomain'] not in domain.subdomains():
        ap.error('unknown subdomain')
    check_credentials(ap, config)
    run_parallel(run, config, domain, a.generation_workers, a.judge_workers)
