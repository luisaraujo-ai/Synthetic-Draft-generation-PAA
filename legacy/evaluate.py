"""Executable evaluation of a draft that passed the static review: kit -> evaluator build (-> probe) -> assessment.

1. kit.build: a kforge grader for the draft, validated in the image.
2. solver.run informed: the cheap evaluator builds the design with the reference outline and the grade tool, aiming
   for a good run (reward 1).
3. solver.run probe (optional, --probe): a blind attempt, as a cheap difficulty signal.
4. Assessment (llm.REVIEW_MODEL): combines the static review, the kit coverage and the runs into an outcome
   (feasible | grader_problem | infeasible | inconclusive), an Astra pass estimate and fixes.
Fitness records validated feasibility only. Difficulty estimates are advisory and remain uncalibrated until
target-model trials exist. Readiness requires a passing build, independent grader tests and no blocking issues.
"""
import json
from typing import List, Literal

from pydantic import BaseModel, Field

import judge
import kit
import llm
import solver
from evidence import draft_digest, physically_ready


class Assessment(BaseModel):
    outcome: Literal['feasible', 'grader_problem', 'infeasible', 'inconclusive']
    p_pass_per_attempt: float = Field(description='probability, 0 to 1, that one Astra attempt passes every check')
    reasons: List[str] = Field(description='what the runs show, with check ids and margins')
    binding_checks: List[str] = Field(description='check ids that were hardest to meet, with why')
    limitation_bites: str = Field(description='does the targeted limitation decide the outcome, and where')
    fixes: List[str] = Field(description='changes to the task, most important first; empty if ready')
    blocking_issues: List[str] = Field(description="Unresolved feasibility, physical-model credibility, missing data, uncalibrated numerical limits, or verifier correctness issues. Empty only when execution evidence resolves them; cite evidence in reasons.")
    sme_notes: List[str] = Field(description='for the SME: what to tighten if Astra passes, what to check first')


SYSTEM = """You assess engineering design tasks for the FAIR Forge benchmark after they were actually built and
graded. You are strict and you reason from the evidence: the grader's per-check results, margins and the build
history, not the author's claims. A check that errors or measures something the brief does not ask for is a grader
problem, not difficulty. An evaluator that passed easily with the reference outline says the task is feasible; it
also says how far the binding checks are from the limits. A blind probe that passes says the task is easy."""

PROMPT = """Task (draft, with the author's plan):
{task}

Targeted limitation: {limitation}

Static review (earlier): verdict {verdict}; Astra estimate {p_static:.2f}; reasons: {reasons}

Grader kit: {kit}

Evaluator build with the reference outline and the grade tool ({turns} tool calls; reward after each grade call:
{history}); final checks:
{checks}
Evaluator's last messages: {tail}
{probe}
Decide the outcome. "feasible" needs a passing design (reward 1) and a grader that measures exactly the brief's
requirements. Then estimate the probability that one Astra attempt (a strong frontier agent, 2 h, 400 turns, no
reference outline, no grade tool) passes every check: work out its most likely route and where it fails. Compare it
with the evaluator: Astra is stronger than the evaluator but works blind."""


def fitness(outcome, reward, p, rules_ok):
    return 1.0 if rules_ok and outcome == 'feasible' and reward == 1 else 0.0


def evaluate(domain, draft_dir, draft, limitation, static, probe=False):
    """Returns a record with outcome, run results, Astra estimate, fitness, ready flag and fixes."""
    k = kit.build(domain, draft_dir, draft)
    rec = {'draft_digest': draft_digest(draft), 'difficulty_status': 'uncalibrated',
           'kit': {key: k[key] for key in ('ok', 'audit_ok', 'coverage', 'uncovered', 'notes', 'problems')}}
    if not k['ok']:
        rec.update(outcome='grader_problem', p=1.0, fitness=0.0, ready=False,
                   fixes=['The grader could not be built from the plan: ' + (k['problems'][0][:600] if k['problems']
                                                                              else 'unknown')] + [k['notes']])
        return rec
    run = solver.run(domain, draft_dir, draft, 'informed')
    rec['informed'] = {key: run[key] for key in ('reward', 'turns', 'grade_history', 'checks', 'submitted', 'timed_out', 'elapsed_s', 'budget_s')}
    if run['timed_out']:
        rec.update(outcome='inconclusive', p=None, fitness=0.0, ready=False,
                   fixes=['Evaluator timed out: feasibility and difficulty remain unestablished.'])
        return rec
    pr = solver.run(domain, draft_dir, draft, 'probe') if probe else None
    if pr:
        rec['probe'] = {key: pr[key] for key in ('reward', 'turns', 'checks')}
    a = llm.review_parse(SYSTEM, PROMPT.format(
        task=judge.draft_text(draft), limitation=f"{limitation['id']}: {limitation['mechanism']}",
        verdict=static['verdict'], p_static=static['astra']['p_pass_per_attempt'], reasons='; '.join(static['reasons']),
        kit=json.dumps({key: k[key] for key in ('coverage', 'notes')}, ensure_ascii=False),
        turns=run['turns'], history=run['grade_history'], checks=run['checks'],
        tail=' | '.join(str(t)[:400] for t in run['transcript_tail'][-4:]),
        probe=(f"\nBlind probe ({pr['turns']} tool calls, no outline, no grade tool): reward {pr['reward']}; checks:\n"
               f"{pr['checks']}\n") if pr else ''), Assessment, purpose='assess')
    p = min(max(a.p_pass_per_attempt, 0.0), 1.0)
    fit = fitness(a.outcome, run['reward'], p, not static['failed_rules'])
    rec.update(outcome=a.outcome, p=p, fitness=fit, assessment=a.model_dump(),
               blocking_issues=a.blocking_issues, fixes=a.fixes + a.blocking_issues)
    rec['ready'] = physically_ready(rec) and not static['failed_rules']
    if not rec['ready']:
        rec['fitness'] = 0.0
    return rec
