"""Sampling and generation: pick a cell, write a concept, write the draft.

1. Cell: a limitation from the mined catalogue x a subdomain, weighted by confidence and by how few accepted tasks the
   cell already holds (ledger). Inside the cell the circuit family rotates, the descriptors (phenomenon, design
   freedom, reasoning) go to under-represented values, and the secondary axes are stratified per run.
2. Concept (adapter.py, cheap model).
3. Draft (generator): writes the complete task from the concept, the cell and the guidelines. It never sees existing
   tasks and has no execution backend to lean on: every input is stated in the brief or written out in full.
The judge (judge.py) and the loop (run.py) decide what happens to the draft.
"""
import random
from collections import Counter
from typing import List, Literal

from pydantic import BaseModel, Field

import llm
from domain import guidelines

CONF_WEIGHT = {'high': 3.0, 'medium': 2.0, 'low': 1.0}


class Requirement(BaseModel):
    rid: str = Field(description='R1, R2, ...')
    metric: str = Field(description='the measured quantity, e.g. "output ripple, peak to peak"')
    condition: str = Field(description='state, load, corner or configuration it must hold under')
    location: str = Field(description='where it is measured or extracted')
    limit: str = Field(description='minimum, maximum or band, with units, e.g. "<= 8 mV"')


class OutputFile(BaseModel):
    path: str = Field(description='exact path of the delivered file, under the artifact root')
    format: str
    contents: str = Field(description='fields, units and structure the checker reads; published in the brief')


class CheckPlan(BaseModel):
    cid: str = Field(description='G1, G2, ...')
    covers: List[str] = Field(description='requirement rids')
    method: str = Field(description='what the checker runs itself on the delivered files, with which open-source '
                                    'tool; never a number the agent reports; names the output file it reads')
    tolerance: str = Field(description='how it accepts handbook ranges and reasonable method differences, so any '
                                       'sound design passes')
    held_out: str = Field(description='test points sampled inside a declared range, if any; empty otherwise')


class PublicDesign(BaseModel):
    name: str
    fails: str = Field(description='the requirement rid it fails and by roughly how much')


class Asset(BaseModel):
    path: str = Field(description='path under the input root (model-visible)')
    purpose: str
    content: str = Field(description='the complete file text (small text files only)')


class Draft(BaseModel):
    title: str = Field(description='functional title: the required outcome and context, no open mechanism named')
    slug: str = Field(description='lowercase-hyphenated, under 50 characters')
    task_form: Literal['feasibility', 'target matching', 'tight target']
    design_freedom: Literal['parameter selection', 'configuration design', 'concept synthesis']
    instruction_md: str = Field(description='the brief: a short request in the voice of the person asking, stating '
                                            'every requirement, fixed interface and output file briefly; no hint of '
                                            'a benchmark, grader or environment, no solution hints')
    handbook_md: str = Field(description='the separate handbook: data a skilled engineer knows or looks up, constants '
                                         'as ranges; no requirement, interface or solution hint lives only here')
    requirements: List[Requirement]
    outputs: List[OutputFile]
    provided_assets: List[Asset] = Field(description='input files the task writes out in full; empty when the brief '
                                                     'states every fixed input')
    external_references: List[str] = Field(description='every real-world thing the brief or handbook names (part '
                                                       'number, standard, datasheet, library, vendor model), each '
                                                       'with why you are certain it exists; empty when none')
    grader_plan: List[CheckPlan]
    distinct_valid_designs: List[str] = Field(description='at least three materially different designs that would '
                                                          'pass, one line each (evaluator-only)')
    reference_outline: str = Field(description='how a reference solution solves it (evaluator-only, never in the '
                                               'brief or handbook)')
    catching_rows: List[str] = Field(description='rids of the requirements an agent showing the targeted limitation '
                                                 'fails')
    public_designs: List[PublicDesign] = Field(description='well-known public designs a solver would reach for and '
                                                           'the requirement each fails')
    mechanism_survival: str = Field(description='cite the limitation id and the requirement rows that catch it; why '
                                                'an agent with it still fails although it can simulate and '
                                                'measure its own design (rule 7)')


SYSTEM = """You write engineering design tasks ({discipline}) for a benchmark that measures AI agents. Each task
targets one limitation that real agent trials have shown, so it measures that weakness instead of general
difficulty. Every requirement must be measurable and every number must be something the stated tools could reproduce.
The solver works in {environment}. Tools the solver and checker have, for your feasibility reasoning and the check
plan only (never name them in the brief or handbook): {tools}.

The task is STANDALONE. This is the contract; the brief states its interface parts in plain words (delivered file
path, ports or fields, roles):
{contract}

The verified asset inventory the guidelines mention is EMPTY. Never state or imply that something exists unless the brief or one of your provided files holds it in full. If you
are not certain a real part, standard or document exists, do not name it: give the parameters instead.

Follow the guidelines below strictly. They override the concept, the cell and the targeted limitation wherever those
conflict with them.

<guidelines>
{guidelines}
</guidelines>"""

PROMPT = """Write one task draft from this concept.

{cell}

Concept (keep its engineering decision, fix anything that breaks the guidelines):
{concept}
{retry}
Already drafted in this run (do not repeat these topics): {previous}

Requirements for the draft (guidelines/04_generation_rules.md is checked mechanically; a draft that breaks it is
sent back):
- Brief (instruction_md): aim for 400 words and keep within 450 words. Move supporting detail to the handbook. Word count alone must not cause rejection or a revision request. A request in the voice of the person in the framing. Two or
  three sentences of context, then every requirement (metric, condition, location, limit), the fixed interfaces and
  each output file with its format. Nothing else: no software, tool, solver or setting names; no test bench; no
  procedure; no preference without a number; no benchmark words; no canary. Apply the three sentence tests.
- Handbook (handbook_md): at most {handbook_max_words} words. Data an engineer would know or look up (constants and
  practice as ranges), measurement definitions and reference conventions. Graded limits and interfaces stay in the
  brief; no hidden limits.
- Standalone inputs: every fixed input is a number the brief states or a small text file you write out in full in
  provided_assets (schema, units, ranges, consistent with the stated physics). No starter project, library, vendor
  model or part you cannot place. List anything real you name in external_references.
- Outputs: only files a check in the plan reads (name the file in the check's method), under {artifact_root}.
- One concrete {unit}, small enough to finish in one attempt (2 h, 400 turns, 10M tokens).
- An agent that shows the targeted limitation must fail at least one requirement; list those in catching_rows. No
  route around the limitation may pass every requirement. Do not point at the trap.
- The task must be hard for a strong agent for real engineering reasons: limits leave a narrow window around the
  targeted trade-off, not large margins for any sensible design, and no public design meets every requirement.
- The check plan measures the delivered files themselves, covers every requirement and operating state, accepts every
  valid design, and uses held-out points inside declared ranges where the limitation needs them.
- Complete JSON: at most {draft_max_words} words across all fields. Keep reference_outline at most
  {reference_max_words} words and all provided file content together at most {assets_max_chars} characters."""


def system_prompt(domain):
    return SYSTEM.format(discipline=domain['discipline'], environment=domain['environment'], tools=domain['tools'],
                         contract=domain['deliverable_contract'], guidelines=guidelines())


def user_prompt(domain, cell, concept, retry, previous):
    return PROMPT.format(cell=cell, concept=concept.model_dump_json(indent=1), retry=retry,
                         previous='; '.join(previous) or 'none', max_words=domain.get('brief_max_words', 400),
                         aim_words=int(domain.get('brief_max_words', 400) * 0.82),
                         handbook_max_words=domain.get('handbook_max_words', 1000),
                         reference_max_words=domain.get('reference_outline_max_words', 400),
                         draft_max_words=domain.get('draft_max_words', 6000),
                         assets_max_chars=domain.get('generated_assets_max_chars', 12000),
                         artifact_root=domain['artifact_root'], unit=domain['unit_of_design'])


def abstract(lim):
    """The limitation as the generator sees it: the mechanism, never the tasks it was mined from."""
    return {k: lim[k] for k in ('id', 'name', 'mechanism', 'how_to_elicit', 'how_to_grade', 'confidence')}


def stratified(axes, keys, n, rng):
    pools, out = {k: [] for k in keys}, []
    for _ in range(n):
        p = {}
        for k in keys:  # refill each axis pool when empty: every value is used before any repeats
            if not pools[k]:
                pools[k] = rng.sample(axes[k], len(axes[k]))
            p[k] = pools[k].pop()
        out.append(p)
    return out


class Sampler:
    """Cell sampling against the ledger of accepted tasks (plus this run's picks)."""

    def __init__(self, domain, n, seed=0):
        self.domain, self.rng = domain, random.Random(seed)
        self.lims = domain.limitations()['limitations']
        if not self.lims:
            raise SystemExit(f'{domain.limitations_file} has no limitations: re-run mine.py')
        self.vocab = domain.vocab()
        self.profiles = stratified(domain.axes(), domain['secondary_axes'], n, self.rng)
        self.fam_pools = {}
        self.cell, self.single, self.pair = Counter(), Counter(), Counter()
        for e in domain.ledger():
            if e['status'] == 'accepted':
                self.count(e['limitation'], e['subdomain'], e['descriptors'])

    def count(self, lim_id, subdomain, descriptors):
        self.cell[(lim_id, subdomain)] += 1
        for dim, val in descriptors.items():
            self.single[(subdomain, dim, val)] += 1
            self.pair[(lim_id, subdomain, dim, val)] += 1

    def pick(self, i, subdomain=None):
        sub = subdomain or self.rng.choice(self.domain.subdomains())
        w = [CONF_WEIGHT[l['confidence']] / (1 + self.cell[(l['id'], sub)]) for l in self.lims]
        lim = self.rng.choices(self.lims, weights=w)[0]
        pool = self.fam_pools.setdefault(sub, [])
        if not pool:
            fams = self.domain.families(sub)
            pool.extend(self.rng.sample(fams, len(fams)))
        desc = {}
        for dim, vals in self.vocab.items():
            w = [1 / (1 + self.single[(sub, dim, v)] + 2 * self.pair[(lim['id'], sub, dim, v)]) for v in vals]
            desc[dim] = self.rng.choices(vals, weights=w)[0]
        return sub, lim, pool.pop(), desc, self.profiles[i]


def generate(domain, cell, concept, retry, previous, model):
    """Returns (Draft, system, prompt)."""
    system, prompt = system_prompt(domain), user_prompt(domain, cell, concept, retry, previous)
    return llm.generate_parse(system, prompt, Draft, model=model), system, prompt


class Edited(BaseModel):
    instruction_md: str
    handbook_md: str


FIX = """Edit the brief and handbook of this engineering task so they follow the rules. Change only what the feedback
names and keep everything else word for word. Never change a requirement's number, unit, limit or condition, the
fixed interfaces or the output files. A sentence that breaks a rule because of the words it uses (a tool, a solver
setting, a preference, a benchmark word) is rephrased as a plain engineering definition or, if it is only rationale
or procedure, deleted; a definition that changes pass or fail may move to the handbook. The brief must end up at most
{target} words and the handbook at most {handbook_max}.

Feedback:
{problems}

<brief words="{words}">
{brief}
</brief>

<handbook>
{handbook}
</handbook>

Return the complete instruction_md and handbook_md."""


def fix_text(domain, d, problems, model):
    """One edit call on the brief and handbook only; far cheaper than regenerating the whole draft."""
    import lint
    limit = domain.get('brief_max_words', 400)
    out = llm.generate_parse('You edit engineering task text to meet rules and change nothing else.',
                             FIX.format(target=int(limit * 0.9), handbook_max=domain.get('handbook_max_words', 1000),
                                        problems='\n'.join(f'- {p}' for p in problems),
                                        words=lint.words(d.instruction_md), brief=d.instruction_md,
                                        handbook=d.handbook_md), Edited, model=model)
    d.instruction_md, d.handbook_md = lint.strip_canary(out.instruction_md), lint.strip_canary(out.handbook_md)
