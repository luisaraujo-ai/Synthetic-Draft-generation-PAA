"""Mechanical checks of a draft against guidelines/04_generation_rules.md (rules 1-5 and 9).

The generator is told the rules; this enforces them. Each check returns a readable violation, and sample.py
regenerates the draft with the list. Tool names and the word limit come from domain.json, so other domains list
their own tools.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from domain import CANARY

BENCHMARK = r'\b(benchmark|grader|grading|reward|sandbox|container|docker|AI agent|agent|run budget|this task|the task)\b'
PREFERENCE = (r'\bas (low|high|small|large|fast|short|long|little|much|few|cheap|quiet) as (possible|you can)\b',
              r'\b(lower|higher|smaller|larger|less|more)\b[^.\n]{0,40}\bwins\b', r'\bwins\b', r'\bwelcome\b',
              r'\bideally\b', r'\bpreferabl[ey]\b', r'\bminimi[sz]\w*', r'\bmaximi[sz]\w*', r'\bthe best\b',
              r'\bthe (lower|higher|smaller|larger) the better\b', r'\bmore margin\b', r'\bwhat I care about most\b')
SOLVER_SETTINGS = r'\b(trapezoidal|gear integration|max(imum)? (time ?)?step|time ?step|reltol|abstol|vntol|' \
                  r'\.options|\.tran\b|\.meas|integration method|solver settings|mesh size)'
FILE_EXT = r'\b[\w-]+\.(?:lib|cir|spice|sp|sub|mod|model|step|stp|stl|json|csv|txt|yaml|yml|py|dat|s\dp|' \
           r'kicad_sch|kicad_pcb|gds|lef|def|v|xml|dxf)\b'
SPICE_EXT = ('.lib', '.cir', '.spice', '.sp', '.sub', '.mod', '.model')
DEVICE_EXT = SPICE_EXT + ('.kicad_sym', '.kicad_mod', '.step', '.stp')  # models and symbols of parts
BENCH_ASSET = r'(bench|harness|testbench|\btb\b|stimulus|probe|\.meas|measure\w* script)'


def words(text):
    return len(re.findall(r'\S+', text))


def strip_canary(text):
    return '\n'.join(l for l in text.splitlines() if CANARY not in l).strip() + '\n'


def find(pattern, text):
    return sorted({m.group(0) for m in re.finditer(pattern, text, re.I)})


def check(draft, domain):
    """Violations of rules 1-5 in a Draft (pydantic model or dict); empty when clean."""
    d = draft if isinstance(draft, dict) else draft.model_dump()
    brief, handbook = d['instruction_md'], d['handbook_md']
    out = []
    limit = domain.get('brief_max_words', 400)
    if words(brief) > limit:
        out.append(f'rule 4: the brief has {words(brief)} words; the limit is {limit}. Cut at least '
                   f'{words(brief) - limit} words: move definitions, rationale and procedure to the handbook.')
    for name, text, maximum in (
        ('handbook', handbook, domain.get('handbook_max_words', 1000)),
        ('parts catalogue', d['parts_catalogue_md'], domain.get('parts_catalogue_max_words', 500)),
        ('reference outline', d['reference_outline'], domain.get('reference_outline_max_words', 400)),
    ):
        count = words(text)
        if count > maximum:
            out.append(f'output size: the {name} has {count} words; the limit is {maximum}. Remove repetition and '
                       'keep only information needed for this task.')
    total = words(json.dumps(d, ensure_ascii=False))
    maximum = domain.get('draft_max_words', 6500)
    if total > maximum:
        out.append(f'output size: the complete draft has {total} words; the limit is {maximum}. Make every field '
                   'concise and remove repetition.')
    asset_chars = sum(len(a.get('content', '')) for a in d['provided_assets'] if a.get('status') == 'generated')
    maximum = domain.get('generated_assets_max_chars', 12000)
    if asset_chars > maximum:
        out.append(f'output size: generated assets contain {asset_chars} characters; the limit is {maximum}. Keep '
                   'generated models small or use an available inventory asset.')
    tools = [t for t in domain.get('tool_names', []) if re.search(rf'\b{re.escape(t)}\b', brief, re.I)]
    if tools:
        out.append(f'rule 1: the brief names software or tools: {", ".join(tools)}. Name the deliverable format only.')
    hb_tools = [t for t in domain.get('tool_names', []) if re.search(rf'\b{re.escape(t)}\b', handbook, re.I)]
    if hb_tools:
        out.append(f'rule 1: the handbook names software or tools: {", ".join(hb_tools)}.')
    if s := find(SOLVER_SETTINGS, brief):
        out.append(f'rule 1: the brief gives solver settings: {", ".join(s)}. State a definition only where it '
                   'changes pass or fail, never a procedure.')
    bench = [a['path'] for a in d['provided_assets'] if re.search(BENCH_ASSET, a['path'] + ' ' + a['purpose'], re.I)]
    if bench:
        out.append(f'rule 2: supplied assets include a test bench or measurement setup: {", ".join(bench)}. Supply '
                   'only models of fixed external parts (and the failing design for a repair task).')
    if p := sorted({m for pat in PREFERENCE for m in find(pat, brief)}):
        out.append(f'rule 3: ungraded preference wording in the brief: {", ".join(p)}. Make it a numbered limit or '
                   'delete it.')
    for name, text in (('brief', brief), ('handbook', handbook)):
        if b := find(BENCHMARK, text):
            out.append(f'rule 4: the {name} mentions the benchmark setting: {", ".join(b)}.')
    out += artifacts(d, domain)
    graded_paths = ' '.join(c['method'] for c in d['grader_checks'])
    unread = [o['path'] for o in d['outputs'] if o['path'].rsplit('/', 1)[-1] not in graded_paths]
    if unread:
        out.append(f'rule 5: no grader check reads these outputs: {", ".join(unread)}. Either add a grader check '
                   'whose method names each file, or remove the file from outputs.')
    return out


def artifacts(d, domain):
    """Rule 9: everything the solver is told about exists, either in the image (asset_inventory) or as a file the
    draft writes out in full; a repair task's failing design is one of them."""
    out = []
    inventory = {a['path'].rstrip('/') for a in domain.get('asset_inventory', [])}
    root = domain.get('input_root', '/workspace/input_assets').rstrip('/')
    file_ref = rf'({re.escape(root)}/[\w./-]+|{FILE_EXT})'
    known, names = [], set()
    for a in d['provided_assets']:
        path = a['path'].rstrip('/')
        if a.get('status') == 'generated' and not a.get('content', '').strip():
            out.append(f'rule 9: {path} is marked generated but its content is empty. Write the full file or drop it.')
        elif a.get('status') != 'generated' and not any(path == i or path.startswith(i + '/') for i in inventory):
            out.append(f'rule 9: {path} is not in the asset inventory and not generated. Generate it in full, or make '
                       'the task self-contained without it.')
        else:
            known.append(path)
            names.add(path.rsplit('/', 1)[-1])
    names |= {o['path'].rsplit('/', 1)[-1] for o in d['outputs']}
    for where, text in (('brief', d['instruction_md']), ('handbook', d['handbook_md'])):
        for ref in sorted({m.group(0).rstrip('.') for m in re.finditer(file_ref, text)}):
            if ref.startswith(root + '/'):
                ok = any(ref.rstrip('/') == k or ref.startswith(k + '/') or k.startswith(ref.rstrip('/') + '/')
                         for k in known)
            else:
                ok = ref in names or any(k.endswith('/' + ref) for k in known)
            if not ok:
                out.append(f'rule 9: the {where} references {ref}, which no available or generated asset provides.')
    allowed = inventory | set(known) | {o['path'].rstrip('/') for o in d['outputs']}
    named = d['instruction_md'] + ' ' + d['handbook_md']
    for p in sorted({m.group(0).rstrip('.,;)') for m in re.finditer(r'/app/[\w./-]+', named)}):
        if not any(p == a or p.startswith(a + '/') or a.startswith(p + '/') for a in allowed):
            out.append(f'rule 9: the brief or handbook names {p}, which is not in the asset inventory, the outputs or '
                       'the generated assets. Remove the reference or make it a generated asset.')
    out += validate_spice(d)
    return out


def validate_spice(d):
    """Parse each generated SPICE asset with ngspice when it is installed (skipped otherwise)."""
    ngspice = shutil.which('ngspice')
    if not ngspice:
        return []
    out = []
    gen = [a for a in d['provided_assets'] if a.get('status') == 'generated' and a['path'].endswith(SPICE_EXT)]
    for a in gen:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / Path(a['path']).name
            f.write_text(a['content'])
            deck = Path(tmp) / 'parse.cir'
            deck.write_text(f'* parse check\n.include {f}\n.end\n')
            r = subprocess.run([ngspice, '-b', str(deck)], capture_output=True, text=True, timeout=60)
            errs = [l for l in (r.stdout + r.stderr).splitlines() if re.search(r'error|unknown|not found', l, re.I)]
            if r.returncode or errs:
                out.append(f"rule 9: generated {a['path']} does not parse in ngspice: {'; '.join(errs[:3]) or r.returncode}")
    return out
