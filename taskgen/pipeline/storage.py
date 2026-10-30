"""Run-folder persistence: JSON files, atomic writes, cost merging and the accepted-task folder layout."""
import json
import os
import uuid
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False))


def atomic(path, value):
    """Write JSON through a unique temp file so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def merge_cost(path, new):
    """Add this session's cost to what an earlier session of the same run already recorded."""
    if not path.exists():
        return new
    old = read_json(path)
    by = {k: dict(v) for k, v in old['by_purpose'].items()}
    for k, v in new['by_purpose'].items():
        b = by.setdefault(k, {f: 0 for f in v})
        for f, x in v.items():
            b[f] = round(b[f] + x, 4) if f == 'usd' else b[f] + x
    return {'total_usd': round(old['total_usd'] + new['total_usd'], 4), 'by_purpose': dict(sorted(by.items())),
            'unpriced_calls': old.get('unpriced_calls', 0) + new['unpriced_calls']}


def write_task(run, domain, d, meta, rv, p):
    out = run / 'tasks' / d.slug
    save(out / 'instruction.md', d.instruction_md)
    save(out / 'handbook.md', d.handbook_md)
    for a in d.provided_assets:
        save(out / 'inputs' / Path(a.path).name, a.content)
    save(out / 'draft.json', {'domain': domain.name, **meta, 'draft': d.model_dump()})
    save(out / 'judge.json', {'p_pass_per_attempt': p, **rv.model_dump()})
    return out
