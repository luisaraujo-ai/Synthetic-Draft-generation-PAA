"""Kit: turn a draft's grader plan into a runnable kforge grader, and validate it in the image.

The kit builder (llm.REVISE_MODEL, high effort) writes tests/requirements.yaml (kforge-req/1) from the draft, using
library checks from the kforge catalogue (catalog.py), plus any catalogue entries and SPICE models the fixed parts
need. Validation runs in the domain image (sandbox.py):
1. kforge loads the spec (`python -m kforge brief`);
2. a starter project is built from the spec's ports;
3. an empty submission and the bare starter are graded: both must give failing checks and no grader_error.
kforge's errors go back to the builder (up to `repairs` times). Result: <draft>/kit/{tests/, starter/, input_assets/}
and kit.json with the requirement coverage.
"""
import json
import re
import shutil
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

import catalog
import lint
import verifier_audit
import llm
import sandbox
from domain import REPO

CF = REPO / 'kforge' / 'docs' / 'CANONICAL_FORMAT.md'
EXAMPLES = [REPO / 'kforge' / 'examples' / 'catalogue' / 'afe_frontend' / 'requirements.json',
            REPO / 'tasks' / 'lp1-broadband-match' / 'tests' / 'requirements.yaml']


class KitFile(BaseModel):
    path: str = Field(description='path relative to tests/, e.g. catalog/parts.json or models/opamp.lib')
    content: str


class Kit(BaseModel):
    requirements_yaml: str = Field(description='the complete tests/requirements.yaml, schema kforge-req/1')
    files: List[KitFile] = Field(description='catalogue/model files plus optional verifier_extensions.py and test_verifiers.py for new checks')
    coverage: List[str] = Field(description='one line per draft requirement: rid -> kforge requirement ids, or '
                                            'UNCOVERED with the reason')
    notes: str = Field(description='anything the task author must change for the grader to match the brief')


SYSTEM = """You build kforge graders for engineering design tasks. kforge grades delivered artifacts from a
declarative requirements.yaml (kforge-req/1): published ports, a circuit section (reference, passives, catalogue,
scenarios and testbenches) and requirements that each bind one registered check with params, limit and unit. Every
requirement in the task's brief must map to one or more kforge requirements with the same limits and conditions;
nothing the brief does not state may be checked. Use only documented params of each check.

The canonical format (deliverable contract and task contract):
<canonical_format>
{cf}
</canonical_format>

Example specs:
{examples}

Registered checks you may use (id: summary):
{compact}"""

PROMPT = """Write the kforge grader for this task.

<task>
{task}
</task>

Ranked matches from the actual local kforge source (contracts and implementations):
{contracts}

Rules:
- Preserve the task's actual deliverable format. KiCad tasks use integrity.loads and integrity.ports with
  appropriate dependencies. Netlist tasks can use deliverable.require: [] and ports: {{}}; read sanitized files
  through Context.intake_dir. Never force KiCad on a task asking for a netlist.
- Reuse or compose the matched library checks first. If a requirement lacks a check, propose its observable,
  units, governing law, validity range and exclusions in notes, then IMPLEMENT it in verifier_extensions.py
  under tests/ using @primitive("task.<name>") from kforge.checks.registry. Return Outcome measurements;
  the engine applies published limits. Use absolute imports. Do not replace existing primitives.
- Include test_verifiers.py (unittest) for new checks: independent oracle, two valid designs, physical failures,
  boundaries, invariances and guard fault injections. Call the actual extension implementation. These tests run
  inside the offline image before grading. Document Parameters and the enriched physics contract in docstrings.
- Adapt library APIs to the required task interface in these extensions; inspect the supplied implementation.
  Build solver inputs from sanitized deliverables; never execute submitted decks or trust submitted measurements.
  Use kforge solver run-integrity helpers, finite outputs, residual/log checks. DeliverableError means invalid
  delivered data; GraderError means the grader cannot decide. Never widen limits or omit requirements.
- Every task requirement must appear in coverage as RID -> actual YAML requirement ids. UNCOVERED is a build
  failure. If missing authoritative models or data prevent implementation, report the gap instead of fabricating.
- Handbook data and part tolerances are ranges the checks take the worst case over; generic passives get
  circuit.passives entries for the packages the brief allows.
- Fixed active parts: a catalogue entry of model type subckt with its SPICE model file under models/ (set
  circuit.models_dir and catalog_files), matching the generated model the solver also gets.
- Held-out test points must lie inside the declared ranges.
- Keep every simulation small enough that the full grade finishes in minutes.{errors}"""


def _cf_excerpt():
    text = CF.read_text()
    keep = []
    for head in ('## 3. Deliverable contract', '## 5. Task contract'):
        i = text.find(head)
        j = text.find('\n## ', i + 5)
        keep.append(text[i:j if j > 0 else None])
    return '\n\n'.join(keep)


def _task_text(draft):
    import judge
    return judge.draft_text(draft)


def _cited(draft):
    ids = {c.get('primitive', '') for c in draft['grader_checks']}
    ids |= {w.strip('`.,;:()') for c in draft['grader_checks'] for w in c['method'].split() if catalog.known(w.strip('`.,;:()'))}
    return sorted(i for i in ids if i and catalog.known(i)) + ['integrity.loads', 'integrity.ports']


def validate(domain, kit_dir):
    """Load the spec, build the starter, grade empty and starter-only submissions. Returns (problems, info)."""
    problems, info = [], {}
    work = kit_dir / '_validate'
    shutil.rmtree(work, ignore_errors=True)
    with sandbox.Sandbox(domain, work, kit_dir / 'tests') as sb:
        shutil.copytree(kit_dir / 'input_assets', work / 'input_assets', dirs_exist_ok=True)
        if (kit_dir / 'tests' / 'verifier_extensions.py').exists():
            if not (kit_dir / 'tests' / 'test_verifiers.py').exists():
                return ['New verifiers require test_verifiers.py with independent oracle tests'], info
            code, out = sb.exec('cd /tests && python -m unittest -v test_verifiers', timeout=600)
            if code or 'Ran 0 tests' in out:
                return [f'Extension tests failed or empty: {out[-3000:]}'], info
            info['extension_tests'] = out[-3000:]
        code, out = sb.exec('python -m kforge brief --spec /tests/requirements.yaml', timeout=300)
        if code:
            return [f'kforge cannot load requirements.yaml:\n{out[-3000:]}'], info
        info['brief_table'] = out[-6000:]
        code, spec_json = sb.exec("python -c \"import yaml,json; print(json.dumps(yaml.safe_load(open('/tests/requirements.yaml'))))\"", timeout=120)
        try:
            spec = json.loads(spec_json.strip().splitlines()[-1])
            info['requirement_ids'] = [r['id'] for r in spec.get('requirements', [])]
            if not info['requirement_ids']:
                return ['requirements.yaml has no requirements'], info
        except (ValueError, IndexError, KeyError, TypeError):
            return ['Cannot inspect requirements.yaml'], info
        code, ports = sb.exec('python -c "import yaml,json;print(json.dumps(yaml.safe_load('
                              'open(\'/tests/requirements.yaml\'))[\'ports\']))"', timeout=120)
        try:
            ports = json.loads(ports.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return [f'could not read ports from requirements.yaml: {ports[-1000:]}'], info
        if not spec.get('deliverable', {}).get('require', []) and not ports:
            (work / 'starter' / 'design').mkdir(parents=True, exist_ok=True)
            code, out = 0, ''
        else:
            code, out = sb.make_starter(ports)
        if code:
            return [f'starter project could not be built from the ports: {out[-2000:]}'], info
        for label, prep in (('empty submission', 'rm -rf /app/output/*'),
                            ('starter only', 'rm -rf /app/output/* && cp -r /app/starter/design /app/output/')):
            sb.exec(prep, timeout=60)
            code, checks, log = sb.grade()
            if not checks:
                problems.append(f'{label}: kforge grade produced no checks.json:\n{log[-2500:]}')
                continue
            if not checks.get('checks'):
                problems.append(f'{label}: empty grader is invalid')
            errs = [c for c in checks['checks'] if c.get('status') == 'grader_error']
            if errs:
                problems.append(f'{label}: grader_error (a spec fault, not a design fault):\n' + '\n'.join(
                    f"- {c['id']}: {' '.join(str(c.get('detail')).split())[:400]}" for c in errs))
            if checks.get('reward') == 1:
                problems.append(f'{label}: reward 1 without a design. The checks do not test the requirements.')
            info[label] = sandbox.summarize(checks)
        sb.exec('rm -rf /app/output/*', timeout=60)
    shutil.copytree(work / 'starter', kit_dir / 'starter', dirs_exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    return problems, info


def build(domain, draft_dir, draft, repairs=3):
    """Write and validate the kit for a draft. Returns a dict with ok, coverage, notes, problems."""
    kit_dir = draft_dir / 'kit'
    shutil.rmtree(kit_dir, ignore_errors=True)
    (kit_dir / 'tests').mkdir(parents=True)
    system = SYSTEM.format(cf=_cf_excerpt(), compact=catalog.compact(domain),
                           examples='\n\n'.join(f'{p.name}:\n{p.read_text()}' for p in EXAMPLES if p.exists()))
    errors, kit, problems, info = '', None, [], {}
    assets = kit_dir / 'input_assets'
    assets.mkdir()
    asset_problems = lint.artifacts(draft, domain)
    root = domain.get('input_root', '/app/input_assets').rstrip('/') + '/'
    for asset in draft['provided_assets']:
        if asset.get('status') == 'generated':
            path = asset['path']
            if not path.startswith(root) or '..' in Path(path).parts:
                asset_problems.append(f'Invalid input asset path: {path}')
                continue
            dest = assets / path[len(root):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(asset.get('content', ''))
    if asset_problems:
        result = dict(ok=False, audit_ok=False, coverage=[], uncovered=[], notes='Input assets are incomplete',
                      problems=asset_problems, info={})
        (kit_dir / 'kit.json').write_text(json.dumps(result, indent=1))
        return result
    for attempt in range(repairs + 1):
        prompt = PROMPT.format(task=_task_text(draft), contracts=json.dumps(catalog.match_task(draft), ensure_ascii=False), errors=errors)
        kit = llm.claude_parse(system, prompt, Kit, effort='high', model=llm.REVISE_MODEL, purpose='kit')
        shutil.rmtree(kit_dir / 'tests', ignore_errors=True)
        (kit_dir / 'tests').mkdir(parents=True)
        (kit_dir / 'tests' / 'requirements.yaml').write_text(kit.requirements_yaml)
        for f in kit.files:
            p = (kit_dir / 'tests' / f.path).resolve()
            if (kit_dir / 'tests').resolve() not in p.parents:
                continue  # no writes outside the kit
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f.content)
        (kit_dir / 'tests' / 'run_kforge.py').write_text(
            "import runpy, sys\nimport kforge.checks\nfrom pathlib import Path\n"
            "p = Path('/tests/verifier_extensions.py')\n"
            "if p.exists(): runpy.run_path(str(p))\n"
            "from kforge.__main__ import main\nsys.exit(main())\n")
        problems, info = validate(domain, kit_dir)
        for req in draft['requirements']:
            rows = [row for row in kit.coverage if re.match(r'^' + re.escape(req['rid']) + r'\b', row)]
            bound = any(re.search(r'(?<![\w.-])' + re.escape(rid) + r'(?![\w.-])', row.split('->', 1)[-1])
                        for row in rows for rid in info.get('requirement_ids', []))
            if not rows or not bound or any('UNCOVERED' in row.upper() for row in rows):
                problems.append(f"{req['rid']}: missing verifier coverage; implement and bind a check")
        print(f"    kit attempt {attempt}: {'valid' if not problems else 'invalid: ' + problems[0].splitlines()[0]}",
              flush=True)
        if not problems:
            problems += verifier_audit.audit(domain, kit_dir, draft, info.get('requirement_ids', []))
        if not problems:
            break
        errors = ('\n\nYour previous requirements.yaml failed validation in the grader image. Fix it:\n' +
                  '\n'.join(problems) + f'\n\nPrevious requirements.yaml:\n{kit.requirements_yaml}')
    result = {'ok': not problems, 'audit_ok': not problems, 'coverage': kit.coverage, 'notes': kit.notes, 'problems': problems, 'info': info,
              'uncovered': [c for c in kit.coverage if 'UNCOVERED' in c]}
    (kit_dir / 'kit.json').write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result
