"""Persistent parallel task generation: concept -> novelty -> draft -> split judge -> novelty -> package.

Workers make isolated API calls; the coordinator alone owns SQLite, entries, summaries and the domain ledger.
--n counts candidate slots, not accepted tasks. No task simulation or Harbor run is performed.
"""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import multiprocessing
import signal
import time
import uuid

from taskgen.generation.sampler import Sampler
from taskgen.pipeline.locks import coordinator_lock
from taskgen.pipeline.parallel.outputs import archive_catalog, catalog_for, write_outputs
from taskgen.pipeline.parallel.queue import PENDING, Queue
from taskgen.pipeline.parallel.transitions import finish
from taskgen.pipeline.parallel.worker import worker
from taskgen.pipeline.storage import atomic



def terminate(pool):
    # terminate_workers is public on Python >=3.14; support the project's Python 3.10+ too.
    if hasattr(pool, 'terminate_workers'):
        pool.terminate_workers()
    else:
        for process in list((getattr(pool, '_processes', None) or {}).values()):
            process.terminate()
        pool.shutdown(wait=False, cancel_futures=True)


def seed_queue(queue, run, domain, config):
    """Create one concept_pending job per slot that the queue does not hold yet, on reproducible cells."""
    existing = {ident: data for ident, _, data in queue.rows()}
    if len(existing) >= config['n']:
        return
    sampler = Sampler(domain, config['n'], config['seed'])
    for ident in range(config['n']):
        sub, lim, family, desc, profile = sampler.pick(ident, config['subdomain'])
        if ident in existing:
            pick = existing[ident]['pick']
        else:
            pick = dict(subdomain=sub, limitation=lim['id'], family=family, descriptors=desc, profile=profile)
            queue.put(ident, 'concept_pending', dict(pick=pick, attempt=1, retry=''))
            atomic(run / 'picks' / f'{ident:02d}.json', pick)
        sampler.count(pick['limitation'], pick['subdomain'], pick['descriptors'])


def defer_novelty(queue):
    for ident, state, data in queue.rows():
        if state == 'concept_novelty_pending':
            queue.put(ident, 'draft_pending', data)
        elif state == 'final_novelty_pending':
            queue.put(ident, 'accepted', data)


def dispatch(queue, pools, capacities, active, config, archive, run):
    """Submit pending jobs to their pools, novelty first (serialized), then judge, then generation."""
    occupied = {kind: sum(x[2] == kind for x in active.values()) for kind in pools}
    plan = ([('novelty', ['final_novelty', 'concept_novelty'])] if 'novelty' in pools else []) + \
        [('judge', ['judge']), ('generation', ['draft', 'concept'])]
    for kind, stages in plan:
        for stage in stages:
            for ident, state, data in queue.rows():
                if occupied[kind] >= capacities[kind]:
                    break
                if state != PENDING[stage]:
                    continue
                ticket = uuid.uuid4().hex
                if not queue.reserve(ticket, stage, config.get('budget_usd')):
                    continue
                data = queue.claim(ident, stage)
                if data is None:
                    queue.settle(ticket, {'ok': True, 'records': []})
                    continue
                catalog = [] if config.get('skip_novelty') else catalog_for(stage, ident, queue, archive, run)
                work = run / 'work' / f'{ident:02d}' / f'attempt-{data["attempt"]}' / stage
                future = pools[kind].submit(worker, stage, config, ident, data, catalog, str(work))
                active[future] = (ident, stage, kind, data, work, ticket)
                occupied[kind] += 1
                print(f'[{ident:02d}] {stage} (attempt {data["attempt"]})', flush=True)


def collect(queue, active, completed, config, domain, run):
    """Account each finished worker receipt exactly once and move its candidate to the next state."""
    for future in completed:
        ident, stage, kind, data, work, ticket = active.pop(future)
        try:
            receipt = future.result()
        except Exception as exc:
            receipt = dict(ok=False, error=f'Worker failure: {exc}', records=[], token=uuid.uuid4().hex)
        queue.account(receipt)
        queue.settle(ticket, receipt)
        atomic(run / 'history' / f'{ident:02d}' / f'{data["attempt"]}-{stage}-{receipt["token"]}.json', receipt)
        state = finish(stage, data, receipt, config, domain)
        queue.put(ident, state, data)
        print(f'[{ident:02d}] -> {state}' + (' ' + receipt.get('error', '') if not receipt['ok'] else ''), flush=True)
        write_outputs(queue, run, domain, config)


def run_parallel(run, config, domain, generation_workers, judge_workers):
    """Drive the queue until no job can be dispatched (done, or paused by the budget). Safe to resume."""
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    with coordinator_lock(domain.dir / '.parallel.lock'):
        run.mkdir(parents=True, exist_ok=True)
        atomic(run / 'run.json', config)
        queue = Queue(run / 'queue.sqlite')
        seed_queue(queue, run, domain, config)
        if config.get('budget_usd') is not None:
            queue.retain_interrupted()
        queue.recover()
        if config.get('skip_novelty'):
            defer_novelty(queue)
        archive = [] if config.get('skip_novelty') else archive_catalog(domain)
        ctx = multiprocessing.get_context('spawn')
        pools = {'generation': ProcessPoolExecutor(generation_workers, mp_context=ctx),
                 'judge': ProcessPoolExecutor(judge_workers, mp_context=ctx)}
        if not config.get('skip_novelty'):
            pools['novelty'] = ProcessPoolExecutor(1, mp_context=ctx)
        capacities = {'generation': generation_workers, 'judge': judge_workers, 'novelty': 1}
        active = {}
        print(f'Parallel run: {run}\n{config["n"]} slots; {generation_workers} generation, {judge_workers} judge; '
              f'novelty={not config.get("skip_novelty", False)}; budget=${config.get("budget_usd")}', flush=True)
        last = time.monotonic()
        try:
            write_outputs(queue, run, domain, config)
            while True:
                # Serialize novelty decisions and prioritize final admission. Refresh catalog AFTER each acceptance.
                dispatch(queue, pools, capacities, active, config, archive, run)
                if not active:
                    if any(state.endswith('_pending') for _, state, _ in queue.rows()):
                        print(f'Budget pause: {queue.budget_usage()}; pending work saved for resume.', flush=True)
                    break
                completed, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                collect(queue, active, completed, config, domain, run)
                if time.monotonic() - last >= 45:
                    counts = {}
                    for _, s, _ in queue.rows():
                        counts[s] = counts.get(s, 0) + 1
                    print(f'Status: {counts}; ${queue.cost()["total_usd"]:.4f}', flush=True)
                    last = time.monotonic()
        except KeyboardInterrupt:
            print('\nStopping workers; saved queue and completed judge stages can be resumed.', flush=True)
        finally:
            for pool in pools.values():
                terminate(pool)
            queue.retain_interrupted()
            write_outputs(queue, run, domain, config)
            queue.db.close()
        print(f'Saved: {run}', flush=True)
