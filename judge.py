"""The judge: one call that decides whether a draft is a good task and whether it looks hard.

1. Guidelines: every required criterion below, judged against guidelines/ (vendor guidelines, function-first primer,
   client feedback, generation rules), including that the task is standalone and grounded: nothing it relies on is
   assumed to exist unless the draft states or writes it out.
2. Difficulty: the judge solves the task in its head the way a strong frontier agent (Astra) would, names the easiest
   route and where it breaks, and estimates the pass probability per attempt.
A task is accepted when no required criterion fails and the estimated probability is at most domain.json
"astra_target". SKILL.md is not applied here; it is checked by hand (idea_review.py can screen a run's tasks).
The probability is the judge model's opinion, not a measurement; real trials are the calibration.
"""
import json
import hashlib
import os
from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, Field

import llm
from domain import guidelines


class Criterion(BaseModel):
    rule: str = Field(description='the criterion name exactly as listed in the prompt')
    verdict: Literal['pass', 'fail', 'unclear']
    evidence: str = Field(description='short quote, number or what is missing')
    fix: str = Field(description='the concrete change; empty when it passes')


class Difficulty(BaseModel):
    easiest_route: str = Field(description='the most likely way a strong agent passes, step by step')
    where_it_fails: str = Field(description='the specific requirement and moment where it most likely fails, and why')
    p_pass_per_attempt: float = Field(description='probability, 0 to 1, that one attempt passes every requirement')
    confidence: Literal['low', 'medium', 'high']
    harder_if: str = Field(description='the one change to the engineering that would make the trade-off bind more '
                                       '(narrower window, held-out point, effect to model); empty if already hard')


class Review(BaseModel):
    criteria: List[Criterion]
    difficulty: Difficulty
    summary: str = Field(description='two sentences: is it a good standalone task, and is it hard')


CRITERIA = (
    ('function-first', 'The brief states the required physical function, interfaces, conditions and outcomes, and '
                       'preserves real design freedom without revealing the solution.'),
    ('objective acceptance', 'Every mandatory performance criterion has a measurable quantity, conditions, units and '
                             'an explicit limit; no ungraded preference or hidden threshold.'),
    ('brief says what, not how', 'No software, solver settings, test bench or procedure in the brief; no benchmark words.'),
    ('short and human', 'The brief reads like a person asking a colleague; detail sits in the handbook; no graded value '
                        'exists only in the handbook.'),
    ('standalone', 'Every input the solver needs is stated in the brief or written out in full in the provided files. '
                   'Nothing is assumed to exist on disk or in an environment (starter project, library, vendor model, '
                   'reference design, datasheet).'),
    ('grounded references', 'Every part, standard, document or library named is real and you are certain of it, or '
                            'the parameters that matter are given in full. Flag anything that looks invented or whose '
                            'specifics you cannot confirm.'),
    ('feasible', 'Recompute the key numbers (units, time constants, bounds, power and energy balance) from the draft\'s '
                 'own values; distinguish demonstrated contradictions from provisional, unverified thresholds.'),
    ('verifiable', 'Every requirement can be measured from the delivered files by the stated open-source tools, without '
                   'trusting a number the solver reports, and the plan accepts every valid design.'),
    ('no public solution', 'No well-known public design meets every requirement; each listed public design fails a '
                           'named requirement.'),
    ('survives own checks', 'An agent with the targeted limitation still fails although it can simulate and measure '
                            'its own design; the reason is not "it might not check".'),
    ('binding limits', 'The limits leave a narrow window around the targeted trade-off, not large margins.'),
    ('targets limitation', 'Evaluator-only fields explain a concrete expected mistake, exposing conditions and why routine '
                           'self-verification may miss it. This is a hypothesis pending agent trials.'),
    ('fits one run', 'Fits about 2 hours and 400 model turns; the checker is computable in bounded time.'),
)

TEXT_CRITERIA = {'function-first', 'objective acceptance', 'brief says what, not how', 'short and human'}
# failures a rewrite of the brief and handbook alone can settle; the rest need the draft regenerated

MANDATORY_CRITERIA = {'function-first', 'objective acceptance', 'standalone', 'grounded references',
                      'verifiable', 'targets limitation'}
# Feasibility blocks only on a demonstrated contradiction, not uncertainty about a passing design.

SYSTEM = """Review engineering task drafts for handoff to an engineer, following the mandatory requirements in
01_fair_forge_vendor_guidelines.md. Where secondary authoring preferences demand more than those mandatory
requirements, treat them as advisory. Acceptance is readiness for engineering review, not benchmark validation.

Ground each objection in the actual text. Do not default to rejection merely because something is untested.
Secondary criteria (style, public-design comparisons, numerical tightness, estimated difficulty, and run-budget
estimates) cannot independently reject a task. If they expose a mandatory defect, report that defect under its
mandatory criterion with specific evidence. Do not propagate an uncertain claim into multiple failures.

For feasible: fail ONLY for a demonstrated internal contradiction or violation of a necessary physical bound;
show the calculation and assumptions. Use unclear for untested thresholds or insufficient evidence of a passing
design; that is an engineer-review advisory. Do not equate unverified with impossible.
For targets limitation: require an evaluator-only, concrete hypothesis naming the mistake, exposing conditions,
and why routine self-verification may miss it. Agent trials establish whether it actually works. Do not demand
proof that every possible solution route fails. For survives own checks, assess that hypothesis, not certainty.
Never weaken an explicit measurement definition or conceal a condition to preserve difficulty.
For grounded references and standalone: distinguish workflow-generated inputs supplied in full from invented
claims about existing files. Check dependency compatibility with current automatically generated inputs or
confirmed infrastructure. Explicitly declared remaining assumptions are not evidence of validation.
For verifiable: check the requirement-to-artifact/measurement/capability mapping and absence of hidden thresholds.

The guidelines every task follows:
<guidelines>
{guidelines}
</guidelines>"""


PROMPT = """Review this draft task.

The task targets this model limitation, observed in real trials of {models}:
<limitation>
{limitation}
</limitation>

{task}

Give exactly one criterion per name below, in this order:
{criteria}

Then judge difficulty. Work out the easiest route a strong frontier agent would take, with the 2 h, 400-turn budget and
internet access, before you estimate; say where it fails. Base the probability on the engineering, not on the author's
claims. Calibrate: 0.5 means a strong agent passes half its attempts; most tasks that pass guidelines review are
easier than their authors think."""


def draft_text(d):
    sec = lambda title, body: f'## {title}\n{body}\n' if body else ''
    rows = '\n'.join(f"- {r['rid']}: {r['metric']} | {r['condition']} | at {r['location']} | {r['limit']}"
                     for r in d['requirements'])
    checks = '\n'.join(f"- {c['cid']} covers {', '.join(c['covers'])}: {c['method']}; tolerance: {c['tolerance']}"
                       + (f"; held out: {c['held_out']}" if c['held_out'] else '') for c in d['grader_plan'])
    assets = '\n'.join(f"- {a['path']}: {a['purpose']}\n```\n{a['content']}\n```" for a in d['provided_assets'])
    return '\n'.join([
        f"# {d['title']}  (form: {d['task_form']}; freedom: {d['design_freedom']})",
        sec('Brief (model-visible)', d['instruction_md']), sec('Handbook (model-visible in handbook mode)', d['handbook_md']),
        sec('Provided input files (model-visible, written out in full)', assets),
        sec('Requirements as the author tabulated them', rows),
        sec('Output files', '\n'.join(f"- {o['path']} ({o['format']}): {o['contents']}" for o in d['outputs'])),
        sec('Check plan (evaluator-only)', checks),
        sec('Real-world things named, with the author\'s reason they exist',
            '\n'.join(f'- {x}' for x in d['external_references'])),
        sec('Distinct valid designs the author expects', '\n'.join(f'- {x}' for x in d['distinct_valid_designs'])),
        sec('Public designs the author checked', '\n'.join(f"- {x['name']}: fails {x['fails']}"
                                                           for x in d['public_designs'])),
        sec("Why the limitation survives the solver's own checks (author)", d['mechanism_survival']),
        sec('Catching rows (author)', ', '.join(d['catching_rows'])),
        sec('Reference outline (evaluator-only)', d['reference_outline'])])


def passes(review, target):
    """(accepted, failed criteria, hard enough)."""
    failed = [c.rule for c in review.criteria
              if (c.rule in MANDATORY_CRITERIA and c.verdict != 'pass')
              or (c.rule == 'feasible' and c.verdict == 'fail')]
    p = min(max(review.difficulty.p_pass_per_attempt, 0.0), 1.0)
    return not failed, failed, p <= target


HARD_CRITERIA = {'no public solution', 'survives own checks', 'binding limits', 'targets limitation'}


class SpecificationReview(BaseModel):
    criteria: List[Criterion]
    summary: str


class DifficultyReview(SpecificationReview):
    difficulty: Difficulty


def review(draft, limitation, models=(), effort=None, reasoning_tokens=None, checkpoint_dir=None):
    """Two bounded audits; combine every criterion exactly once in canonical order."""
    system = SYSTEM.format(guidelines=guidelines()) + (
        '\nAudit only your assigned criteria. Keep evidence concrete and concise (at most 100 words per '
        'criterion). Do not design a complete solution or iterate repairs. Identify decisive issues, then '
        'return the required JSON. Brief word count is advisory only and cannot alone fail a criterion.')
    lim = {k: limitation[k] for k in ('id', 'name', 'mechanism', 'how_to_elicit', 'how_to_grade')}
    context = draft_text(draft) + '\nTarget limitation:\n' + json.dumps(lim)
    context += '\nTarget models: ' + (', '.join(models) or 'the target model')
    results = []
    for stage, schema in [('specification', SpecificationReview), ('difficulty', DifficultyReview)]:
        criteria = [(n, d) for n, d in CRITERIA if (n in HARD_CRITERIA) == (stage == 'difficulty')]
        prompt = context + '\nReview ONLY these criteria, once each:\n'
        prompt += '\n'.join(f'{n}: {d}' for n, d in criteria)
        if results:
            prompt += ('\nPrior specification findings (do not re-audit these):\n' + results[0].model_dump_json()
                       + '\nAlso estimate difficulty using the easiest credible solution route. '
                       'Keep each difficulty field below 150 words. Calibrate 0.5 as a strong agent passing '
                       'half its attempts within 2 hours and 400 turns with internet access.')
        fingerprint = hashlib.sha256(json.dumps({'system':system, 'prompt':prompt,
            'model':llm.REVIEW_MODEL, 'effort':effort or llm.JUDGE_EFFORT,
            'reasoning_tokens':reasoning_tokens, 'schema':schema.model_json_schema()}, sort_keys=True).encode()).hexdigest()
        checkpoint = Path(checkpoint_dir) / (stage + '.json') if checkpoint_dir else None
        cached = json.loads(checkpoint.read_text()) if checkpoint and checkpoint.exists() else None
        if cached and cached.get('fingerprint') == fingerprint:
            result = schema.model_validate(cached['review'])
            llm.LEDGER.extend(cached.get('records', []))
        else:
            before = len(llm.LEDGER)
            result = llm.review_parse(system, prompt, schema, purpose='judge-' + stage,
                                      effort=effort or llm.JUDGE_EFFORT, request_timeout_s=llm.JUDGE_TIMEOUT_S,
                                      repairs=1, transport_retries=1, reasoning_tokens=reasoning_tokens,
                                      max_tokens=60000)
            expected = {n for n, _ in criteria}
            if len(result.criteria) != len(expected) or {c.rule for c in result.criteria} != expected:
                raise RuntimeError(f'{stage} review did not return each assigned criterion exactly once')
            if checkpoint:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                tmp = checkpoint.with_suffix('.tmp')
                tmp.write_text(json.dumps({'fingerprint':fingerprint, 'review':result.model_dump(),
                                           'records':llm.LEDGER[before:]}, indent=2))
                os.replace(tmp, checkpoint)
        expected = {n for n, _ in criteria}
        if len(result.criteria) != len(expected) or {c.rule for c in result.criteria} != expected:
            raise RuntimeError(f'{stage} review did not return each assigned criterion exactly once')
        results.append(result)
    by_name = {c.rule: c for result in results for c in result.criteria}
    return Review(criteria=[by_name[n] for n, _ in CRITERIA], difficulty=results[1].difficulty,
                  summary=results[0].summary + '\n' + results[1].summary)


def feedback(rv, failed, hard):
    """What the generator sees when the judge sent a draft back."""
    out = []
    if failed:
        out.append('The judge failed these criteria:')
        out += [f'- {c.rule}: {c.evidence} Fix: {c.fix}' for c in rv.criteria if c.rule in failed]
    if not hard and failed:
        d = rv.difficulty
        out.append(f'The judge thinks a strong agent passes about {d.p_pass_per_attempt:.0%} of the time. Easiest route: '
                   f'{d.easiest_route} Make the targeted trade-off bind: {d.harder_if or "narrow the window"}. Change the '
                   'engineering, not the wording.')
    return '\n'.join(out)
