"""Materialize queue state into the run folder: entries, accepted tasks, ledger, cost and budget files."""
import json

from taskgen.generation.schema import Draft
from taskgen.paths import ROOT, RUNS
from taskgen.pipeline.parallel.queue import TERMINAL
from taskgen.pipeline.storage import atomic, write_task
from taskgen.review import judge, novelty


def archive_catalog(domain):
    documents = {}
    for group in ('tasks', 'drafts'):
        for path in sorted(RUNS.glob(f'*/{group}/*/draft.json')):
            try:
                saved = json.loads(path.read_text())
                if saved.get('domain', domain.name) != domain.name:
                    continue
                d = saved.get('draft', saved)
                identifier = str(path.parent.relative_to(ROOT))
                documents[identifier] = novelty.document(identifier, draft=d, concept=saved.get('concept'),
                                                        subdomain=saved.get('subdomain',''), limitation=saved.get('limitation',''))
            except (ValueError, KeyError, OSError):
                continue
    return list(documents.values())


def meta_for(data, config):
    return {**data['pick'], 'concept':data['concept'], 'generator_model':config['generator_model'],
            'judge_model':config['judge_model'], 'novelty':({'status':'deferred'} if config.get('skip_novelty') else data.get('novelty', {}))}


def catalog_for(stage, ident, queue, archive, run):
    # This run is read from SQLite only, avoiding duplicates from its materialized task folders.
    docs = [x for x in archive if not x['id'].startswith(f'runs/{run.name}/')]
    for other, state, data in queue.rows():
        if other == ident:
            continue
        if state == 'accepted' or (stage == 'concept_novelty' and data.get('concept_reserved') and state not in TERMINAL):
            docs.append(novelty.document(f'{run.name}:{other}', draft=data.get('draft') if state=='accepted' else None,
                                        concept=data.get('concept'), subdomain=data['pick']['subdomain'],
                                        limitation=data['pick']['limitation']))
    return docs


def write_outputs(queue, run, domain, config):
    entries = []
    for ident, state, data in queue.rows():
        entry = dict(title=data.get('draft',{}).get('title') or data.get('concept',{}).get('device'),
                     subdomain=data['pick']['subdomain'], limitation=data['pick']['limitation'],
                     descriptors=data['pick']['descriptors'], status=state, run=run.name, index=ident)
        if state == 'accepted':
            d = Draft.model_validate(data['draft'])
            rv = judge.Review.model_validate(data['review'])
            p = min(max(rv.difficulty.p_pass_per_attempt,0),1)
            write_task(run, domain, d, meta_for(data,config), rv, p)
            atomic(run/'tasks'/d.slug/'novelty.json',meta_for(data,config)['novelty'])
            entry.update(slug=d.slug,p_pass=p)
        elif state == 'unjudged' and data.get('draft'):
            atomic(run/'unjudged'/f'{ident:02d}-attempt-{data["attempt"]}.json',
                   {'draft':data['draft'],'meta':meta_for(data,config),'resume_state':data.get('resume_state')})
        if state in TERMINAL:
            atomic(run/'entries'/f'{ident:02d}.json',entry)
        entries.append(entry)
    # Only this coordinator writes. Preserve unrelated runs in the current ledger.
    ledger = [x for x in domain.ledger() if x.get('run') != run.name]
    ledger += [x for x in entries if x['status'] in TERMINAL]
    atomic(domain.ledger_file,ledger)
    cost = queue.cost()
    atomic(run/'cost.json',cost)
    atomic(run/'budget.json',{'cap_usd':config.get('budget_usd'), **queue.budget_usage(),
                            'note':'Local scheduling budget; reserved includes in-flight and uncertain interrupted-call allowances. Provider billing can differ.'})
    atomic(run/'summary.json',{'candidates':entries,'cost_usd':cost['total_usd']})
