"""The kforge validator catalogue (kforge/docs/VALIDATORS.json) as the generator and kit builder see it.

compact(domain) is a one-line-per-check list of the families in domain.json "verifier_families", so the generator
plans grader checks with checks that exist. contracts(ids) gives the full contract (parameters, limit, errors,
example) of the checks a draft uses, for the kit builder that writes requirements.yaml.
"""
import json
from functools import lru_cache

from domain import Kforge

INDEX = Kforge / 'kforge' / 'docs' / 'VALIDATORS.json'


@lru_cache(maxsize=1)
def entries():
    return {e['id']: e for e in json.loads(INDEX.read_text())['entries']}


def compact(domain):
    fams = set(domain.get('verifier_families', []))
    rows = [e for e in entries().values() if not fams or e['family'] in fams]
    return '\n'.join(f"- {e['id']}: {' '.join(e['summary'].split())[:170]}" for e in sorted(rows, key=lambda e: e['id']))


def contracts(ids):
    out = []
    for i in ids:
        e = entries().get(i)
        if not e:
            out.append(f'## {i}\n(not a registered check)')
            continue
        c = e['contract']
        out.append(f"## {i}\nsummary: {' '.join(e['summary'].split())}\nparameters:\n{c.get('parameters', '')}\n"
                   f"limit: {c.get('limit', '')}\nerrors: {c.get('errors', '')}\nexample:\n{c.get('example', '')}")
    return '\n\n'.join(out)


def known(check_id):
    return check_id in entries()


def match_task(draft, limit=24):
    """Return ranked local verifier contracts and source for a task draft (no model or network)."""
    import ast
    import re
    words = set(re.findall(r'[a-z][a-z0-9_]{2,}', json.dumps(draft).lower()))
    cited = {c.get('primitive') for c in draft.get('grader_checks', [])}
    matches = []
    for path in sorted((Kforge / 'kforge' / 'kforge' / 'checks').glob('*.py')):
        source = path.read_text()
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                        and dec.func.id == 'primitive' and dec.args and isinstance(dec.args[0], ast.Constant)):
                    continue
                check_id = dec.args[0].value
                doc = ast.get_docstring(node) or ''
                tokens = set(re.findall(r'[a-z][a-z0-9_]{2,}', (check_id + ' ' + doc).lower()))
                score = len(words & tokens) + (10000 if check_id in cited else 0)
                matches.append({'id': check_id, 'score': score, 'contract': doc,
                                'source_path': str(path.relative_to(REPO)),
                                'implementation': ast.get_source_segment(source, node)})
    return sorted(matches, key=lambda m: (-m['score'], m['id']))[:limit]
