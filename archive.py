"""Quality-diversity archive after Rainbow Teaming, per domain: domains/<d>/archive.json.

  uv run archive.py --domain electrical           # index existing tasks and print the grid
  uv run archive.py --domain electrical --reindex # re-label existing tasks

The grid is limitation x subdomain (Rainbow Teaming's two descriptor axes: here, the model weakness and the part of
the engineering domain). Each cell holds candidates and one elite: the draft with the highest fitness so far
(evaluate.py sets fitness). A new draft replaces the elite only by scoring higher, as in Rainbow Teaming. Further
descriptors (domain.json "descriptors": phenomenon, design freedom, reasoning) are sampled toward under-represented
values inside the chosen cell.

Entries: id, kind (existing | draft), status, title, summary, subdomain, descriptors, structure, failure_mechanisms,
concept, fitness, elite, provenance, notes. Existing tasks (seeds plus trials.json tasks) are labelled once by the
cheap review model and serve only the novelty check: the generator never sees them. Failed attempts (near-duplicates,
review failures) stay in the archive with their reasons, which the prompt adapter reads.
"""
import argparse
import json
from collections import Counter
from typing import List, Literal

from pydantic import BaseModel, Field, create_model

import llm
from domain import Kforge, Domain

INACTIVE = ('rejected-duplicate',)  # kept for memory, but they occupy no cell


class Structure(BaseModel):
    physical_principle: str
    governing_equations: str
    topology: str = Field(description='architecture fixed by the task, or the solution classes it admits')
    interfaces: str
    constraint_interactions: str = Field(description='which requirements pull against each other')
    operating_regime: str
    reasoning_required: str


class Novelty(BaseModel):
    novel: bool
    nearest: str = Field(description='id of the closest existing task, or empty')
    shared: List[str]
    differences: List[str]
    reason: str


LABEL_SYSTEM = """You index engineering design tasks ({discipline}) for a benchmark archive. Describe each task by
its engineering structure, not its wording, so that two tasks that need the same engineering decision look alike
even when their stories differ."""

LABEL_PROMPT = """Index this task. Pick the subdomain and each descriptor from its allowed list (the closest fit),
then describe the engineering structure.

<task id="{tid}">
{text}
</task>"""

NOVELTY_SYSTEM = """You check whether a proposed engineering design task is genuinely new compared with existing
tasks. Changes in wording, numerical constants or superficial context are NOT novelty. Compare at the
engineering-structural level: physical principle, governing equations, design topology, interfaces, constraint
interactions, operating regime and the reasoning required. The candidate is NOT novel when an existing task requires
essentially the same engineering decision, even if worded differently or set in another product."""

NOVELTY_PROMPT = """<candidate>
{candidate}
</candidate>

<existing>
{existing}
</existing>"""


def path(domain):
    return domain.dir / 'archive.json'


def load(domain):
    p = path(domain)
    return json.loads(p.read_text()) if p.exists() else {'domain': domain.name, 'entries': []}


def save(domain, arc):
    path(domain).write_text(json.dumps(arc, indent=1, ensure_ascii=False))


def by_id(arc):
    return {e['id']: e for e in arc['entries']}


def add(arc, entry):
    arc['entries'] = [e for e in arc['entries'] if e['id'] != entry['id']] + [entry]


def update(domain, entry_id, **fields):
    arc = load(domain)
    e = by_id(arc).get(entry_id)
    if e:
        e.update(fields)
        save(domain, arc)


def axes(domain):
    return json.loads(domain.axes_file.read_text())


def subdomains(domain):
    return list(axes(domain)['subdomain'])


def families(domain, subdomain):
    return axes(domain)['subdomain'][subdomain]


def vocab(domain):
    a = axes(domain)
    return {dim: a[key] for dim, key in domain['descriptors'].items()}


def existing_tasks(domain):
    """Seeds and evaluated tasks: id -> statement path."""
    tasks = {s['id']: Kforge / s['statement'] for s in json.loads(domain.seeds_file.read_text())['seeds']}
    for t in json.loads(domain.trials_file.read_text())['tasks']:
        tasks.setdefault(t['id'], Kforge / t['dir'] / 'task_files' / 'instruction.md')
    return tasks


def index_existing(domain, reindex=False):
    """Label existing tasks not yet in the archive; refresh failure mechanisms and pass rates from the catalogue."""
    arc = load(domain)
    have = by_id(arc)
    Descriptors = create_model('Descriptors', **{d: (Literal[tuple(v)], ...) for d, v in vocab(domain).items()})
    Label = create_model('Label', title=(str, ...), summary=(str, Field(description='one line: system and ask')),
                         subdomain=(Literal[tuple(subdomains(domain))], ...), descriptors=(Descriptors, ...),
                         structure=(Structure, ...))
    for tid, stmt in existing_tasks(domain).items():
        if tid in have and not reindex and have[tid].get('subdomain'):
            continue
        print(f'  indexing {tid}', flush=True)
        lab = llm.review_parse(LABEL_SYSTEM.format(discipline=domain['discipline']),
                               LABEL_PROMPT.format(tid=tid, text=stmt.read_text()), Label,
                               model=llm.INDEX_MODEL, purpose='index')
        add(arc, {'id': tid, 'kind': 'existing', 'status': 'existing', **lab.model_dump(), 'failure_mechanisms': [],
                  'provenance': {'statement': str(stmt.relative_to(Kforge))}, 'notes': []})
    if domain.limitations_file.exists():
        cat = json.loads(domain.limitations_file.read_text())
        mech = {}
        for l in cat['limitations']:
            for ev in l['evidence']:
                mech.setdefault(ev['task'], set()).add(l['id'])
        rewards = {}
        for t in cat['trials']:
            if t['triage'] == 'analysed':
                rewards.setdefault(t['task'], []).append(t['reward'])
        for e in arc['entries']:
            if e['kind'] == 'existing':
                e['failure_mechanisms'] = sorted(mech.get(e['id'], ()))
                if e['id'] in rewards:
                    r = rewards[e['id']]
                    e['pass_rate'] = {'passed': sum(x == 1.0 for x in r), 'trials': len(r), 'models': cat['models']}
    save(domain, arc)
    return arc


def active(arc):
    return [e for e in arc['entries'] if e['status'] not in INACTIVE]


def in_cell(e, lim_id, subdomain):
    return lim_id in e['failure_mechanisms'] and e.get('subdomain') == subdomain


def cell_drafts(arc, lim_id, subdomain):
    return [e for e in active(arc) if e['kind'] == 'draft' and in_cell(e, lim_id, subdomain)]


def elite(arc, lim_id, subdomain):
    elites = [e for e in cell_drafts(arc, lim_id, subdomain) if e.get('elite')]
    return elites[0] if elites else None


def offer_elite(domain, entry_id, fitness):
    """Rainbow Teaming replacement: the entry becomes its cell's elite if its fitness beats the incumbent's."""
    arc = load(domain)
    e = by_id(arc).get(entry_id)
    if not e:
        return False
    e['fitness'] = fitness
    lim_id, sub = e['failure_mechanisms'][0], e.get('subdomain')
    inc = elite(arc, lim_id, sub)
    won = fitness > 0 and (inc is None or fitness > (inc.get('fitness') or 0))
    if won:
        if inc:
            inc['elite'] = False
        e['elite'] = True
    save(domain, arc)
    return won


def counts(arc, subdomain):
    """Occupancy per descriptor value inside a subdomain, and per (limitation, descriptor value)."""
    single, pair = Counter(), Counter()
    for e in active(arc):
        if e.get('subdomain') != subdomain:
            continue
        for dim, val in e['descriptors'].items():
            single[(dim, val)] += 1
            for m in e['failure_mechanisms']:
                pair[(m, dim, val)] += 1
    return single, pair


def pick_descriptors(domain, arc, lim_id, subdomain, rng):
    """Per dimension, weight each value by 1 / (1 + occupancy + 2 * occupancy with this limitation), inside the
    subdomain, so drafts go to under-represented values."""
    single, pair = counts(arc, subdomain)
    pick = {}
    for dim, vals in vocab(domain).items():
        w = [1 / (1 + single[(dim, v)] + 2 * pair[(lim_id, dim, v)]) for v in vals]
        pick[dim] = rng.choices(vals, weights=w)[0]
    return pick


def neighbours(arc, subdomain, descriptors, lim_id, k=8, exclude=()):
    """Active entries closest to a target, for the novelty check: same subdomain, shared descriptor values, same
    failure mechanism."""
    def score(e):
        return (2 * (e.get('subdomain') == subdomain) + 2 * (lim_id in e['failure_mechanisms'])
                + sum(e['descriptors'].get(d) == v for d, v in descriptors.items()))
    pool = [e for e in active(arc) if e['id'] not in exclude]
    return sorted(pool, key=score, reverse=True)[:k]


def failed_attempts(arc, lim_id, subdomain, k=6):
    bad = [e for e in arc['entries'] if lim_id in e['failure_mechanisms'] and e.get('subdomain') == subdomain
           and e['status'] in ('rejected-duplicate', 'judge-fail')]
    return bad[-k:]


def structure_text(e):
    s = e['structure']
    return (f"[{e['id']}] {e['title']}: {e['summary']}\n  principle: {s['physical_principle']}\n"
            f"  equations: {s['governing_equations']}\n  topology: {s['topology']}\n  interfaces: {s['interfaces']}\n"
            f"  constraint interactions: {s['constraint_interactions']}\n  regime: {s['operating_regime']}\n"
            f"  reasoning: {s['reasoning_required']}")


def novelty(candidate, existing):
    if not existing:
        return {'novel': True, 'nearest': '', 'shared': [], 'differences': [], 'reason': 'archive is empty'}
    return llm.review_parse(NOVELTY_SYSTEM, NOVELTY_PROMPT.format(
        candidate=structure_text(candidate), existing='\n\n'.join(structure_text(e) for e in existing)),
        Novelty, model=llm.NOVELTY_MODEL, effort='medium', max_tokens=2500, repairs=1,
        transport_retries=0, request_timeout_s=180,
        purpose='novelty').model_dump()


def grid(domain, arc, lims=None):
    """Rainbow grid view: per limitation x subdomain, active drafts and the elite's fitness."""
    lims = lims or sorted({m for e in arc['entries'] for m in e['failure_mechanisms']})
    subs = subdomains(domain)
    lines = ['limitation \\ subdomain: ' + ' | '.join(f'S{i}' for i in range(len(subs)))]
    lines += [f'  S{i}: {s}' for i, s in enumerate(subs)]
    for l in lims:
        cells = []
        for s in subs:
            n, el = len(cell_drafts(arc, l, s)), elite(arc, l, s)
            cells.append(f"{n}" + (f"*{el['fitness']:.2f}" if el else ''))
        lines.append(f'{l[:44]:44s} ' + ' | '.join(f'{c:>8s}' for c in cells))
    lines.append('(drafts in cell, *elite fitness)')
    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', default='electrical')
    ap.add_argument('--reindex', action='store_true', help='re-label existing tasks')
    a = ap.parse_args()
    d = Domain(a.domain)
    arc = index_existing(d, reindex=a.reindex)
    lims = [l['id'] for l in d.limitations()['limitations']] if d.limitations_file.exists() else None
    print(grid(d, arc, lims))
