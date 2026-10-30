"""Mechanical checks of a draft against guidelines/04_generation_rules.md (rules 1-5, 9) and the client feedback.

The generator is told the rules; this enforces the ones a regex can. Each check returns a readable violation, and
sample.py regenerates the draft with the list. Tool names and word limits come from domain.json.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from taskgen.domain import CANARY

BENCHMARK = r'\b(benchmark|grader|grading|reward|sandbox|container|docker|AI agent|agent|run budget|this task|the task)\b'
PREFERENCE = (r'\bas (low|high|small|large|fast|short|long|little|much|few|cheap|quiet) as (possible|you can)\b',
              r'\b(lower|higher|smaller|larger|less|more)\b[^.\n]{0,40}\bwins\b', r'\bwins\b', r'\bwelcome\b',
              r'\bideally\b', r'\bpreferabl[ey]\b', r'\bminimi[sz]\w*', r'\bmaximi[sz]\w*', r'\bthe best\b',
              r'\bthe (lower|higher|smaller|larger) the better\b', r'\bmore margin\b', r'\bwhat I care about most\b')
SOLVER_SETTINGS = r'\b(trapezoidal|gear integration|max(imum)? (time ?)?step|time ?step|reltol|abstol|vntol|' \
                  r'\.options|\.tran\b|\.meas|integration method|solver settings|mesh size)'
FILE_EXT = r'\b[\w-]+\.(?:lib|cir|spice|sp|sub|mod|model|step|stp|stl|json|csv|txt|yaml|yml|py|dat|s\dp|' \
           r'kicad_sch|kicad_pcb|kicad_sym|gds|lef|def|v|xml|dxf)\b'
SPICE_EXT = ('.lib', '.cir', '.spice', '.sp', '.sub', '.mod', '.model')
BENCH_ASSET = r'(bench|harness|testbench|\btb\b|stimulus|probe|\.meas|measure\w* script)'


def words(text):
    return len(re.findall(r'\S+', text))


def strip_canary(text):
    return '\n'.join(l for l in text.splitlines() if CANARY not in l).strip() + '\n'


def find(pattern, text):
    return sorted({m.group(0) for m in re.finditer(pattern, text, re.I)})


def check(draft, domain):
    """Violations in a Draft (pydantic model or dict); empty when clean."""
    d = draft if isinstance(draft, dict) else draft.model_dump()
    brief, handbook = d['instruction_md'], d['handbook_md']
    out = []
    for name, text, maximum in (('handbook', handbook, domain.get('handbook_max_words', 1000)),
                                ('reference outline', d['reference_outline'], domain.get('reference_outline_max_words', 400))):
        if words(text) > maximum:
            out.append(f'output size: the {name} has {words(text)} words; the limit is {maximum}.')
    total, maximum = words(json.dumps(d, ensure_ascii=False)), domain.get('draft_max_words', 6000)
    if total > maximum:
        out.append(f'output size: the complete draft has {total} words; the limit is {maximum}. Remove repetition.')
    asset_chars = sum(len(a.get('content', '')) for a in d['provided_assets'])
    maximum = domain.get('generated_assets_max_chars', 12000)
    if asset_chars > maximum:
        out.append(f'output size: the input files hold {asset_chars} characters; the limit is {maximum}.')
    for where, text in (('brief', brief), ('handbook', handbook)):
        tools = [t for t in domain.get('tool_names', []) if re.search(rf'\b{re.escape(t)}\b', text, re.I)]
        if tools:
            out.append(f'rule 1: the {where} names software or tools: {", ".join(tools)}. Name the format only.')
        if b := find(BENCHMARK, text):
            out.append(f'rule 4: the {where} mentions the benchmark setting: {", ".join(b)}.')
    if s := find(SOLVER_SETTINGS, brief):
        out.append(f'rule 1: the brief gives solver settings: {", ".join(s)}.')
    bench = [a['path'] for a in d['provided_assets'] if re.search(BENCH_ASSET, Path(a['path']).name, re.I)]  # by file name: a failing design's purpose text may say stimulus
    if bench:
        out.append(f'rule 2: supplied files include a test bench or measurement setup: {", ".join(bench)}.')
    if p := sorted({m for pat in PREFERENCE for m in find(pat, brief)}):
        out.append(f'rule 3: ungraded preference wording in the brief: {", ".join(p)}. Make it a limit or delete it.')
    out += assets(d)
    graded = ' '.join(c['method'] for c in d['grader_plan'])
    unread = [o['path'] for o in d['outputs'] if o['path'].rsplit('/', 1)[-1] not in graded]
    if unread:
        out.append(f'rule 5: no check in the grader plan reads these outputs: {", ".join(unread)}. Name each file in a '
                   'check method, or remove it from outputs.')
    return out


STYLE = ('rule 1', 'rule 3', 'rule 4', 'output size: the handbook')


def split(problems):
    """(style, structural): style problems live in the brief or handbook wording and an edit call can fix them;
    structural ones (missing files, unread outputs, a bench asset, oversize inputs) need the draft regenerated."""
    style = [p for p in problems if p.startswith(STYLE)]
    return style, [p for p in problems if p not in style]


def assets(d):
    """Rule 9: every file the brief or handbook points at is an output or a file the draft writes out in full."""
    out, names = [], set()
    for a in d['provided_assets']:
        if not a.get('content', '').strip():
            out.append(f"rule 9: {a['path']} has no content. Write the full file or drop it.")
        names.add(a['path'].rsplit('/', 1)[-1])
    names |= {o['path'].rsplit('/', 1)[-1] for o in d['outputs']}
    for where, text in (('brief', d['instruction_md']), ('handbook', d['handbook_md'])):
        for ref in sorted({m.group(0).rstrip('.') for m in re.finditer(FILE_EXT, text)}):
            if ref not in names:
                out.append(f'rule 9: the {where} references {ref}, which is neither an output nor a file the draft '
                           'writes out. Supply it in full, state its content in the brief, or remove the reference.')
    return out + validate_spice(d)


def validate_spice(d):
    """Parse each supplied SPICE file with ngspice when it is installed (skipped otherwise)."""
    ngspice = shutil.which('ngspice')
    if not ngspice:
        return []
    out = []
    for a in (a for a in d['provided_assets'] if a['path'].endswith(SPICE_EXT)):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / Path(a['path']).name
            f.write_text(a['content'])
            deck = Path(tmp) / 'parse.cir'
            deck.write_text(f'* parse check\n.include {f}\n.end\n')
            r = subprocess.run([ngspice, '-b', str(deck)], capture_output=True, text=True, timeout=60)
            errs = [l for l in (r.stdout + r.stderr).splitlines() if re.search(r'error|unknown|not found', l, re.I)]
            if r.returncode or errs:
                out.append(f"rule 9: {a['path']} does not parse in ngspice: {'; '.join(errs[:3]) or r.returncode}")
    return out
