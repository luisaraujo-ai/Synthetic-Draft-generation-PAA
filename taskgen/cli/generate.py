"""Task + handbook generation: sample -> concept -> generate -> judge. Nothing else.

  uv run mine.py --domain electrical                       # once: limitations.json from evaluated trials
  uv run run.py --n 10                                     # sample 10 candidates
  uv run run.py --subdomain "power electronics" --n 10     # fix the subdomain
  uv run run.py --list                                     # the subdomains
  uv run run.py --skillcheck runs/<id>                     # optional: SKILL.md stage 1 screen of a run's tasks
  uv run run.py --resume runs/<id>                         # continue an interrupted run: finished candidates are kept

Per candidate: cell -> concept -> draft -> mechanical lint -> judge (guidelines + standalone + difficulty). A draft the
lint or judge sends back is regenerated with the reasons (--retries), also when it passes the guidelines but looks too
easy. An accepted task is written to runs/<id>/tasks/<slug>/ (instruction.md, handbook.md, inputs/, draft.json,
judge.json): that is what you take to the SKILL.md check. Everything sent back is kept in runs/<id>/rejected/.
A run keeps its settings in run.json, each sampled cell in picks/ and each finished candidate in entries/, so --resume
redoes only the candidates that did not finish (interrupted, or the judge gave no answer), on the same cells.
Needs ANTHROPIC_API_KEY (generator, if a claude-* model) and OPENROUTER_API_KEY or OR_API_KEY (concept, judge).
"""
import argparse
import json
import re
import time
from pathlib import Path

from taskgen import llm
from taskgen.domain import Domain
from taskgen.generation.sampler import Sampler
from taskgen.paths import RUNS, SKILL_FILE
from taskgen.pipeline.locks import acquire_writer_lock
from taskgen.pipeline.serial import candidate
from taskgen.pipeline.storage import merge_cost, save
from taskgen.review import stage1


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', default='electrical', help='folder under domains/')
    ap.add_argument('--subdomain', help='fix the subdomain (default: sampled per candidate)')
    ap.add_argument('--list', action='store_true', help='list the subdomains and exit')
    ap.add_argument('--n', type=int, default=10, help='candidates to sample')
    ap.add_argument('--generator-model', default=llm.settings.GENERATOR_MODEL,
                    help='draft writer: claude-* via Anthropic, or provider/model via OpenRouter')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--retries', type=int, default=2, help='regenerations after a lint or judge rejection')
    ap.add_argument('--skillcheck', type=Path, help='run SKILL.md stage 1 on the tasks of this run and exit')
    ap.add_argument('--resume', type=Path, help='continue this run (runs/<id>): redo only the unfinished candidates')
    ap.add_argument('--skill', type=Path, default=SKILL_FILE)
    return ap, ap.parse_args(argv)


def load_finished(run, resume):
    """Entries of every candidate that reached a verdict; unjudged ones are retried."""
    done = {}
    for f in sorted((run / 'entries').glob('*.json')):
        done[int(f.stem)] = json.loads(f.read_text())
    if resume and not done and (run / 'summary.json').exists():  # a run from before entries/: summary order = index
        for k, e in enumerate(json.loads((run / 'summary.json').read_text())['candidates']):
            done[k] = e
            save(run / 'entries' / f'{k:02d}.json', e)
    return {k: e for k, e in done.items() if e['status'] != 'unjudged'}  # the judge never answered: try again


def main(argv=None):
    ap, a = parse_args(argv)
    if a.resume:
        run = a.resume.resolve()
        if not run.is_dir():
            ap.error(f'no such run: {a.resume}')
        if (run / 'run.json').exists():  # the run's own settings win over the flags
            for k, v in json.loads((run / 'run.json').read_text()).items():
                setattr(a, k, v)
        if getattr(a, 'parallel', False):
            ap.error('Use parallel_run.py --resume for a parallel queue run')
    domain = Domain(a.domain)
    if a.list:
        print('\n'.join(domain.subdomains()))
        raise SystemExit
    if a.skillcheck:
        dirs = sorted(p for p in (a.skillcheck / 'tasks').iterdir() if (p / 'draft.json').exists())
        records = stage1.review_drafts(dirs, a.skill)
        stage1.write_summary(a.skillcheck, records)
        raise SystemExit
    if a.subdomain and a.subdomain not in domain.subdomains():
        ap.error(f'unknown subdomain "{a.subdomain}"; choose one of: {domain.subdomains()}')
    _writer_lock = acquire_writer_lock(domain.dir / '.parallel.lock')  # held until the process exits
    if not a.resume:
        slug = re.sub(r'[^a-z0-9]+', '-', (a.subdomain or 'run').lower()).strip('-')[:30]
        run = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    if not (run / 'run.json').exists():
        save(run / 'run.json', {k: getattr(a, k) for k in ('domain', 'subdomain', 'n', 'generator_model', 'seed', 'retries')})
    done = load_finished(run, a.resume)
    if a.resume:
        print(f'resuming {run.name}: {len(done)}/{a.n} candidates finished, {a.n - len(done)} to go', flush=True)
    sampler, ledger = Sampler(domain, a.n, a.seed), domain.ledger()
    entries = []
    try:
        for i in range(a.n):
            if i in done:
                entries.append(done[i])
                continue
            entry = candidate(domain, sampler, i, a, run, ledger, a.generator_model)
            save(run / 'entries' / f'{i:02d}.json', entry)
            ledger.append(entry)
            entries.append(entry)
            domain.save_ledger(ledger)
    finally:
        cost = merge_cost(run / 'cost.json', llm.cost_summary()) if a.resume else llm.cost_summary()
        save(run / 'cost.json', cost)
        save(run / 'summary.json', {'candidates': entries, 'cost_usd': cost['total_usd']})
        ok = [e for e in entries if e['status'] == 'accepted']
        print(f"done: {len(ok)} accepted, {sum(e['status'] == 'too_easy' for e in entries)} too easy, "
              f"{len(entries)} sampled; ${cost['total_usd']:.2f}; {run}")
