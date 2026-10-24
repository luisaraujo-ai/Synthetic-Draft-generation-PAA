"""Persistent parallel task generation: concept -> novelty -> draft -> split judge -> novelty -> package.

Workers make isolated API calls; the coordinator alone owns SQLite, entries, summaries and the domain ledger.
--n counts candidate slots, not accepted tasks. No task simulation or Harbor run is performed.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import multiprocessing
import math
import os
from pathlib import Path
import signal
import sqlite3
import time
import traceback
import uuid

import adapter
from domain import Domain, HERE, guidelines
import judge
import lint
import llm
import novelty
import run as serial
import sample
from pipeline_lock import coordinator_lock

PENDING = {'concept': 'concept_pending', 'draft': 'draft_pending', 'judge': 'judge_pending',
           'concept_novelty': 'concept_novelty_pending', 'final_novelty': 'final_novelty_pending'}
TERMINAL = {'accepted', 'rejected', 'unjudged', 'failed', 'duplicate'}
# Conservative scheduling allowances, not provider-enforced billing limits.
RESERVE_USD = {'concept':1.0, 'draft':4.0, 'judge':1.0, 'concept_novelty':1.0, 'final_novelty':1.0}


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Queue:
    """Durable job state and exactly-once accounting for returned worker receipts."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.had_budget_table = bool(self.db.execute("SELECT name FROM sqlite_master WHERE name='budget_events'").fetchone())
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS receipts (token TEXT PRIMARY KEY, records TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS budget_events (token TEXT PRIMARY KEY, amount REAL NOT NULL, status TEXT NOT NULL);
        ''')

    def put(self, ident, state, payload):
        with self.db:
            self.db.execute('INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload',
                            (ident, state, json.dumps(payload)))

    def rows(self):
        return [(i, s, json.loads(p)) for i, s, p in self.db.execute('SELECT * FROM jobs ORDER BY id')]

    def claim(self, ident, stage):
        with self.db:
            cursor = self.db.execute('UPDATE jobs SET state=? WHERE id=? AND state=?',
                                     (stage + '_running', ident, PENDING[stage]))
            if cursor.rowcount != 1:
                return None
            payload = self.db.execute('SELECT payload FROM jobs WHERE id=?', (ident,)).fetchone()[0]
        return json.loads(payload)

    def account(self, receipt):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)',
                            (receipt['token'], json.dumps(receipt.get('records', []))))

    def cost(self):
        records = [r for (raw,) in self.db.execute('SELECT records FROM receipts') for r in json.loads(raw)]
        # A resumed judge stage carries its original usage IDs, so cached usage is never charged twice.
        unique = {}
        for i, record in enumerate(records):
            unique[record.get('call_id', f'legacy-{i}')] = record
        return llm.cost_summary(list(unique.values()))

    def budget_usage(self):
        reserved = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM budget_events WHERE status IN ('held','uncertain')").fetchone()[0]
        return {'recorded_usd':self.cost()['total_usd'], 'reserved_usd':round(reserved,4),
                'committed_usd':round(self.cost()['total_usd']+reserved,4)}

    def reserve(self, ticket, stage, cap):
        amount = RESERVE_USD[stage]
        if cap is not None and self.budget_usage()['committed_usd']+amount>cap:
            return False
        with self.db:
            self.db.execute('INSERT INTO budget_events VALUES (?,?,?)', (ticket,amount,'held'))
        return True

    def settle(self, ticket, receipt):
        # Failed/unknown calls may still be billed; retain their allowance instead of refunding it.
        status = 'settled' if receipt['ok'] and all(r.get('usd') is not None for r in receipt.get('records',[])) else 'uncertain'
        with self.db:
            self.db.execute('UPDATE budget_events SET status=? WHERE token=?',(status,ticket))

    def retain_interrupted(self):
        with self.db:
            self.db.execute("UPDATE budget_events SET status='uncertain' WHERE status='held'")
        if not self.had_budget_table:
            # Existing runs predate reservations. Conservatively account for calls interrupted during migration.
            with self.db:
                for ident,state,data in self.rows():
                    if state.endswith('_running'):
                        stage=state[:-8]
                        self.db.execute('INSERT OR IGNORE INTO budget_events VALUES (?,?,?)',
                            (f'legacy:{ident}:{data["attempt"]}:{stage}',RESERVE_USD[stage],'uncertain'))

    def recover(self):
        for ident, state, data in self.rows():
            if state.endswith('_running'):
                self.put(ident, PENDING[state[:-8]], data)
            elif state == 'unjudged':
                target = data.get('resume_state', 'judge_pending')
                data.pop(target.removesuffix('_pending') + '_errors', None)
                self.put(ident, target, data)


def configure(config):
    for key, attr in [('generator_model', 'GENERATOR_MODEL'), ('judge_model', 'REVIEW_MODEL'),
                      ('adapter_model', 'ADAPTER_MODEL'), ('novelty_model', 'NOVELTY_MODEL')]:
        setattr(llm, attr, config[key])


def worker(stage, config, ident, data, catalog, workdir):
    """Only worker-specific artifacts are written here. Successful receipts survive a coordinator crash."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure(config)
    llm.LEDGER.clear()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # The stage/attempt directory fixes identity. Novelty includes the latest catalog in its key.
    stable_data = {k:v for k,v in data.items() if not k.endswith('_errors') and k not in ('error','resume_state')}
    stable_config = {k:v for k,v in config.items() if k not in ('budget_usd','skip_novelty')}
    fingerprint = digest({'stage': stage, 'config': stable_config, 'data': stable_data,
                          'guidelines':guidelines(),
                          'effort':llm.JUDGE_EFFORT if stage=='judge' else llm.GENERATOR_EFFORT,
                          'reasoning_tokens':llm.JUDGE_REASONING_TOKENS,
                          'catalog': catalog if stage in ('concept_novelty','final_novelty') else []})
    receipt_path = workdir / 'receipt.json'
    if receipt_path.exists():
        cached = json.loads(receipt_path.read_text())
        if cached.get('fingerprint') == fingerprint and cached.get('ok'):
            return cached
    token = uuid.uuid4().hex
    started = time.monotonic()
    with (workdir / 'worker.log').open('a', buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        try:
            domain = Domain(config['domain'])
            limitation = next(x for x in domain.limitations()['limitations'] if x['id'] == data['pick']['limitation'])
            pick = data['pick']
            cell = adapter.cell_text(sample.abstract(limitation), pick['subdomain'], pick['family'], pick['descriptors'], pick['profile'])
            if stage == 'concept':
                avoid = [x['title'] for x in novelty.shortlist({'subdomain':pick['subdomain'], 'cell':cell}, catalog)]
                concept = adapter.adapt(domain, cell + '\n' + data.get('retry', ''), avoid)
                result = {'concept': concept.model_dump()}
            elif stage == 'draft':
                concept = adapter.Concept.model_validate(data['concept'])
                previous = [x['title'] for x in novelty.shortlist({'subdomain':pick['subdomain'], 'concept':data['concept']}, catalog)]
                system, prompt = sample.system_prompt(domain), sample.user_prompt(domain, cell, concept, data.get('retry', ''), previous)
                atomic(workdir / 'request.json', llm.generation_request_record(system, prompt, sample.Draft, config['generator_model']))
                generated = workdir / 'generated.json'
                if generated.exists():
                    saved = json.loads(generated.read_text())
                else:
                    saved = None
                if saved and saved.get('fingerprint') == fingerprint:
                    d = sample.Draft.model_validate(saved['draft'])
                    llm.LEDGER.extend(saved.get('records', []))
                else:
                    d = llm.generate_parse(system, prompt, sample.Draft, model=config['generator_model'])
                    atomic(generated, {'fingerprint':fingerprint, 'draft':d.model_dump(), 'records':list(llm.LEDGER)})
                d.slug = f'{ident:02d}-{d.slug}'[:60]
                d.instruction_md, d.handbook_md = lint.strip_canary(d.instruction_md), lint.strip_canary(d.handbook_md)
                atomic(workdir / 'draft.json', d.model_dump())
                problems = serial.polish_style(domain, d, config['generator_model'])
                atomic(workdir / 'draft.json', d.model_dump())
                result = {'draft': d.model_dump(), 'problems': problems}
            elif stage == 'judge':
                d = sample.Draft.model_validate(data['draft'])
                rv = judge.review(d.model_dump(), limitation, serial.concept_models(domain),
                                  reasoning_tokens=llm.JUDGE_REASONING_TOKENS, checkpoint_dir=workdir / 'stages')
                result = {'review': rv.model_dump()}
            else:
                candidate = novelty.document(f'candidate:{ident}', draft=data.get('draft') if stage=='final_novelty' else None,
                                             concept=data['concept'], subdomain=pick['subdomain'], limitation=pick['limitation'])
                result = {'novelty': novelty.review(candidate, catalog, model=config['novelty_model']).model_dump()}
            receipt = dict(ok=True, result=result)
        except Exception as exc:
            traceback.print_exc()
            receipt = dict(ok=False, error=f'{type(exc).__name__}: {exc}')
    receipt.update(token=token, fingerprint=fingerprint, records=list(llm.LEDGER), seconds=round(time.monotonic()-started,2))
    atomic(receipt_path, receipt)
    return receipt


def archive_catalog(domain):
    documents = {}
    for group in ('tasks', 'drafts'):
        for path in sorted((HERE/'runs').glob(f'*/{group}/*/draft.json')):
            try:
                saved = json.loads(path.read_text())
                if saved.get('domain', domain.name) != domain.name:
                    continue
                d = saved.get('draft', saved)
                identifier = str(path.parent.relative_to(HERE))
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


def next_attempt(data, config, reason, new_concept=False):
    data['retry'] = reason
    for stage in ('judge', 'concept_novelty', 'final_novelty'):
        data.pop(stage + '_errors', None)
    data['attempt'] += 1
    if data['attempt'] > config['retries'] + 1:
        return 'duplicate' if new_concept else 'rejected'
    if new_concept:
        data['concept_reserved'] = False
        data.pop('concept', None)
    data.pop('draft', None)
    data.pop('review', None)
    return 'concept_pending' if new_concept else 'draft_pending'


def finish(stage, data, receipt, config, domain):
    """Pure transition policy. API faults never ask the generator to replace a judged draft."""
    if not receipt['ok']:
        data['error'] = receipt['error']
        if stage in ('judge', 'concept_novelty', 'final_novelty'):
            key = stage + '_errors'
            data[key] = data.get(key, 0) + 1
            data['resume_state'] = PENDING[stage]
            if data[key] <= config['transport_retries']:
                return PENDING[stage]
            return 'unjudged'
        data['attempt'] += 1
        data['retry'] = 'Previous API call failed to finish. Produce a complete compact response.\n' + data.get('retry','')
        return PENDING[stage] if data['attempt'] <= config['retries']+1 else 'failed'
    data.pop('error', None)
    result = receipt['result']
    if stage == 'concept':
        data['concept'] = result['concept']
        return 'draft_pending' if config.get('skip_novelty') else 'concept_novelty_pending'
    if stage == 'draft':
        data['draft'] = result['draft']
        if result['problems']:
            d = sample.Draft.model_validate(data['draft'])
            return next_attempt(data, config, serial.retry_note(d, '\n'.join(result['problems'])))
        return 'judge_pending'
    if stage == 'judge':
        data['review'] = result['review']
        rv = judge.Review.model_validate(data['review'])
        ok, failed, hard = judge.passes(rv, domain.get('astra_target', .5))
        if ok:
            return 'accepted' if config.get('skip_novelty') else 'final_novelty_pending'
        d = sample.Draft.model_validate(data['draft'])
        reason = serial.retry_note(d, judge.feedback(rv, failed, hard))
        reason += '\nPrevious complete draft (edit the same engineering where sound):\n' + json.dumps(data['draft'])
        return next_attempt(data, config, reason)
    nr = result['novelty']
    data.setdefault('novelty', {})[stage] = nr
    if nr['verdict'] == 'distinct':
        if stage == 'concept_novelty':
            data['concept_reserved'] = True
            return 'draft_pending'
        return 'accepted'
    if nr['verdict'] == 'unclear':
        data['resume_state'] = PENDING[stage]
        return 'unjudged'
    return next_attempt(data, config, 'Engineering duplicate: '+nr['evidence']+'\nRequired difference: '+nr['change_needed'], new_concept=True)


def publish(queue, run, domain, config):
    entries = []
    for ident, state, data in queue.rows():
        entry = dict(title=data.get('draft',{}).get('title') or data.get('concept',{}).get('device'),
                     subdomain=data['pick']['subdomain'], limitation=data['pick']['limitation'],
                     descriptors=data['pick']['descriptors'], status=state, run=run.name, index=ident)
        if state == 'accepted':
            d = sample.Draft.model_validate(data['draft'])
            rv = judge.Review.model_validate(data['review'])
            p = min(max(rv.difficulty.p_pass_per_attempt,0),1)
            serial.write_task(run, domain, d, meta_for(data,config), rv, p)
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


def terminate(pool):
    # terminate_workers is public on Python >=3.14; support the project's Python 3.10+ too.
    if hasattr(pool,'terminate_workers'):
        pool.terminate_workers()
    else:
        for process in list((getattr(pool,'_processes',None) or {}).values()):
            process.terminate()
        pool.shutdown(wait=False,cancel_futures=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--domain',default='electrical')
    ap.add_argument('--n',type=int,default=50,help='candidate slots, not accepted tasks')
    ap.add_argument('--subdomain')
    ap.add_argument('--seed',type=int,default=0)
    ap.add_argument('--retries',type=int,default=2,help='draft/concept revisions per slot')
    ap.add_argument('--transport-retries',type=int,default=1,help='additional stage attempts before preserving as unjudged')
    ap.add_argument('--generation-workers',type=int,default=3)
    ap.add_argument('--judge-workers',type=int,default=2)
    ap.add_argument('--generator-model',default=llm.GENERATOR_MODEL)
    ap.add_argument('--judge-model',default=llm.REVIEW_MODEL)
    ap.add_argument('--novelty-model',default=llm.NOVELTY_MODEL)
    ap.add_argument('--budget-usd',type=float,default=None,help='cumulative local spending budget including this run’s prior costs')
    ap.add_argument('--skip-novelty',action='store_true',default=None,help='defer novelty checks to a later batch review')
    ap.add_argument('--resume',type=Path,help='resume a run created by this script')
    a=ap.parse_args(argv)
    if a.budget_usd is not None and (not math.isfinite(a.budget_usd) or a.budget_usd<=0):
        ap.error('--budget-usd must be finite and positive')
    if min(a.n,a.generation_workers,a.judge_workers)<1 or min(a.retries,a.transport_retries)<0:
        ap.error('counts must be positive; retry counts must be nonnegative')
    if a.resume:
        run=a.resume.resolve()
        if not (run/'queue.sqlite').exists():ap.error('--resume requires a parallel_run.py run with queue.sqlite')
        config=json.loads((run/'run.json').read_text())
    else:
        run=HERE/'runs'/f'{time.strftime("%Y%m%d-%H%M%S")}-parallel-{uuid.uuid4().hex[:6]}'
        config={k:getattr(a,k) for k in ('domain','n','subdomain','seed','retries','transport_retries','generator_model','judge_model','novelty_model')}
        config.update(adapter_model=llm.ADAPTER_MODEL,parallel=True)
    if a.budget_usd is not None:config['budget_usd']=a.budget_usd
    if a.skip_novelty is not None:config['skip_novelty']=a.skip_novelty
    domain=Domain(config['domain'])
    if config['subdomain'] and config['subdomain'] not in domain.subdomains():ap.error('unknown subdomain')
    models = [config[k] for k in ('generator_model','judge_model','adapter_model')]
    if not config.get('skip_novelty'):models.append(config['novelty_model'])
    if any(m.startswith('claude-') for m in models) and not os.environ.get('ANTHROPIC_API_KEY'):
        ap.error('export ANTHROPIC_API_KEY for the configured Claude models')
    if any(not m.startswith('claude-') for m in models) and not (os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_API_KEY')):
        ap.error('export OPENROUTER_API_KEY (or OR_API_KEY) for the configured OpenRouter models')
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    with coordinator_lock(domain.dir/'.parallel.lock'):
        run.mkdir(parents=True,exist_ok=True)
        atomic(run/'run.json',config)
        queue=Queue(run/'queue.sqlite')
        existing = {ident:data for ident,_,data in queue.rows()}
        if len(existing) < config['n']:
            sampler=sample.Sampler(domain,config['n'],config['seed'])
            for ident in range(config['n']):
                sub,lim,family,desc,profile=sampler.pick(ident,config['subdomain'])
                if ident in existing:
                    pick=existing[ident]['pick']
                else:
                    pick=dict(subdomain=sub,limitation=lim['id'],family=family,descriptors=desc,profile=profile)
                    queue.put(ident,'concept_pending',dict(pick=pick,attempt=1,retry=''))
                    atomic(run/'picks'/f'{ident:02d}.json',pick)
                sampler.count(pick['limitation'],pick['subdomain'],pick['descriptors'])
        if config.get('budget_usd') is not None:queue.retain_interrupted()
        queue.recover()
        if config.get('skip_novelty'):
            for ident,state,data in queue.rows():
                if state=='concept_novelty_pending':queue.put(ident,'draft_pending',data)
                elif state=='final_novelty_pending':queue.put(ident,'accepted',data)
        archive=[] if config.get('skip_novelty') else archive_catalog(domain)
        ctx=multiprocessing.get_context('spawn')
        pools={'generation':ProcessPoolExecutor(a.generation_workers,mp_context=ctx),
               'judge':ProcessPoolExecutor(a.judge_workers,mp_context=ctx)}
        if not config.get('skip_novelty'):pools['novelty']=ProcessPoolExecutor(1,mp_context=ctx)
        capacities={'generation':a.generation_workers,'judge':a.judge_workers,'novelty':1}
        active={}
        print(f'Parallel run: {run}\n{config["n"]} slots; {a.generation_workers} generation, {a.judge_workers} judge; novelty={not config.get("skip_novelty",False)}; budget=${config.get("budget_usd")}',flush=True)
        last=time.monotonic()
        try:
            publish(queue,run,domain,config)
            while True:
                # Serialize novelty decisions and prioritize final admission. Refresh catalog AFTER each acceptance.
                occupied={kind:sum(x[2]==kind for x in active.values()) for kind in pools}
                for kind,stages in ([('novelty',['final_novelty','concept_novelty'])] if 'novelty' in pools else []) + [('judge',['judge']),('generation',['draft','concept'])]:
                    for stage in stages:
                        for ident,state,data in queue.rows():
                            if occupied[kind]>=capacities[kind]:break
                            if state!=PENDING[stage]:continue
                            cap=config.get('budget_usd')
                            ticket=uuid.uuid4().hex
                            if not queue.reserve(ticket,stage,cap):continue
                            data=queue.claim(ident,stage)
                            if data is None:
                                queue.settle(ticket,{'ok':True,'records':[]})
                                continue
                            catalog=[] if config.get('skip_novelty') else catalog_for(stage,ident,queue,archive,run)
                            work=run/'work'/f'{ident:02d}'/f'attempt-{data["attempt"]}'/stage
                            future=pools[kind].submit(worker,stage,config,ident,data,catalog,str(work))
                            active[future]=(ident,stage,kind,data,work,ticket)
                            occupied[kind]+=1
                            print(f'[{ident:02d}] {stage} (attempt {data["attempt"]})',flush=True)
                if not active:
                    if any(state.endswith('_pending') for _,state,_ in queue.rows()):
                        print(f'Budget pause: {queue.budget_usage()}; pending work saved for resume.',flush=True)
                    break
                completed,_=wait(active,timeout=1,return_when=FIRST_COMPLETED)
                for future in completed:
                    ident,stage,kind,data,work,ticket=active.pop(future)
                    try:receipt=future.result()
                    except Exception as exc:receipt=dict(ok=False,error=f'Worker failure: {exc}',records=[],token=uuid.uuid4().hex)
                    queue.account(receipt)
                    queue.settle(ticket,receipt)
                    atomic(run/'history'/f'{ident:02d}'/f'{data["attempt"]}-{stage}-{receipt["token"]}.json',receipt)
                    state=finish(stage,data,receipt,config,domain)
                    queue.put(ident,state,data)
                    print(f'[{ident:02d}] -> {state}'+(' '+receipt.get('error','') if not receipt['ok'] else ''),flush=True)
                    publish(queue,run,domain,config)
                if time.monotonic()-last>=45:
                    counts={}
                    for _,s,_ in queue.rows():counts[s]=counts.get(s,0)+1
                    print(f'Status: {counts}; ${queue.cost()["total_usd"]:.4f}',flush=True)
                    last=time.monotonic()
        except KeyboardInterrupt:
            print('\nStopping workers; saved queue and completed judge stages can be resumed.',flush=True)
        finally:
            for pool in pools.values():terminate(pool)
            queue.retain_interrupted()
            publish(queue,run,domain,config)
            queue.db.close()
        print(f'Saved: {run}',flush=True)

if __name__=='__main__':main()
