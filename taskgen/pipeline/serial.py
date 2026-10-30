"""Serial candidate loop: cell -> concept -> draft -> mechanical lint -> judge, regenerating with the reasons.

A draft the lint or judge sends back is regenerated (retries), also when it passes the guidelines but looks too easy.
Everything sent back is kept in runs/<id>/rejected/. An accepted task is written to runs/<id>/tasks/<slug>/.
"""
import json
import time

from taskgen import llm
from taskgen.generation import concepts, lint
from taskgen.generation.generator import fix_text
from taskgen.generation.prompts import retry_note, system_prompt, user_prompt
from taskgen.generation.sampler import abstract
from taskgen.generation.schema import Draft
from taskgen.pipeline.steps import judge_call, polish_style, timed
from taskgen.pipeline.storage import save, write_task
from taskgen.review import judge


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
    cell = concepts.cell_text(abstract(lim), sub, family, desc, prof)
    previous = [e['title'] for e in ledger if e['subdomain'] == sub][-12:]
    print(f'[{i + 1}/{a.n}] {sub} / {family} / {lim["id"]}: concept with {llm.settings.ADAPTER_MODEL}', flush=True)
    print(f'  cell: {desc}', flush=True)
    concept = timed('concept', lambda: concepts.adapt(domain, cell, previous))
    print(f'  concept: {concept.device} | bites where: {concept.where_limitation_bites[:160]}', flush=True)
    meta = {'subdomain': sub, 'limitation': lim['id'], 'family': family, 'descriptors': desc, 'profile': prof,
            'concept': concept.model_dump(), 'generator_model': generator_model, 'judge_model': llm.settings.REVIEW_MODEL}
    entry = {'title': None, 'subdomain': sub, 'limitation': lim['id'], 'descriptors': desc, 'status': 'rejected',
             'run': run.name}
    retry, easy = '', None
    for attempt in range(a.retries + 1):
        print(f'  attempt {attempt + 1}/{a.retries + 1}: generating with {generator_model}', flush=True)
        system, prompt = system_prompt(domain), user_prompt(domain, cell, concept, retry, previous)
        save(run / 'prompts' / f'{i:02d}-attempt-{attempt + 1}.json',
             llm.generation_request_record(system, prompt, Draft, generator_model))
        reject = run / 'rejected' / f'{i:02d}-attempt-{attempt + 1}.json'
        try:
            d = llm.generate_parse(system, prompt, Draft, model=generator_model)
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
            fix_text(domain, d, [f'{c.rule}: {c.evidence} Fix: {c.fix}' for c in rv.criteria
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
