"""Task + handbook generation: sample -> concept -> generate -> judge. Nothing else.

  uv run mine.py --domain electrical                       # once: limitations.json from evaluated trials
  uv run run.py --n 10                                     # sample 10 candidates
  uv run run.py --subdomain "power electronics" --n 10     # fix the subdomain
  uv run run.py --list                                     # the subdomains
  uv run run.py --skillcheck runs/<id>                     # optional: SKILL.md stage 1 screen of a run's tasks
  uv run run.py --resume runs/<id>                         # continue an interrupted run: finished candidates are kept

Per candidate: cell -> concept -> draft -> mechanical lint -> judge (guidelines + standalone + difficulty). A draft the
lint or judge sends back is regenerated with the reasons (--retries), also when it passes the guidelines but looks too
easy. An accepted task is written to runs/<id>/tasks/<slug>/ (instruction.md, handbook.md, inputs/, draft.json,
judge.json): that is what you take to the SKILL.md check. Everything sent back is kept in runs/<id>/rejected/.
A run keeps its settings in run.json, each sampled cell in picks/ and each finished candidate in entries/, so --resume
redoes only the candidates that did not finish (interrupted, or the judge gave no answer), on the same cells.
Needs ANTHROPIC_API_KEY (generator, if a claude-* model) and OPENROUTER_API_KEY or OR_API_KEY (concept, judge).
"""
import argparse
import json
import re
import time
from pathlib import Path

import adapter
import judge
import lint
import llm
import sample
from domain import HERE, Domain


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False))


def timed(label, call):
    """Run one model call, announcing it first and its duration after (the OpenRouter calls are not streamed)."""
    print(f'    {label} ...', flush=True)
    started = time.monotonic()
    result = call()
    print(f'    {label}: {time.monotonic() - started:.0f}s', flush=True)
    return result


def retry_note(d, why):
    return (f'\nYour previous attempt "{d.title}" was sent back:\n{why}\n\nYour previous brief, to edit rather than '
            f'rewrite from scratch:\n<previous_brief>\n{d.instruction_md}\n</previous_brief>\nFix every point and '
            'keep the engineering that was sound. Do not claim anything exists that the brief or your files do not '
            'hold in full.\n')


def write_task(run, domain, d, meta, rv, p):
    out = run / 'tasks' / d.slug
    save(out / 'instruction.md', d.instruction_md)
    save(out / 'handbook.md', d.handbook_md)
    for a in d.provided_assets:
        save(out / 'inputs' / Path(a.path).name, a.content)
    save(out / 'draft.json', {'domain': domain.name, **meta, 'draft': d.model_dump()})
    save(out / 'judge.json', {'p_pass_per_attempt': p, **rv.model_dump()})
    return out


def candidate(domain, sampler, i, a, run, ledger, generator_model):
    pick = run / 'picks' / f'{i:02d}.json'
    if pick.exists():  # a resumed candidate keeps the cell it was first given
        c = json.loads(pick.read_text())
        sub, lim, family, desc, prof = c['subdomain'], next(l for l in sampler.lims if l['id'] == c['limitation']), \
            c['family'], c['descriptors'], c['profile']
    else:
        sub, lim, family, desc, prof = sampler.pick(i, a.subdomain)
        save(pick, {'subdomain': sub, 'limitation': lim['id'], 'family': family, 'descriptors': desc, 'profile': prof})
    started = time.monotonic()
    cell = adapter.cell_text(sample.abstract(lim), sub, family, desc, prof)
    previous = [e['title'] for e in ledger if e['subdomain'] == sub][-12:]
    print(f'[{i + 1}/{a.n}] {sub} / {family} / {lim["id"]}: concept with {llm.ADAPTER_MODEL}', flush=True)
    print(f'  cell: {desc}', flush=True)
    concept = timed('concept', lambda: adapter.adapt(domain, cell, previous))
    print(f'  concept: {concept.device} | bites where: {concept.where_limitation_bites[:160]}', flush=True)
    meta = {'subdomain': sub, 'limitation': lim['id'], 'family': family, 'descriptors': desc, 'profile': prof,
            'concept': concept.model_dump(), 'generator_model': generator_model, 'judge_model': llm.REVIEW_MODEL}
    entry = {'title': None, 'subdomain': sub, 'limitation': lim['id'], 'descriptors': desc, 'status': 'rejected',
             'run': run.name}
    retry, easy = '', None
    for attempt in range(a.retries + 1):
        print(f'  attempt {attempt + 1}/{a.retries + 1}: generating with {generator_model}', flush=True)
        system, prompt = sample.system_prompt(domain), sample.user_prompt(domain, cell, concept, retry, previous)
        save(run / 'prompts' / f'{i:02d}-attempt-{attempt + 1}.json',
             llm.generation_request_record(system, prompt, sample.Draft, generator_model))
        reject = run / 'rejected' / f'{i:02d}-attempt-{attempt + 1}.json'
        try:
            d = llm.generate_parse(system, prompt, sample.Draft, model=generator_model)
        except Exception as exc:  # slow, truncated, invalid or dropped generation (timeout, bad JSON, API error)
            print(f'  generation failed: {type(exc).__name__}: {str(exc)[:300]}', flush=True)
            save(reject, {'stage': 'generation', 'error': f'{type(exc).__name__}: {exc}'})
            retry = ('\nYour previous attempt did not finish or was cut off. Write a more compact draft: shorter '
                     'handbook, fewer input files, one line per list item.\n') if attempt else retry
            continue
        d.slug = f'{i:02d}-{d.slug}'[:60]
        d.instruction_md, d.handbook_md = lint.strip_canary(d.instruction_md), lint.strip_canary(d.handbook_md)
        entry['title'] = d.title
        print(f'  draft: "{d.title}" | brief {lint.words(d.instruction_md)} words, handbook {lint.words(d.handbook_md)}, '
              f'{len(d.requirements)} requirements, {len(d.provided_assets)} input files', flush=True)
        if lint.words(d.instruction_md) > 450:
            print('  advisory: brief exceeds 450 words; aim for 400 and keep within 450. Continuing without a word-count edit.', flush=True)
        problems = polish_style(domain, d, generator_model)
        if problems:  # structural, or style the edit calls could not settle: regenerate
            print(f'  lint: {len(problems)} problem(s), regenerating', flush=True)
            for p in problems:
                print(f'    - {p[:200]}', flush=True)
            save(reject, {'stage': 'lint', 'reasons': problems, 'draft': d.model_dump()})
            retry = retry_note(d, '\n'.join(f'- {p}' for p in problems))
            continue
        for round_ in range(2):  # a second round only after a text-only fix of the brief and handbook
            rv = judge_call(d, lim, domain)
            if rv is None:  # the judge never answered: keep the draft for a later look, do not regenerate blindly
                save(run / 'unjudged' / f'{i:02d}-attempt-{attempt + 1}.json', {'draft': d.model_dump(), 'meta': meta})
                entry['status'] = 'unjudged'
                print('  judge gave no answer: draft saved in unjudged/', flush=True)
                return entry
            ok, failed, hard = judge.passes(rv, domain.get('astra_target', 0.5))
            p = min(max(rv.difficulty.p_pass_per_attempt, 0.0), 1.0)
            print(f'  judge: {"ACCEPT" if ok else "sent back"} (guidelines {"ok" if not failed else "fail: " + ", ".join(failed)}; '
                  f'p_pass={p:.2f}, target <= {domain.get("astra_target", 0.5)})', flush=True)
            for c in rv.criteria:
                if c.verdict != 'pass':
                    print(f'    - {c.rule}: {c.verdict}: {c.evidence[:200]}', flush=True)
            print(f'    judge difficulty: fails at {rv.difficulty.where_it_fails[:200]}', flush=True)
            if ok or round_ or not failed or not hard or not set(failed) <= judge.TEXT_CRITERIA:
                break
            print('  text-only failures: editing the brief and handbook instead of regenerating', flush=True)
            sample.fix_text(domain, d, [f'{c.rule}: {c.evidence} Fix: {c.fix}' for c in rv.criteria
                                        if c.verdict != 'pass'], generator_model)
            if polish_style(domain, d, generator_model):
                break
        if ok:
            out = write_task(run, domain, d, meta, rv, p)
            entry.update(status='accepted', slug=d.slug, p_pass=p)
            sampler.count(lim['id'], sub, desc)
            print(f'  accepted {d.slug} in {time.monotonic() - started:.0f}s -> {out}', flush=True)
            return entry
        save(reject, {'stage': 'judge', 'failed': failed, 'p_pass_per_attempt': p, 'review': rv.model_dump(),
                      'draft': d.model_dump()})
        if not failed:
            easy = (d, meta, rv, p)
        retry = retry_note(d, judge.feedback(rv, failed, hard))
    if easy:  # clean on the guidelines but never hard: keep it for a human to look at, not as an accepted task
        d, meta, rv, p = easy
        write_task(run / 'too_easy', domain, d, meta, rv, p)
        entry.update(status='too_easy', slug=d.slug, p_pass=p)
    print(f'  no accepted task after {a.retries + 1} attempts ({entry["status"]})', flush=True)
    return entry


def judge_call(d, lim, domain):
    """The judge with a capped thinking budget; on a timeout or a bad answer, once more with half the budget.
    None if both fail."""
    for budget in (llm.JUDGE_REASONING_TOKENS, llm.JUDGE_REASONING_TOKENS // 2):
        try:
            return timed(f'judge with {llm.REVIEW_MODEL} (thinking cap {budget} tok, limit {llm.JUDGE_TIMEOUT_S}s)',
                         lambda: judge.review(d.model_dump(), lim, concept_models(domain), reasoning_tokens=budget))
        except (TimeoutError, RuntimeError) as exc:
            print(f'  judge failed with cap {budget}: {type(exc).__name__}: {str(exc)[:200]}', flush=True)
    return None


def polish_style(domain, d, model, rounds=3):
    """Lint the draft; fix wording problems (tools, settings, preferences, benchmark words, length) with edit calls
    on the brief and handbook. Returns what is left: structural problems, or style ones that survived every round."""
    problems = lint.check(d, domain)
    for n in range(rounds):
        style, struct = lint.split(problems)
        if not style or struct:  # nothing to fix, or the draft is regenerated anyway
            return problems
        print(f'  wording: {len(style)} problem(s), edit {n + 1}/{rounds}', flush=True)
        for p in style:
            print(f'    - {p[:200]}', flush=True)
        timed('edit brief/handbook', lambda: sample.fix_text(domain, d, style, model))
        problems = lint.check(d, domain)
    return problems


def merge_cost(path, new):
    """Add this session's cost to what an earlier session of the same run already recorded."""
    if not path.exists():
        return new
    old = json.loads(path.read_text())
    by = {k: dict(v) for k, v in old['by_purpose'].items()}
    for k, v in new['by_purpose'].items():
        b = by.setdefault(k, {f: 0 for f in v})
        for f, x in v.items():
            b[f] = round(b[f] + x, 4) if f == 'usd' else b[f] + x
    return {'total_usd': round(old['total_usd'] + new['total_usd'], 4), 'by_purpose': dict(sorted(by.items())),
            'unpriced_calls': old.get('unpriced_calls', 0) + new['unpriced_calls']}


def concept_models(domain):
    return json.loads(domain.limitations_file.read_text()).get('models', [])


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', default='electrical', help='folder under domains/')
    ap.add_argument('--subdomain', help='fix the subdomain (default: sampled per candidate)')
    ap.add_argument('--list', action='store_true', help='list the subdomains and exit')
    ap.add_argument('--n', type=int, default=10, help='candidates to sample')
    ap.add_argument('--generator-model', default=llm.GENERATOR_MODEL,
                    help='draft writer: claude-* via Anthropic, or provider/model via OpenRouter')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--retries', type=int, default=2, help='regenerations after a lint or judge rejection')
    ap.add_argument('--skillcheck', type=Path, help='run SKILL.md stage 1 on the tasks of this run and exit')
    ap.add_argument('--resume', type=Path, help='continue this run (runs/<id>): redo only the unfinished candidates')
    ap.add_argument('--skill', type=Path, default=HERE / 'SKILL.md')
    a = ap.parse_args()
    if a.resume:
        run = a.resume.resolve()
        if not run.is_dir():
            ap.error(f'no such run: {a.resume}')
        if (run / 'run.json').exists():  # the run's own settings win over the flags
            for k, v in json.loads((run / 'run.json').read_text()).items():
                setattr(a, k, v)
        if getattr(a, 'parallel', False):
            ap.error('Use parallel_run.py --resume for a parallel queue run')
    domain = Domain(a.domain)
    if a.list:
        print('\n'.join(domain.subdomains()))
        raise SystemExit
    if a.skillcheck:
        import idea_review
        dirs = sorted(p for p in (a.skillcheck / 'tasks').iterdir() if (p / 'draft.json').exists())
        records = idea_review.review_drafts(dirs, a.skill)
        idea_review.write_summary(a.skillcheck, records)
        raise SystemExit
    if a.subdomain and a.subdomain not in domain.subdomains():
        ap.error(f'unknown subdomain "{a.subdomain}"; choose one of: {domain.subdomains()}')
    from pipeline_lock import acquire_writer_lock
    _writer_lock = acquire_writer_lock(domain.dir / '.parallel.lock')
    if not a.resume:
        slug = re.sub(r'[^a-z0-9]+', '-', (a.subdomain or 'run').lower()).strip('-')[:30]
        run = HERE / 'runs' / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"
    if not (run / 'run.json').exists():
        save(run / 'run.json', {k: getattr(a, k) for k in ('domain', 'subdomain', 'n', 'generator_model', 'seed', 'retries')})
    done = {}  # index -> entry of every candidate that reached a verdict
    for f in sorted((run / 'entries').glob('*.json')):
        done[int(f.stem)] = json.loads(f.read_text())
    if a.resume and not done and (run / 'summary.json').exists():  # a run from before entries/: summary order = index
        for k, e in enumerate(json.loads((run / 'summary.json').read_text())['candidates']):
            done[k] = e
            save(run / 'entries' / f'{k:02d}.json', e)
    done = {k: e for k, e in done.items() if e['status'] != 'unjudged'}  # the judge never answered: try again
    if a.resume:
        print(f'resuming {run.name}: {len(done)}/{a.n} candidates finished, {a.n - len(done)} to go', flush=True)
    sampler, ledger = sample.Sampler(domain, a.n, a.seed), domain.ledger()
    entries = []
    try:
        for i in range(a.n):
            if i in done:
                entries.append(done[i])
                continue
            entry = candidate(domain, sampler, i, a, run, ledger, a.generator_model)
            save(run / 'entries' / f'{i:02d}.json', entry)
            ledger.append(entry)
            entries.append(entry)
            domain.save_ledger(ledger)
    finally:
        cost = merge_cost(run / 'cost.json', llm.cost_summary()) if a.resume else llm.cost_summary()
        save(run / 'cost.json', cost)
        save(run / 'summary.json', {'candidates': entries, 'cost_usd': cost['total_usd']})
        ok = [e for e in entries if e['status'] == 'accepted']
        print(f"done: {len(ok)} accepted, {sum(e['status'] == 'too_easy' for e in entries)} too easy, "
              f"{len(entries)} sampled; ${cost['total_usd']:.2f}; {run}")
