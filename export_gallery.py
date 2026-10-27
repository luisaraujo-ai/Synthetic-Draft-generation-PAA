"""Export selected, reviewed tasks into the private Vercel gallery bundle."""
import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from publish_drive import plan, read, atomic
from pipeline_lock import coordinator_lock


def export(runs, destination):
    items=[];skipped=[]
    for run in runs:
        manifest=run/'novelty_selection.json'
        selection=read(manifest) if manifest.exists() else None
        for directory in sorted((run/'tasks').iterdir()):
            if not directory.is_dir():continue
            try:items.append(plan(directory,selection))
            except (ValueError,KeyError,FileNotFoundError) as exc:skipped.append({'task':str(directory),'reason':str(exc)})
    if not items: raise ValueError('No eligible tasks; run Stage 1 and batch_novelty.py first. Existing gallery unchanged.')
    catalog=[]
    destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        tmp=Path(tmp)
        for item in items:
            directory=Path(item['directory']);meta=read(directory/'draft.json');review=read(directory/'stage1_review.json')
            key=hashlib.sha256(item['id'].encode()).hexdigest()[:20]
            with zipfile.ZipFile(tmp/f'{key}.zip','w',zipfile.ZIP_DEFLATED) as bundle:
                for file in item['files']:bundle.write(file,file.relative_to(directory).as_posix())
            if (tmp/f'{key}.zip').stat().st_size>4_000_000:raise ValueError(f'{directory}: ZIP exceeds 4 MB download budget')
            row=item['row']
            catalog.append({'key':key,'id':item['id'],'title':row[1],'description':row[2], 'domain':row[3],
                            'bundle_digest':hashlib.sha256((tmp/f'{key}.zip').read_bytes()).hexdigest(),
                            'rank':review['rank'],'feasibility':row[5],'difficulty':row[6],'concern':row[7],
                            'brief':(directory/'instruction.md').read_text(),
                            'handbook':(directory/'handbook.md').read_text() if (directory/'handbook.md').exists() else '',
                            'review':review.get('report_md','')})
        catalog.sort(key=lambda t:(-t['rank'],t['title']))
        for file in tmp.iterdir():file.replace(destination/file.name)
        atomic(destination/'catalog.json',{'tasks':catalog})
    print(json.dumps({'exported':len(catalog),'destination':str(destination),'skipped':skipped},indent=2))
    return catalog

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs',type=Path,nargs='+')
    ap.add_argument('--output',type=Path,default=Path('task-gallery/gallery_data'))
    args=ap.parse_args()
    with coordinator_lock(Path('.gallery-export.lock')):export(args.runs,args.output)
