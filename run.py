"""Run the pipeline: sample (adapter + generator) -> evaluator loop -> package.

  uv run mine.py --domain electrical                       # once: limitations.json from evaluated trials
  uv run archive.py --domain electrical                    # optional: index existing tasks, print the grid
  uv run run.py --full --n 10                             # complete pipeline
  uv run run.py --subdomain "power electronics" --n 10     # optionally fix the subdomain
  uv run run.py --run runs/<id> --step stage1              # idea screening only
Needs ANTHROPIC_API_KEY (generation, revision, kit), OPENROUTER_API_KEY or OR_API_KEY (adapter, review, novelty,
evaluator, assessment) and Docker with the domain image (executable evaluation; --static-only skips it).
Writes runs/<id>/cost.json with tokens and dollars per model and purpose.
"""
import argparse
import json
import re
import time
from pathlib import Path

import archive
import llm
from domain import HERE, Domain

STAGES = ('sample', 'stage1', 'static', 'evaluate', 'package')
FULL_STAGES = ('sample', 'stage1', 'evaluate', 'package')


def resolve_run_path(value):
    """Accept either a run path or a bare run id located under the workspace's runs/ directory."""
    if value is None:
        return None
    value = Path(value)
    if len(value.parts) == 1 and not (value / 'drafts').is_dir():
        candidate = HERE / 'runs' / value.name
        if (candidate / 'drafts').is_dir():
            return candidate
    return value


def selected_steps(step=None, full=False, start=None):
    if sum((bool(step), bool(full), bool(start))) > 1:
        raise ValueError('Choose one of --step, --full, or --from')
    if full:
        return FULL_STAGES
    if start:
        start = 'evaluate' if start == 'review' else start
        return STAGES[STAGES.index(start):]
    return tuple(step or ['sample'])


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', default='electrical', help='folder under domains/')
    ap.add_argument('--subdomain', help='fix the subdomain (default: randomly sample one per draft)')
    ap.add_argument('--list', action='store_true', help='list the subdomains and exit')
    ap.add_argument('--n', type=int, default=10, help='drafts to sample')
    ap.add_argument('--generator-model', default=llm.GENERATOR_MODEL,
                    help='draft writer: claude-* via Anthropic, or provider/model via OpenRouter; defaults to TASKGEN_GENERATOR_MODEL or Opus')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--retries', type=int, default=3,
                    help='regenerations when a draft breaks the generation rules or is a near-duplicate')
    ap.add_argument('--rounds', type=int, default=3, help='evaluator rounds after the first (each may revise)')
    ap.add_argument('--probe', action='store_true', help='also run a blind evaluator attempt per evaluated round')
    ap.add_argument('--static-only', action='store_true', help='static review only (no Docker build and grading)')
    ap.add_argument('--draft', help='review only this folder name under the run drafts directory')
    ap.add_argument('--run', type=Path, help='existing run directory (with --from)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--step', choices=STAGES, nargs='+', help='run only these steps, in the order given (default: sample)')
    mode.add_argument('--full', action='store_true', help='sample, stage1, evaluate/revise, package')
    mode.add_argument('--from', dest='start', choices=STAGES + ('review',), help='run this step and later steps; review aliases evaluate')
    ap.add_argument('--skill', type=Path, default=HERE / 'SKILL.md')
    ap.add_argument('--package-failed', action='store_true', help='also package drafts that are not ready')
    a = ap.parse_args()
    a.run = resolve_run_path(a.run)
    domain = Domain(a.domain)
    if a.list:
        print('\n'.join(archive.subdomains(domain)))
        raise SystemExit
    todo = selected_steps(a.step, a.full, a.start)
    if a.static_only:
        if a.full or a.step:
            ap.error('--static-only is a legacy option; use --step static')
        todo = ('static',)
    if a.draft and ('sample' in todo or a.run is None):
        ap.error('--draft requires --run and steps without sample')
    if a.draft and (Path(a.draft).name != a.draft or a.draft in ('.', '..')):
        ap.error('--draft must be a folder name, not a path')
    if 'sample' not in todo and a.run is None:
        ap.error('--run is required when not sampling')
    if 'sample' in todo and todo[0] != 'sample':
        ap.error('sample must be the first step')
    if len(todo) != len(set(todo)):
        ap.error('steps must not be repeated')
    if any(t in todo for t in ('stage1', 'static', 'evaluate')) and not a.skill.is_file():
        ap.error(f'skill file not found: {a.skill}')
    slug = re.sub(r'[^a-z0-9]+', '-', (a.subdomain or 'run').lower()).strip('-')[:30]
    run = a.run or HERE / 'runs' / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    drafts_dir, out_dir = run / 'drafts', run / 'packages'
    invocation_started = time.monotonic()
    invocation_started_at = time.strftime('%Y-%m-%d %H:%M:%S')
    step_metrics, current_step, current_step_started, current_ledger_start = [], None, None, None
    dirs, ready, packaged_count, completed = [], [], 0, False
    try:
        for step in todo:
            current_step = step
            current_step_started = time.monotonic()
            current_ledger_start = len(llm.LEDGER)
            if step == 'sample':
                import sample
                print(f'sampling {a.n} {domain.name} drafts into {drafts_dir}')
                sample.sample(domain, a.subdomain, a.n, drafts_dir, seed=a.seed, retries=a.retries, generator_model=a.generator_model)
            dirs = sorted(p for p in drafts_dir.iterdir() if (p / 'draft.json').exists()) if drafts_dir.is_dir() else []
            if a.draft:
                selected = drafts_dir / a.draft
                required = 'instruction.md' if step == 'stage1' and not (selected / 'draft.json').is_file() else 'draft.json'
                if not (selected / required).is_file():
                    raise SystemExit(f'{step} requires {selected / required}')
                dirs = [selected]
            if not dirs:
                raise SystemExit(f'No drafts found in {drafts_dir}')
            if step == 'stage1':
                import idea_review
                records = idea_review.review_drafts(dirs, a.skill)
                if not a.draft:
                    idea_review.write_summary(run, records)
            elif step == 'static':
                import refine
                refine.refine_drafts(dirs, a.skill, rounds=0, static_only=True)
            elif step == 'evaluate':
                import refine
                print(f'evaluating with {llm.EVALUATOR_MODEL}; up to {a.rounds} revisions')
                refine.refine_drafts(dirs, a.skill, a.rounds, a.probe)
            elif step == 'package':
                import package
                out_dir.mkdir(parents=True, exist_ok=True)
                for d in dirs:
                    if a.package_failed or json.loads((d / 'draft.json').read_text()).get('ready'):
                        package.package(d, out_dir)
                        packaged_count += 1
            step_cost = llm.cost_summary(llm.LEDGER[current_ledger_start:])
            step_metrics.append({'step': step, 'elapsed_seconds': round(time.monotonic() - current_step_started, 3),
                                 **step_cost})
            current_step = current_step_started = current_ledger_start = None
        ready = [d for d in dirs if (d / 'draft.json').exists() and json.loads((d / 'draft.json').read_text()).get('ready')]
        completed = True
        print(f'done: {len(dirs)} drafted, {len(ready)} ready; run directory {run}')
        if domain.limitations_file.exists():
            lims = [l['id'] for l in domain.limitations()['limitations']]
            print(archive.grid(domain, archive.load(domain), lims))
    finally:
        cost = llm.cost_summary()
        run.mkdir(parents=True, exist_ok=True)
        if current_step is not None:
            step_cost = llm.cost_summary(llm.LEDGER[current_ledger_start:])
            step_metrics.append({'step': current_step,
                                 'elapsed_seconds': round(time.monotonic() - current_step_started, 3),
                                 'status': 'failed_or_interrupted', **step_cost})
        elapsed = round(time.monotonic() - invocation_started, 3)
        measured_dirs = dirs or (sorted(p for p in drafts_dir.iterdir() if (p / 'draft.json').exists())
                                 if drafts_dir.is_dir() else [])
        draft_count = len(measured_dirs)
        ready_count = sum(bool(json.loads((d / 'draft.json').read_text()).get('ready')) for d in measured_dirs)
        pipeline_record = {
            'stages': list(todo), 'full_pipeline': tuple(todo) == FULL_STAGES,
            'status': 'complete' if completed else 'failed_or_interrupted',
            'started_at': invocation_started_at, 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'elapsed_seconds': elapsed, 'step_metrics': step_metrics,
            'generated_drafts': draft_count, 'ready_tasks': ready_count, 'packaged_tasks': packaged_count,
            **cost,
            'usd_per_generated_draft': round(cost['total_usd'] / draft_count, 3) if draft_count else None,
            'usd_per_ready_task': round(cost['total_usd'] / ready_count, 3) if ready_count else None,
            'seconds_per_ready_task': round(elapsed / ready_count, 3) if ready_count else None,
        }
        metrics_path = run / 'pipeline_metrics.json'
        metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {'invocations': []}
        metrics['invocations'].append(pipeline_record)
        metrics_path.write_text(json.dumps(metrics, indent=1))
        prev = json.loads((run / 'cost.json').read_text()) if (run / 'cost.json').exists() else {'invocations': []}
        prev['invocations'].append({'stages': list(todo), 'at': time.strftime('%Y-%m-%d %H:%M:%S'), **cost})
        prev['total_usd'] = round(sum(i['total_usd'] for i in prev['invocations']), 2)
        (run / 'cost.json').write_text(json.dumps(prev, indent=1))
        print(f"cost this invocation: ${cost['total_usd']:.2f}"
              + (f" ({cost['unpriced_calls']} calls without a price)" if cost['unpriced_calls'] else '')
              + f"; run total ${prev['total_usd']:.2f} (runs/.../cost.json)")
