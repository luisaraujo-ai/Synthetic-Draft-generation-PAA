"""Judge saved unjudged drafts without generation; package those accepted."""
import json

from taskgen import llm
from taskgen.domain import Domain
from taskgen.generation.schema import Draft
from taskgen.pipeline.locks import coordinator_lock
from taskgen.pipeline.steps import concept_models
from taskgen.pipeline.storage import merge_cost, save, write_task
from taskgen.review import judge


def _recover(run):
    run = run.resolve()
    config = json.loads((run / 'run.json').read_text())
    domain = Domain(config['domain'])
    limitations = {x['id']: x for x in domain.limitations()['limitations']}
    ledger = domain.ledger()
    summary = json.loads((run / 'summary.json').read_text())
    try:
        for path in sorted((run / 'unjudged').glob('*.json')):
            index = int(path.name.split('-')[0])
            entry_path = run / 'entries' / f'{index:02d}.json'
            entry = json.loads(entry_path.read_text())
            if entry['status'] != 'unjudged':
                continue
            saved = json.loads(path.read_text())
            draft, meta = Draft.model_validate(saved['draft']), saved['meta']
            print(f'Judging {path.name}: {draft.title}', flush=True)
            try:
                review = judge.review(draft.model_dump(), limitations[meta['limitation']], concept_models(domain),
                                      reasoning_tokens=llm.settings.JUDGE_REASONING_TOKENS)
            except Exception as exc:
                save(run / 'rejudged' / path.name, {'status': 'unjudged', 'error': str(exc)})
                print(f'  still unjudged: {exc}', flush=True)
                continue
            ok, failed, hard = judge.passes(review, domain.get('astra_target', 0.5))
            p = min(max(review.difficulty.p_pass_per_attempt, 0.0), 1.0)
            save(run / 'rejudged' / path.name, {'status': 'accepted' if ok else 'rejected',
                                              'failed': failed, 'review': review.model_dump()})
            if ok:
                meta = {**meta, 'judge_model': llm.settings.REVIEW_MODEL}
                out = write_task(run, domain, draft, meta, review, p)
                entry.update(status='accepted', slug=draft.slug, p_pass=p)
                print(f'  accepted -> {out}', flush=True)
            else:
                entry.update(status='rejected', p_pass=p)
                print(f'  rejected: {failed}; p_pass={p:.2f}', flush=True)
            save(entry_path, entry)
            for i, old in enumerate(summary['candidates']):
                if i == index:
                    summary['candidates'][i] = entry
            for i, old in enumerate(ledger):
                if old.get('run') == run.name and old.get('title') == entry['title']:
                    ledger[i] = entry
            domain.save_ledger(ledger)
            save(run / 'summary.json', summary)
    finally:
        cost = merge_cost(run / 'cost.json', llm.cost_summary())
        save(run / 'cost.json', cost)
        summary['cost_usd'] = cost['total_usd']
        save(run / 'summary.json', summary)


def recover(run):
    config = json.loads((run.resolve() / 'run.json').read_text())
    if config.get('parallel'):
        raise SystemExit("Use parallel_run.py --resume to preserve this queue and its novelty decisions")
    with coordinator_lock(Domain(config['domain']).dir / '.parallel.lock'):
        return _recover(run)
