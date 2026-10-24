"""Publish reviewed task folders to Drive and a compact Sheets index. Dry-run by default."""
import argparse
import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import quote

from evidence import draft_digest
from pipeline_lock import acquire_writer_lock

FOLDER = '1geic-RSeJ8oaNEY7zgOejoOA6qcif-hl'
SHEET = '1lRXKnlk-gXIZmtkpIK0q7-wxBGUjIu1fmlTshOn8GVk'
HEADERS = ['Task ID', 'Task', 'Description', 'Domain', 'Stage 1', 'Feasibility',
           'Difficulty / value', 'Main concern', 'Novelty', 'Task folder']
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']


def read(path):
    return json.loads(path.read_text())


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    tmp.replace(path)


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


class Google:
    def __init__(self, credentials=None):
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise RuntimeError('Install publishing dependencies: uv sync --extra publish') from exc
        # User ADC or a service account supplied through GOOGLE_APPLICATION_CREDENTIALS.
        creds = Credentials.from_authorized_user_file(credentials, SCOPES) if credentials else google.auth.default(scopes=SCOPES)[0]
        self.session = AuthorizedSession(creds)

    def request(self, method, url, **kw):
        for attempt in range(5):
            r = self.session.request(method, url, timeout=90, **kw)
            if r.status_code < 400: return r.json() if r.content else {}
            # Never blindly retry creates: a timed-out create may have succeeded.
            if method != 'POST' and r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(min(2**attempt, 16)); continue
            raise RuntimeError(f'Google {method} failed ({r.status_code}): {r.text[:500]}')
        raise RuntimeError('Google request retry limit')

    def child(self, parent, key, name, folder=False, path=None):
        escaped = lambda s: s.replace('\\', '\\\\').replace("'", "\\'")
        found = self.request('GET', 'https://www.googleapis.com/drive/v3/files', params={
            'q': f"'{escaped(parent)}' in parents and trashed=false and appProperties has {{ key='taskgenKey' and value='{escaped(key)}' }}",
            'fields': 'files(id,name,webViewLink)', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'})['files']
        if len(found)>1: raise RuntimeError('Duplicate remote publication key; inspect Drive before retrying')
        if folder and found: return found[0]['id']
        metadata = {'name': name, 'appProperties': {'taskgenKey':key}}
        if not found: metadata['parents'] = [parent]
        if folder:
            metadata['mimeType'] = 'application/vnd.google-apps.folder'
            return self.request('POST', 'https://www.googleapis.com/drive/v3/files', params={'fields':'id','supportsAllDrives':'true'}, json=metadata)['id']
        boundary = 'taskgen_' + hashlib.sha256(key.encode()).hexdigest()
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n' + json.dumps(metadata) +
                f'\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n').encode() + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        url = 'https://www.googleapis.com/upload/drive/v3/files' + ('/'+found[0]['id'] if found else '')
        return self.request('PATCH' if found else 'POST', url, params={'uploadType':'multipart','fields':'id','supportsAllDrives':'true'},
                            headers={'Content-Type':f'multipart/related; boundary={boundary}'}, data=body)['id']

    def values(self, sheet, tab, method='GET', rows=None, region='A:J'):
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{sheet}/values/' + quote("'"+tab.replace("'","''")+"'!"+region, safe='')
        return self.request(method, url, **({'params':{'valueInputOption':'RAW'}, 'json':{'values':rows}} if rows is not None else {}))

    def preflight(self, folder, sheet, tab):
        meta = self.request('GET', f'https://www.googleapis.com/drive/v3/files/{folder}', params={'fields':'id,mimeType,capabilities(canAddChildren)','supportsAllDrives':'true'})
        if meta.get('mimeType') != 'application/vnd.google-apps.folder' or not meta.get('capabilities', {}).get('canAddChildren'):
            raise RuntimeError('Destination folder is not writable')
        data = self.request('GET', f'https://sheets.googleapis.com/v4/spreadsheets/{sheet}', params={'fields':'sheets(properties)'})
        tabs = [s['properties'] for s in data['sheets']]
        target = next((t for t in tabs if t['title']==tab), None) if tab else next((t for t in tabs if t['sheetId']==0), None)
        if target is None: raise RuntimeError('Destination worksheet not found; specify --tab')
        tab = target['title']
        rows = self.values(sheet, tab).get('values', [])
        if rows and rows[0] != HEADERS: raise RuntimeError('Worksheet headers differ; use a dedicated empty --tab')
        return tab, rows


def publish(api, items, state_path, folder, sheet, tab=None):
    tab, rows = api.preflight(folder, sheet, tab)
    if not rows:
        api.values(sheet, tab, 'PUT', [HEADERS], 'A1:J1'); rows=[HEADERS]
    ids = [r[0] for r in rows[1:] if r]
    if len(ids)!=len(set(ids)): raise RuntimeError('Duplicate task IDs in worksheet')
    state = read(state_path) if state_path.exists() else {}
    for item in items:
        record = state.setdefault(item['id'], {})
        remote = api.child(folder, item['id'], Path(item['directory']).name, folder=True)
        parents = {'':remote}
        for path in item['files']:
            rel = path.relative_to(item['directory'])
            for parent in reversed(rel.parents):
                key = parent.as_posix()
                if key=='.': continue
                if key not in parents:
                    ancestor = '' if parent.parent.as_posix()=='.' else parent.parent.as_posix()
                    parents[key] = api.child(parents[ancestor], item['id']+'/'+key, parent.name, folder=True)
            parent = '' if rel.parent.as_posix()=='.' else rel.parent.as_posix()
            api.child(parents[parent], item['id']+'/'+rel.as_posix(), path.name, path=path)
        record.update(folder_id=remote, digest=item['digest'], status='uploaded')
        atomic(state_path, state)
        row = item['row'][:-1] + ['https://drive.google.com/drive/folders/'+remote]
        index = next((i for i,r in enumerate(rows) if r and r[0]==item['id']), len(rows))
        api.values(sheet, tab, 'PUT', [row], f'A{index+1}:J{index+1}')
        if index==len(rows): rows.append(row)
        else: rows[index]=row
        record.update(status='published', spreadsheet=sheet, tab=tab, row=index+1)
        atomic(state_path, state)
        print('Published:', item['id'], flush=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('run', type=Path)
    ap.add_argument('--selection', type=Path, help='Batch novelty selection JSON with selected IDs and draft_digests')
    ap.add_argument('--folder', default=FOLDER); ap.add_argument('--spreadsheet', default=SHEET)
    ap.add_argument('--tab', help='Existing dedicated worksheet; defaults to sheetId=0')
    ap.add_argument('--credentials', help='Authorized-user OAuth JSON; otherwise Google Application Default Credentials')
    ap.add_argument('--apply', action='store_true', help='Upload and update the spreadsheet; default only plans locally')
    args=ap.parse_args()
    selection=read(args.selection) if args.selection else None
    items=[]; skipped=[]
    for directory in sorted((args.run/'tasks').iterdir()):
        if not directory.is_dir(): continue
        try: items.append(plan(directory, selection))
        except (ValueError, KeyError, FileNotFoundError) as exc: skipped.append({'task':directory.name,'reason':str(exc)})
    print(json.dumps({'publish':[{'id':x['id'],'files':len(x['files']),'row':x['row']} for x in items], 'skipped':skipped}, indent=2))
    if not args.apply or not items: return
    # One publisher per checkout; generation continues independently.
    lock=acquire_writer_lock(Path('.publish-drive.lock'))
    try: publish(Google(args.credentials), items, args.run/'publication.json', args.folder, args.spreadsheet, args.tab)
    finally: lock.close()


if __name__=='__main__': main()
