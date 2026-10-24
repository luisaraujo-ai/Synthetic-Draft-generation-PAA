"""Stage 1, Rainbow-Teaming style: sample a grid cell, adapt it into a concept, and generate a draft.

For each draft, sample a subdomain of the domain (run.py --subdomain optionally fixes it):
1. Cell: a limitation from the mined catalogue x the subdomain. Limitations are weighted by confidence and by how
   few drafts their cell already holds (archive grid), so empty cells fill first. Inside the cell, the circuit family
   rotates and the descriptors (phenomenon, design freedom, reasoning) go to under-represented values; the secondary
   axes (task form, framing, acceptance evidence) are stratified.
2. Prompt adapter (adapter.py, cheap model): writes a task concept for the cell, or mutates the cell's elite concept
   when there is one.
3. Generator (Claude Opus, high effort): writes the full draft from the concept, the cell and the guidelines. It never
   sees existing tasks (no seeds, no parent tasks, no neighbours), so the mined tasks cannot pull every draft toward
   themselves; the limitations carry what was learned from them.
4. Filters: the rule check (lint.py) and the novelty check against the archive (archive.novelty). A draft that fails
   either is regenerated with the reason (--retries); every near-duplicate stays in the archive as a failed attempt.
"""
import json
import random
import time
from collections import Counter
from typing import List, Literal

from pydantic import BaseModel, Field

import adapter
import archive
import lint
import llm
from archive import Structure
from domain import guidelines

CONF_WEIGHT = {'high': 3.0, 'medium': 2.0, 'low': 1.0}
TRIM_FROM_ATTEMPT = 1  # from the second generation on, an over-long brief is trimmed instead of rewritten whole


def _save_timing(path, timing):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(timing, indent=1, ensure_ascii=False))


def _timed_phase(timing, path, phase, call):
    """Run one sampling phase and persist live state, including failures and interruptions."""
    timing['current_phase'] = phase
    _save_timing(path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
    started = time.monotonic()
    try:
        result = call()
    except BaseException as exc:
        timing.update(status='failed', current_phase=phase, error=f'{type(exc).__name__}: {exc}',
                      elapsed_seconds=round(time.monotonic() - timing['_monotonic_start'], 3))
        _save_timing(path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
        raise
    elapsed = round(time.monotonic() - started, 3)
    timing['current_phase'] = None
    _save_timing(path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
    return result, elapsed


class Requirement(BaseModel):
    rid: str = Field(description='R1, R2, ...')
    metric: str = Field(description='the measured quantity, e.g. "output ripple, peak to peak"')
    condition: str = Field(description='state, load, corner or configuration it must hold under')
    location: str = Field(description='where it is measured or extracted (node, port, part)')
    limit: str = Field(description='minimum, maximum or band, with units, e.g. "<= 8 mV"')


class OutputFile(BaseModel):
    path: str = Field(description='exact path of the delivered file (see the deliverable contract)')
    format: str
    contents: str = Field(description='fields, units and structure the grader reads; published in the brief')


class GraderCheck(BaseModel):
    cid: str = Field(description='G1, G2, ...')
    primitive: str = Field(description='the verifier check id that measures it, if you know one (e.g. ac.bandwidth), '
                                       'or "custom: <what>"; name the open-source tool and metric in method either way')
    covers: List[str] = Field(description='requirement rids')
    method: str = Field(description='what the grader runs itself on the delivered files, with which open-source '
                                    'tool; never a number the agent reports')
    reports: str = Field(description='the measured value, limit and unit it writes')
    tolerance: str = Field(description='how it accepts handbook ranges and reasonable method differences, so any '
                                       'sound design passes')
    held_out: str = Field(description='test points sampled inside a declared range, if any; empty otherwise')


class PublicDesign(BaseModel):
    name: str
    source: str = Field(description='where it is published')
    fails: str = Field(description='the requirement rid it fails and by roughly how much')


class Asset(BaseModel):
    path: str = Field(description='path under the input root (model-visible)')
    purpose: str
    must_be_consistent_with: str = Field(description='which simulated or stated numbers this asset has to agree with')
    status: Literal['available', 'generated'] = Field(description='available: listed in the asset inventory; '
                                                                  'generated: its full text is in content')
    content: str = Field(description='the complete file text when generated (small text files only); empty when '
                                     'available')


class Draft(BaseModel):
    title: str = Field(description='functional title: the required outcome and context, no open mechanism named')
    slug: str = Field(description='lowercase-hyphenated, under 50 characters')
    one_line: str
    task_form: Literal['feasibility', 'target matching', 'tight target', 'diagnosis and repair']
    design_freedom: Literal['parameter selection', 'configuration design', 'concept synthesis']
    instruction_md: str = Field(description='the brief: a short request in the voice of the person asking, stating '
                                            'every requirement, fixed interface and output file briefly; no hint of '
                                            'a benchmark, grader or environment, no solution hints')
    handbook_md: str = Field(description='the separate handbook: data a skilled engineer knows or looks up, with '
                                         'grade and source for every value and constants as ranges; no requirement, '
                                         'interface or solution hint lives only here')
    parts_catalogue_md: str = Field(description='parts table (part numbers, dimensions, ratings) when the design '
                                                'uses bought-in parts; empty string otherwise')
    requirements: List[Requirement]
    outputs: List[OutputFile]
    provided_assets: List[Asset]
    grader_checks: List[GraderCheck]
    tools: List[str] = Field(description='open-source, headless tools the agent and grader use, versions pinned')
    fixed_items: List[str]
    open_decisions: List[str] = Field(description='decisions that belong to the solver')
    distinct_valid_designs: List[str] = Field(description='at least three materially different designs that would '
                                                          'pass, one line each (evaluator-only)')
    budget_fit: str = Field(description='why one attempt fits 2 h, 400 turns and 10M tokens; caps on expensive tools')
    difficulty_target: Literal['easy', 'medium', 'hard']
    difficulty_reason: str
    reference_outline: str = Field(description='how the reference solution solves it (evaluator-only, never in the '
                                               'brief or handbook)')
    catching_rows: List[str] = Field(description='rids of the requirements an agent showing the targeted limitation '
                                                 'fails')
    why_hard: str = Field(description='cite the limitation id; the path by which an agent with it goes wrong, and '
                                      'which rows catch it')
    open_risks: List[str] = Field(description='what an SME must check or build before this can ship')
    structure: Structure = Field(description='engineering structure of this task, for the archive and novelty check')
    public_designs: List[PublicDesign] = Field(description='well-known public designs a solver would reach for '
                                                           '(library cells, reference designs, app notes, textbook '
                                                           'circuits, catalogue parts) and the requirement each fails')
    mechanism_survival: str = Field(description='why an agent with the targeted limitation still fails although it '
                                                'can simulate and measure its own design (rule 7)')
    transformation_rationale: str = Field(description='the engineering decision this task requires that a generic '
                                                      'task in its subdomain would not')


SYSTEM = """You write engineering design tasks ({discipline}) for a benchmark that measures AI agents. Each task
targets one limitation that real agent trials have shown, so it measures that weakness instead of general
difficulty. You write a DRAFT that an evaluator will build and test and a subject-matter expert will finish: every
requirement must be measurable and every number must be something the stated tools could reproduce. The solver works
on {environment}. Tools the solver and grader have, for your feasibility reasoning and the grader plan only (never
name them in the brief or handbook): {tools}.

Current execution backend contract (a capability constraint, not an authoring rule; if the guidelines require a different artifact, describe the needed backend work in open_risks and keep the draft blocked from execution). The brief states the interface parts of it in plain words: the delivered file path, the
published ports and roles, the part-field rules:
{contract}

Follow the guidelines below strictly. They override the concept, the cell and the targeted limitation wherever those
conflict with them.

<guidelines>
{guidelines}
</guidelines>"""

PROMPT = """Write one task draft from this concept.

{cell}

Concept (from the prompt adapter; keep its engineering decision, fix anything that breaks the guidelines):
{concept}
{retry}
Already drafted in this run (do not repeat these topics): {previous}

Requirements for the draft (guidelines/04_generation_rules.md is checked mechanically; a draft that breaks it is
sent back):
- Brief (instruction_md): at most {max_words} words, a request in the voice of the person in the framing. Two or
  three sentences of context, then every requirement (metric, condition, location, limit), the fixed interfaces and
  each output file with its format. Nothing else: no software, tool, solver or setting names; no test bench; no
  procedure; no preference without a number; no benchmark words; no canary. Apply the three sentence tests.
- Handbook (handbook_md): at most {handbook_max_words} words. Include only data an engineer would know or look up
  (grade and source per value, constants and practice as ranges). Put detailed reference conventions and measurement definitions here per client feedback. Keep graded limits,
  interfaces and concise unambiguous acceptance definitions in the brief; no hidden limits.
- Assets: follow rules 9 and 15–17. Inventory entries may be called available; generated assets need complete
  content and physical validation. Describe unbuilt assets as author-facing SME prerequisites in open_risks,
  including interfaces, ranges, validation tests, effort and A/B/C classification. Keep them out of the brief
  and provided_assets until built. A concept with prerequisites remains blocked from execution.
  Prefer JSON inputs and physical design decisions when appropriate; preserve topology or geometry tasks when
  essential. Include schemas, units, sources, governing equations and generation methods in the asset description.
  Asset inventory: {inventory}
- Outputs: only files a grader check reads (name the file in the check's method), at the paths of the deliverable
  contract. The brief publishes the ports and roles every requirement is measured at.
- One concrete {unit}, small enough to finish in one attempt (2 h, 400 turns, 10M tokens).
- An agent that shows the targeted limitation must fail at least one requirement; list those in catching_rows. No
  route around the limitation may pass every requirement. Do not point at the trap.
- public_designs: the public designs a solver would reach for, each with the requirement it fails.
- mechanism_survival: why the limitation still bites when the solver simulates its own design (rule 7).
- Grader checks measure the delivered files themselves, cover every requirement and every operating state, accept
  every valid design and report measured value, limit and unit. Use held-out points inside declared ranges where the
  limitation needs them. The grader will be built and run, so keep every check computable with the listed tools.
- Limits leave a narrow window around the targeted trade-off, not large margins for any sensible design.
- Complete JSON: at most {draft_max_words} words across all fields, including generated assets. Keep
  parts_catalogue_md at most {parts_max_words} words, reference_outline at most {reference_max_words} words, and all
  generated asset content together at most {assets_max_chars} characters. Use one concise line per list item. Do not
  repeat the brief, requirements, checks, risks or rationale in other fields. These are hard limits, not targets."""


class Trimmed(BaseModel):
    instruction_md: str
    handbook_md: str


TRIM_SYSTEM = """You edit a draft engineering task so it meets the generation rules. You change only what the
feedback names. You never change the engineering: the requirements, limits, interfaces and output files stay."""

TRIM = """Shorten the brief (instruction_md) below to at most {max_words} words. Keep every requirement with its
number, unit, limit and condition, the fixed interfaces, the ports and each output file. Move definitions, rationale
and procedure to the handbook (handbook_md), which must stay within {handbook_max_words} words. Keep the brief free of
software or tool names, solver settings, procedures, preference words and benchmark words (grader, agent, sandbox).

Feedback from the rule check:
{problems}

Brief ({words} words):
<brief>
{brief}
</brief>

Handbook:
<handbook>
{handbook}
</handbook>

Return the complete instruction_md and handbook_md."""


def trim(d, problems, domain, model):
    """Cut an over-long brief with a short edit call, which keeps the rest of the draft as it is."""
    prompt = TRIM.format(max_words=domain.get('brief_max_words', 400),
                         handbook_max_words=domain.get('handbook_max_words', 1000),
                         problems='\n'.join(f'- {p}' for p in problems), words=lint.words(d.instruction_md),
                         brief=d.instruction_md, handbook=d.handbook_md)
    out = llm.generate_parse(TRIM_SYSTEM, prompt, Trimmed, model=model)
    return lint.strip_canary(out.instruction_md), lint.strip_canary(out.handbook_md)


class Audit(BaseModel):
    violations: List[str] = Field(description="One line per broken rule, starting 'rule N:', naming the draft text "
                                              "that breaks it and the fix. Empty when the draft follows every rule.")


AUDIT_SYSTEM = """You are a strict reviewer of engineering task drafts against the generation rules below. You report
rule breaks only; you do not rate the engineering.

<guidelines>
{guidelines}
</guidelines>"""

AUDIT = """Audit the draft below against rules 6 to 17 and rule 9. Report a violation only where the draft breaks a rule
and the text shows it.

Apply rule 9 and rules 15–17 as written. Distinguish available assets, proposed generated assets and
unbuilt SME prerequisites. Generated physical models are permitted when necessary and validated; never treat
unbuilt prerequisites as supplied. Check physical consistency, asset specifications and validation plans.

Asset inventory: {inventory}

Draft (JSON):
{draft}"""


def audit(d, domain):
    """Rules 6-17 and rule 9 naming, which lint.py cannot check mechanically; empty when the draft follows them."""
    prompt = AUDIT.format(inventory=inventory(domain), draft=d.model_dump_json(indent=1))
    return llm.review_parse(AUDIT_SYSTEM.format(guidelines=guidelines()), prompt, Audit).violations


def retry_note(d, problems):
    """Feedback for the next generation: the violations and the previous brief to edit, not start over from."""
    return (f'\nYour previous attempt "{d.title}" broke the generation rules and was sent back:\n' +
            '\n'.join(f'- {p}' for p in problems) +
            f'\n\nYour previous brief, to edit rather than rewrite from scratch:\n<previous_brief>\n'
            f'{d.instruction_md}\n</previous_brief>\nFix every point; preserve the engineering where feasible. If assets are missing, describe author-facing prerequisites and remove claims that they are supplied.\n')


def keep_rejected(out_dir, i, attempt, d, reason):
    """Keep every generated draft that is sent back, with why, so the failure can be read after the run."""
    kept_dir = out_dir.parent / 'rejected_drafts' / f'{i:02d}'
    kept_dir.mkdir(parents=True, exist_ok=True)
    (kept_dir / f'attempt-{attempt + 1}.json').write_text(json.dumps(
        {'reason': reason, 'draft': d.model_dump()}, indent=1, ensure_ascii=False))


def system_prompt(domain):
    return SYSTEM.format(discipline=domain['discipline'], environment=domain['environment'], tools=domain['tools'],
                         contract=domain.get('deliverable_contract', '(free)'), guidelines=guidelines())


def inventory(domain):
    inv = domain.get('asset_inventory', [])
    return '; '.join(f"{a['path']} ({a['what']})" for a in inv) if inv else 'empty: no input files exist unless you generate them'


def abstract(lim):
    """The limitation as the generator and adapter see it: the mechanism, never the tasks it was mined from."""
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


def pick_limitation(lims, arc, subdomain, rng, run_counts):
    """Rainbow cell sampling: weight = confidence / (1 + drafts already in the cell + drafts this run)."""
    w = [CONF_WEIGHT[l['confidence']] / (1 + len(archive.cell_drafts(arc, l['id'], subdomain)) + run_counts[l['id']])
         for l in lims]
    return rng.choices(lims, weights=w)[0]


def sample(domain, subdomain, n, out_dir, seed=0, retries=3, generator_model=None):
    sampling_started = time.monotonic()
    timing_path = out_dir.parent / 'sampling_timing.json'
    timing = {'status': 'running', 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'requested_drafts': n, 'accepted_count': 0, 'archive_index_seconds': None,
              'time_to_first_accepted_seconds': None, 'candidates': [], 'current_phase': None,
              '_monotonic_start': sampling_started}
    _save_timing(timing_path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
    generator_model = generator_model or llm.GENERATOR_MODEL
    rng = random.Random(seed)
    cat = domain.limitations()
    if not cat['limitations']:
        raise SystemExit(f'{domain.limitations_file} has no limitations: add trials to trials.json and re-run mine.py')
    subdomains = archive.subdomains(domain)
    if subdomain is not None and subdomain not in subdomains:
        raise SystemExit(f'unknown subdomain "{subdomain}"; choose one of: {subdomains}')
    fixed_subdomain = subdomain
    arc, timing['archive_index_seconds'] = _timed_phase(
        timing, timing_path, 'archive_index', lambda: archive.index_existing(domain))
    axes = archive.axes(domain)
    fam_pools = {}
    profiles = stratified(axes, domain['secondary_axes'], n, rng)
    run, unit, system = out_dir.parent.name, domain['unit_of_design'], system_prompt(domain)
    run_counts, drafts = Counter(), []
    for i in range(n):
        candidate_started = time.monotonic()
        candidate_timing = {'number': i + 1, 'status': 'running', 'subdomain': None,
                            'concept_seconds': None, 'attempts': []}
        timing['candidates'].append(candidate_timing)
        subdomain = fixed_subdomain if fixed_subdomain is not None else rng.choice(subdomains)
        candidate_timing['subdomain'] = subdomain
        cell_counts = Counter({l['id']: run_counts[(l['id'], subdomain)] for l in cat['limitations']})
        lim = pick_limitation(cat['limitations'], arc, subdomain, rng, cell_counts)
        run_counts[(lim['id'], subdomain)] += 1
        fam_pool = fam_pools.setdefault(subdomain, [])
        if not fam_pool:
            fams = archive.families(domain, subdomain)
            fam_pool.extend(rng.sample(fams, len(fams)))
        family, prof = fam_pool.pop(), profiles[i]
        target = archive.pick_descriptors(domain, arc, lim['id'], subdomain, rng)
        parent = archive.elite(arc, lim['id'], subdomain)
        print(f'  draft {i + 1}/{n}: creating concept with {llm.ADAPTER_MODEL}', flush=True)
        adapted, candidate_timing['concept_seconds'] = _timed_phase(
            timing, timing_path, f'draft_{i + 1}_concept',
            lambda: adapter.adapt(domain, abstract(lim), subdomain, family, target, prof, parent,
                                  archive.failed_attempts(arc, lim['id'], subdomain)))
        concept, mode = adapted
        print(f'  draft {i + 1}/{n}: concept ready ({mode}); generating with {generator_model}', flush=True)
        cell = adapter.cell_text(abstract(lim), subdomain, family, target, prof)
        near = archive.neighbours(arc, subdomain, target, lim['id'])
        retry = ''
        for attempt in range(retries + 1):
            attempt_timing = {'attempt': attempt + 1, 'generation_seconds': None, 'lint_seconds': None,
                              'novelty_seconds': None, 'result': None}
            candidate_timing['attempts'].append(attempt_timing)
            prompt = PROMPT.format(cell=cell, concept=concept.model_dump_json(indent=1), retry=retry,
                                   previous='; '.join(d.title for d in drafts) or 'none',
                                   max_words=domain.get('brief_max_words', 400),
                                   handbook_max_words=domain.get('handbook_max_words', 1000),
                                   parts_max_words=domain.get('parts_catalogue_max_words', 500),
                                   reference_max_words=domain.get('reference_outline_max_words', 400),
                                   draft_max_words=domain.get('draft_max_words', 6500),
                                   assets_max_chars=domain.get('generated_assets_max_chars', 12000),
                                   inventory=inventory(domain), unit=unit)
            prompt_dir = out_dir.parent / 'generation_prompts' / f'{i:02d}'
            prompt_dir.mkdir(parents=True, exist_ok=True)
            (prompt_dir / f'attempt-{attempt + 1}.json').write_text(json.dumps(
                llm.generation_request_record(system, prompt, Draft, generator_model), indent=1,
                ensure_ascii=False))
            print(f'  draft {i + 1}/{n}: generation attempt {attempt + 1}/{retries + 1}', flush=True)
            d, attempt_timing['generation_seconds'] = _timed_phase(
                timing, timing_path, f'draft_{i + 1}_generation_attempt_{attempt + 1}',
                lambda: llm.generate_parse(system, prompt, Draft, model=generator_model))
            d.slug = f'{i:02d}-{d.slug}'[:60]
            d.instruction_md, d.handbook_md = lint.strip_canary(d.instruction_md), lint.strip_canary(d.handbook_md)
            lint_started = time.monotonic()
            problems = lint.check(d, domain)
            if attempt >= TRIM_FROM_ATTEMPT and any(p.startswith('rule 4: the brief has') for p in problems):
                print(f'  draft {i + 1}/{n}: trimming the brief', flush=True)
                d.instruction_md, d.handbook_md = trim(d, problems, domain, generator_model)
                attempt_timing['trimmed'] = True
                problems = lint.check(d, domain)
            attempt_timing['lint_seconds'] = round(time.monotonic() - lint_started, 3)
            if not problems:
                problems, attempt_timing['audit_seconds'] = _timed_phase(
                    timing, timing_path, f'draft_{i + 1}_audit_attempt_{attempt + 1}', lambda: audit(d, domain))
            if problems:
                attempt_timing['result'] = 'lint_rejected'
                print(f'  sent back {d.slug}: ' + ' | '.join(p.split(':')[0] for p in problems), flush=True)
                keep_rejected(out_dir, i, attempt, d, problems)
                retry = retry_note(d, problems)
                continue
            print(f'  draft {i + 1}/{n}: lint passed; checking novelty with {llm.NOVELTY_MODEL}', flush=True)
            entry = {'id': f'{run}/{d.slug}', 'kind': 'draft', 'status': 'drafted', 'title': d.title,
                     'summary': d.one_line, 'subdomain': subdomain, 'descriptors': target,
                     'structure': d.structure.model_dump(), 'failure_mechanisms': [lim['id']],
                     'concept': concept.model_dump(), 'fitness': None, 'elite': False, 'notes': [],
                     'provenance': {'run': run, 'attempt': attempt, 'mode': mode,
                                    'parent': parent['id'] if parent else None, 'family': family,
                                    'rationale': d.transformation_rationale, 'neighbours': [e['id'] for e in near]}}
            nov, attempt_timing['novelty_seconds'] = _timed_phase(
                timing, timing_path, f'draft_{i + 1}_novelty_attempt_{attempt + 1}',
                lambda: archive.novelty(entry, near))
            entry['provenance']['novelty'] = nov
            if nov.get('novel', True):
                attempt_timing['result'] = 'accepted'
                break
            attempt_timing['result'] = 'novelty_rejected'
            keep_rejected(out_dir, i, attempt, d, nov)
            entry['status'] = 'rejected-duplicate'
            entry['id'] += f'~rejected{attempt}'
            entry['notes'] = [f"near-duplicate of {nov.get('nearest')}: {nov.get('reason')}"]
            archive.add(arc, entry)
            archive.save(domain, arc)
            print(f"  rejected {d.slug}: near-duplicate of {nov.get('nearest')} ({nov.get('reason')})", flush=True)
            retry = (f"\nYour previous attempt \"{d.title}\" was rejected as a near-duplicate of an existing task: "
                     f"{nov.get('reason')} Shared: {'; '.join(nov.get('shared', []))}. Change the engineering decision, "
                     "not the wording or constants.\n")
        else:
            candidate_timing.update(status='skipped',
                                    sampling_acceptance_seconds=round(time.monotonic() - candidate_started, 3))
            _save_timing(timing_path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
            print(f'  no clean, novel draft for {lim["id"]} after {retries + 1} attempts: skipped', flush=True)
            continue
        archive.add(arc, entry)
        archive.save(domain, arc)
        drafts.append(d)
        candidate_timing.update(status='accepted', slug=d.slug,
                                sampling_acceptance_seconds=round(time.monotonic() - candidate_started, 3))
        timing['accepted_count'] = len(drafts)
        if timing['time_to_first_accepted_seconds'] is None:
            timing['time_to_first_accepted_seconds'] = round(time.monotonic() - sampling_started, 3)
        _save_timing(timing_path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
        dd = out_dir / d.slug
        dd.mkdir(parents=True, exist_ok=True)
        (dd / 'instruction.md').write_text(d.instruction_md)
        (dd / 'handbook.md').write_text(d.handbook_md)
        (dd / 'draft.json').write_text(json.dumps(
            {'domain': domain.name, 'subdomain': subdomain, 'archive_id': entry['id'], 'limitation': lim,
             'target_descriptors': target, 'family': family, 'profile': prof, 'concept': concept.model_dump(),
             'generator_model': generator_model, 'catalogue_generated': cat['generated'], 'catalogue_models': cat['models'],
             'provenance': entry['provenance'], 'sampling_timing': candidate_timing,
             'draft': d.model_dump()}, indent=1, ensure_ascii=False))
        print(f'  drafted {d.slug} in {candidate_timing["sampling_acceptance_seconds"]:.1f}s '
              f'[{lim["id"]}, {mode}]: {d.title}', flush=True)
    timing.update(status='complete', current_phase=None,
                  elapsed_seconds=round(time.monotonic() - sampling_started, 3))
    accepted_times = [c['sampling_acceptance_seconds'] for c in timing['candidates']
                      if c['status'] == 'accepted']
    timing['mean_sampling_acceptance_seconds'] = round(sum(accepted_times) / len(accepted_times), 3) \
        if accepted_times else None
    timing['end_to_end_seconds_per_accepted'] = round(timing['elapsed_seconds'] / len(accepted_times), 3) \
        if accepted_times else None
    _save_timing(timing_path, {k: v for k, v in timing.items() if k != '_monotonic_start'})
    return drafts


REVISE = """Revise your draft task. A strict evaluator has gone through it; its findings are below. Return the
complete revised draft.

Targeted limitation (keep targeting it; do not water it down to make the task pass review):
{limitation}

Your current draft:
<draft>
{draft}
</draft>

The evaluator's findings:
<review>
{review}
</review>

Fix every failing or unclear rule and act on each fix, most important first. Where the evaluator says Astra passes
too often, or its own build passed easily, change the engineering so the targeted trade-off binds (narrower window,
held-out points inside declared ranges, an effect the solver must think to model), not the wording. Where its build
or the grader failed for reasons that are not the limitation (a grader bug, an infeasible limit, a missing input),
fix the task. Keep what passed. Keep the slug. All generation rules still apply (they are checked mechanically):
brief at most {max_words} words, no software or solver settings, no bench, no ungraded preferences, only available or
generated assets (inventory: {inventory})."""


def revise(domain, draft, limitation, review_json):
    """One revision from evaluator findings (refine.py), on the cheaper revision model (llm.REVISE_MODEL at
    llm.REVISE_EFFORT). Returns a Draft that passed the rule check, or the last attempt with its violations."""
    prompt = REVISE.format(limitation=json.dumps(abstract(limitation), indent=1, ensure_ascii=False),
                           draft=json.dumps(draft, indent=1, ensure_ascii=False), review=review_json,
                           max_words=domain.get('brief_max_words', 400), inventory=inventory(domain))
    problems = []
    for _ in range(3):
        d = llm.claude_parse(system_prompt(domain), prompt + (('\n\nYour last revision broke the generation rules:\n' +
                                                                 '\n'.join(f'- {p}' for p in problems)) if problems else ''),
                             Draft, effort=llm.REVISE_EFFORT, model=llm.REVISE_MODEL, purpose='revise')
        d.slug = draft['slug']
        d.instruction_md, d.handbook_md = lint.strip_canary(d.instruction_md), lint.strip_canary(d.handbook_md)
        problems = lint.check(d, domain)
        if not problems:
            break
    return d, problems
