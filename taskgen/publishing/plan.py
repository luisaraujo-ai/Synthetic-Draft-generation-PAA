"""The publishable-task plan: which reviewed tasks pass the Stage 1 and novelty gates, and the files and row each exports."""
from taskgen.pipeline.evidence import draft_digest
from taskgen.pipeline.storage import read_json as read


def compact(text, limit=350):
    return ' '.join(str(text or '').split())[:limit]


def section(report, label):
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(label.lower() + ':'):
            value = line.split(':', 1)[1].strip()
            for following in lines[i+1:]:
                if not following.strip():
                    if value: break
                    continue
                if following.strip().endswith(':'): break
                value += ' ' + following.strip().lstrip('-• ')
            return compact(value)
    return ''


def plan(directory, selection=None):
    meta = read(directory/'draft.json')
    draft = meta['draft']
    digest = draft_digest(draft)
    review = read(directory/'stage1_review.json')
    if review.get('draft_digest') != digest:
        raise ValueError('Stage 1 review missing digest or stale; rerun Stage 1')
    if review.get('verdict') != 'advance' or not review.get('design_decision'):
        raise ValueError('Stage 1 did not advance this task')
    if selection is not None:
        if directory.name not in selection.get('selected', []):
            raise ValueError('not selected by batch novelty analysis')
        if selection.get('draft_digests', {}).get(directory.name) != digest:
            raise ValueError('batch selection missing digest or stale')
        novelty = 'Selected by batch novelty analysis'
    else:
        n = read(directory/'novelty.json')
        if n.get('verdict') != 'distinct' or n.get('draft_digest') != digest:
            raise ValueError('need a current distinct novelty review or --selection manifest')
        novelty = compact(n.get('evidence') or 'Distinct')
    files = []
    for name in ['instruction.md', 'handbook.md', 'draft.json', 'judge.json',
                 'stage1_review.json', 'stage1_review.md', 'novelty.json', 'batch_novelty.json']:
        p = directory/name
        if p.is_file(): files.append(p)
    if not (directory/'instruction.md').is_file(): raise ValueError('missing instruction.md')
    if (directory/'inputs').exists(): files.extend(p for p in sorted((directory/'inputs').rglob('*')) if p.is_file())
    for p in files:
        if p.is_symlink() or not p.resolve().is_relative_to(directory.resolve()):
            raise ValueError('asset symlinks outside the task are not publishable')
    report = review.get('report_md', '')
    judge = read(directory/'judge.json') if (directory/'judge.json').exists() else {}
    judge = judge.get('review', judge)
    difficulty = judge.get('difficulty', {})
    feasibility = next((c.get('evidence', '') for c in judge.get('criteria', []) if c.get('rule')=='feasible'), '')
    task_id = directory.parent.parent.name + '/' + directory.name
    row = [task_id, draft['title'], compact(draft.get('instruction_md', '').split('\n\n')[0]),
           compact(meta.get('subdomain') or meta.get('domain')), 'Advance',
           'Judge assessment (not reference validation): ' + compact(feasibility or section(report, 'What stops it'), 260),
           f"Stage 1 value rank {review['rank']}/10; " + compact(difficulty.get('where_it_fails'), 220),
           section(report, 'What stops it') or compact(review.get('main_reason')), novelty, '']
    return dict(id=task_id, digest=digest, directory=str(directory), files=files, row=row)

