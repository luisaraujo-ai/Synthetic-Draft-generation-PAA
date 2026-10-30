"""Worker processes: one isolated model-call stage each. Workers write only their own artifacts and a receipt."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
from pathlib import Path
import signal
import time
import traceback
import uuid

from taskgen import llm
from taskgen.domain import Domain, guidelines
from taskgen.generation import concepts, lint
from taskgen.generation.prompts import system_prompt, user_prompt
from taskgen.generation.sampler import abstract
from taskgen.generation.schema import Draft
from taskgen.pipeline.steps import concept_models, polish_style
from taskgen.pipeline.storage import atomic
from taskgen.review import judge, novelty


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configure(config):
    for key, attr in [('generator_model', 'GENERATOR_MODEL'), ('judge_model', 'REVIEW_MODEL'),
                      ('adapter_model', 'ADAPTER_MODEL'), ('novelty_model', 'NOVELTY_MODEL')]:
        setattr(llm.settings, attr, config[key])


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
                          'effort':llm.settings.JUDGE_EFFORT if stage=='judge' else llm.settings.GENERATOR_EFFORT,
                          'reasoning_tokens':llm.settings.JUDGE_REASONING_TOKENS,
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
            cell = concepts.cell_text(abstract(limitation), pick['subdomain'], pick['family'], pick['descriptors'], pick['profile'])
            if stage == 'concept':
                avoid = [x['title'] for x in novelty.shortlist({'subdomain':pick['subdomain'], 'cell':cell}, catalog)]
                concept = concepts.adapt(domain, cell + '\n' + data.get('retry', ''), avoid)
                result = {'concept': concept.model_dump()}
            elif stage == 'draft':
                concept = concepts.Concept.model_validate(data['concept'])
                previous = [x['title'] for x in novelty.shortlist({'subdomain':pick['subdomain'], 'concept':data['concept']}, catalog)]
                system, prompt = system_prompt(domain), user_prompt(domain, cell, concept, data.get('retry', ''), previous)
                atomic(workdir / 'request.json', llm.generation_request_record(system, prompt, Draft, config['generator_model']))
                generated = workdir / 'generated.json'
                if generated.exists():
                    saved = json.loads(generated.read_text())
                else:
                    saved = None
                if saved and saved.get('fingerprint') == fingerprint:
                    d = Draft.model_validate(saved['draft'])
                    llm.LEDGER.extend(saved.get('records', []))
                else:
                    d = llm.generate_parse(system, prompt, Draft, model=config['generator_model'])
                    atomic(generated, {'fingerprint':fingerprint, 'draft':d.model_dump(), 'records':list(llm.LEDGER)})
                d.slug = f'{ident:02d}-{d.slug}'[:60]
                d.instruction_md, d.handbook_md = lint.strip_canary(d.instruction_md), lint.strip_canary(d.handbook_md)
                atomic(workdir / 'draft.json', d.model_dump())
                problems = polish_style(domain, d, config['generator_model'])
                atomic(workdir / 'draft.json', d.model_dump())
                result = {'draft': d.model_dump(), 'problems': problems}
            elif stage == 'judge':
                d = Draft.model_validate(data['draft'])
                rv = judge.review(d.model_dump(), limitation, concept_models(domain),
                                  reasoning_tokens=llm.settings.JUDGE_REASONING_TOKENS, checkpoint_dir=workdir / 'stages')
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
