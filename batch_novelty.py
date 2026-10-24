"""Rank-first novelty selection across Stage 1-reviewed runs; never generates new drafts."""
import argparse
import hashlib
import json
from pathlib import Path
import llm
import novelty
from evidence import draft_digest
from pipeline_lock import coordinator_lock
from publish_drive import atomic, read


def select(runs, model=None):
    candidates=[]
    for run in runs:
        for directory in sorted((run/'tasks').iterdir()):
            if not (directory/'stage1_review.json').exists(): continue
            meta=read(directory/'draft.json'); draft=meta['draft']; review=read(directory/'stage1_review.json')
            digest=draft_digest(draft)
            if review.get('draft_digest') != digest:
                raise ValueError(f'{directory}: stale Stage 1 review; rerun --skillcheck')
            if review.get('verdict')!='advance' or not review.get('design_decision'): continue
            identifier=run.name+'/'+directory.name
            doc=novelty.document(identifier,draft=draft,subdomain=meta.get('subdomain',''),limitation=meta.get('limitation',''))
            candidates.append((review['rank'],identifier,directory,digest,doc))
    candidates.sort(key=lambda x:(-x[0],x[1]))
    catalog=[]; selected={str(r):{'selected':[],'draft_digests':{}} for r in runs}
    try:
        for rank,identifier,directory,digest,doc in candidates:
            stamp=hashlib.sha256(json.dumps({'candidate':doc,'catalog':catalog,'model':model or llm.NOVELTY_MODEL},sort_keys=True).encode()).hexdigest()
            cache=directory/'batch_novelty.json'
            record=read(cache) if cache.exists() else {}
            if record.get('fingerprint')!=stamp:
                result=novelty.review(doc,catalog,model=model)
                record={'fingerprint':stamp,'draft_digest':digest,'rank':rank,'model':model or llm.NOVELTY_MODEL,**result.model_dump()}
                atomic(cache,record)
            print(f'{identifier}: {record["verdict"]} (Stage 1 rank {rank})',flush=True)
            if record['verdict']=='distinct':
                catalog.append(doc)
                entry=selected[str(directory.parent.parent)]
                entry['selected'].append(directory.name);entry['draft_digests'][directory.name]=digest
        # Publish manifests only after the entire batch completes successfully.
        for run in runs:
            atomic(run/'novelty_selection.json',selected[str(run)])
        print(f'Selected {len(catalog)} of {len(candidates)} Stage 1-advanced tasks.',flush=True)
    finally:
        # Separate records avoid touching generation cost ledgers while it is running.
        costs=llm.cost_summary()
        if costs.get('by_purpose'):
            import time
            atomic(Path('runs')/f'novelty-cost-{time.time_ns()}.json',costs)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs',type=Path,nargs='+')
    ap.add_argument('--model',default=None)
    args=ap.parse_args()
    with coordinator_lock(Path('.batch-novelty.lock')):
        select(args.runs,args.model)
