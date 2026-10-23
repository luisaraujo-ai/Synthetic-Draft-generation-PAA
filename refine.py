"""Stage 2: the evaluator loop. Static review -> (revise) -> executable evaluation -> (revise) ... until ready.

  uv run refine.py runs/<id> --rounds 3 [--probe] [--static-only]

Per round:
1. Static review (judge.py, cheap model): SKILL.md + guidelines/ + an Astra estimate. "not a forge task" stops the
   draft at once (no money spent on revising it). A failing required rule sends the draft to revision.
2. Executable evaluation (evaluate.py), when the rules pass: kforge kit, evaluator build in the image, optional blind
   probe, assessment. Ready = feasible + good run + Astra estimate <= astra_target. Its fitness is offered to the
   archive cell's elite (Rainbow Teaming replacement).
3. Otherwise the revision model (sample.revise: Sonnet at medium effort) rewrites the draft from the findings, the
   rule check runs, and the next round starts.
Every round is kept in rounds/; draft.json, skill_check.json and evaluation.json end as the last version.
--static-only skips step 2 (no Docker) and never marks a draft ready.
"""
import argparse
import json
import shutil
from pathlib import Path

import archive
import evaluate
import judge
import lint
import sample
import sandbox
from domain import HERE, Domain


EVALUATION_PATHS = ('rounds', 'kit', 'eval')
EVALUATION_FILES = ('evaluation.json', 'skill_check.json')


def replace_evaluation_artifacts(draft_dir):
    """Remove results owned by executable evaluation, preserving generation and Stage 1 review files."""
    for name in EVALUATION_PATHS:
        shutil.rmtree(draft_dir / name, ignore_errors=True)
    for name in EVALUATION_FILES:
        (draft_dir / name).unlink(missing_ok=True)


def static_review(draft_dir, skill_text):
    """Write a standalone full-draft review without changing the draft or executable readiness evidence."""
    meta = json.loads((draft_dir / 'draft.json').read_text())
    domain = Domain(meta['domain'])
    draft = meta['draft']
    rv = judge.review(draft, meta.get('limitation'), domain, skill_text, meta.get('catalogue_models', ()))
    record = judge.verdict_record(rv, domain.get('astra_target', 0.5))
    mechanical = lint.check(draft, domain)
    if mechanical:
        record['pass'] = False
        record['failed_rules'] += mechanical
        record['top_fixes'] += mechanical
    record['executable_readiness'] = 'not assessed'
    (draft_dir / 'static_review.json').write_text(json.dumps(record, indent=1, ensure_ascii=False))
    print(f"  {draft_dir.name}: static review {'pass' if record['pass'] else 'fail'}", flush=True)
    return record


def refine(draft_dir, skill_text, rounds=3, probe=False, static_only=False):
    if static_only:
        return static_review(draft_dir, skill_text)
    replace_evaluation_artifacts(draft_dir)
    meta = json.loads((draft_dir / 'draft.json').read_text())
    domain = Domain(meta['domain'])
    target = domain.get('astra_target', 0.5)
    lim, models = meta.get('limitation'), meta.get('catalogue_models', ())
    hist = draft_dir / 'rounds'
    hist.mkdir(exist_ok=True)
    draft, history, ex, ready = meta['draft'], [], None, False
    for r in range(rounds + 1):
        (hist / f'r{r}_draft.json').write_text(json.dumps(draft, indent=1, ensure_ascii=False))
        rv = judge.review(draft, lim, domain, skill_text, models)
        ok, hard_fail = judge.passes(rv, 1.0)  # rules only here; the Astra gate is applied after the build
        static = judge.verdict_record(rv, target)
        mechanical = lint.check(draft, domain)
        if mechanical:
            ok = False
            hard_fail += mechanical
            static['failed_rules'] = hard_fail
            static['top_fixes'] += mechanical
        (hist / f'r{r}_review.json').write_text(json.dumps(static, indent=1, ensure_ascii=False))
        step = {'round': r, 'verdict': rv.verdict, 'failed_rules': hard_fail, 'astra_p': rv.astra.p_pass_per_attempt}
        if rv.verdict == 'not a forge task':
            print(f'  {draft_dir.name} round {r}: not a forge task, stopping ({"; ".join(rv.reasons[:2])})', flush=True)
            history.append({**step, 'pass': False, 'stopped': 'not a forge task'})
            break
        findings = {'static_review': static}
        if ok and not static_only:
            ex = evaluate.evaluate(domain, draft_dir, draft, lim, static, probe=probe)
            (hist / f'r{r}_evaluation.json').write_text(json.dumps(ex, indent=1, ensure_ascii=False, default=str))
            ready = ex['ready']
            step.update(outcome=ex['outcome'], exec_p=ex['p'], fitness=ex['fitness'],
                        reward=(ex.get('informed') or {}).get('reward'))
            if meta.get('archive_id') and ex['fitness'] > 0:
                if archive.offer_elite(domain, meta['archive_id'], ex['fitness']):
                    print(f"    new elite for its cell (fitness {ex['fitness']:.2f})", flush=True)
            findings['evaluation'] = {k: ex.get(k) for k in ('outcome', 'p', 'kit', 'informed', 'probe', 'fixes')}
        else:
            ready = False
        history.append({**step, 'pass': ready})
        print(f"  {draft_dir.name} round {r}: {'READY' if ready else 'not ready'} ({rv.verdict}"
              + (f", failed: {', '.join(hard_fail)}" if hard_fail else '')
              + (f", {ex['outcome']}, reward {step.get('reward')}, Astra p={ex['p']}" if ok and ex and not static_only
                 else f", Astra p={rv.astra.p_pass_per_attempt:.2f}") + ')', flush=True)
        if ready or r == rounds:
            break
        revised, problems = sample.revise(domain, draft, lim, json.dumps(findings, indent=1, ensure_ascii=False,
                                                                         default=str))
        if problems:
            print(f"    revision still breaks the generation rules: {' | '.join(p.split(':')[0] for p in problems)}",
                  flush=True)
        draft, ex = revised.model_dump(), None
    meta.update(draft=draft, refine_history=history, ready=ready)
    (draft_dir / 'draft.json').write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    (draft_dir / 'instruction.md').write_text(draft['instruction_md'])
    (draft_dir / 'handbook.md').write_text(draft['handbook_md'])
    v = {**static, 'pass': ready, 'rounds': history}
    if not ex:
        (draft_dir / 'evaluation.json').unlink(missing_ok=True)
    if ex:
        v['astra'] = {**v['astra'], 'p_pass_per_attempt': ex['p'], 'source': 'assessment after the build'}
        (draft_dir / 'evaluation.json').write_text(json.dumps(ex, indent=1, ensure_ascii=False, default=str))
    (draft_dir / 'skill_check.json').write_text(json.dumps(v, indent=1, ensure_ascii=False))
    if meta.get('archive_id'):
        notes = [] if ready else ([f"stopped: {history[-1].get('stopped')}"] if history[-1].get('stopped') else
                                  [f"not ready after {len(history)} rounds: "
                                   f"{', '.join(history[-1].get('failed_rules') or []) or (ex or {}).get('outcome', 'Astra estimate')}"]
                                  + ((ex or {}).get('fixes') or static['top_fixes'])[:3])
        archive.update(domain, meta['archive_id'], status='judge-pass' if ready else 'judge-fail', notes=notes)
    return v


def refine_drafts(draft_dirs, skill_file=None, rounds=3, probe=False, static_only=False):
    skill = Path(skill_file).read_text() if skill_file and Path(skill_file).exists() else None
    if not static_only and draft_dirs:
        dom = Domain(json.loads((draft_dirs[0] / 'draft.json').read_text())['domain'])
        if not sandbox.available(dom):
            raise RuntimeError(f"Evaluator image {dom['images']['solver']} unavailable; no executable readiness decision. Use --static-only explicitly for review.")
    return {d.name: refine(d, skill, rounds, probe, static_only) for d in draft_dirs}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run', type=Path, help='run directory (with drafts/)')
    ap.add_argument('--rounds', type=int, default=3, help='revisions after the first round')
    ap.add_argument('--probe', action='store_true', help='also run a blind evaluator attempt per evaluated round')
    ap.add_argument('--static-only', action='store_true', help='skip the executable evaluation')
    ap.add_argument('--skill', type=Path, default=HERE / 'SKILL.md')
    a = ap.parse_args()
    dirs = sorted(p for p in (a.run / 'drafts').iterdir() if (p / 'draft.json').exists())
    res = refine_drafts(dirs, a.skill, a.rounds, a.probe, a.static_only)
    print(f"{sum(v['pass'] for v in res.values())}/{len(res)} drafts ready")
