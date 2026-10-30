"""Pure state-transition policy: what a finished stage means for a candidate slot."""
import json

from taskgen.generation.prompts import retry_note
from taskgen.generation.schema import Draft
from taskgen.pipeline.parallel.queue import PENDING
from taskgen.review import judge


def next_attempt(data, config, reason, new_concept=False):
    data['retry'] = reason
    for stage in ('judge', 'concept_novelty', 'final_novelty'):
        data.pop(stage + '_errors', None)
    data['attempt'] += 1
    if data['attempt'] > config['retries'] + 1:
        return 'duplicate' if new_concept else 'rejected'
    if new_concept:
        data['concept_reserved'] = False
        data.pop('concept', None)
    data.pop('draft', None)
    data.pop('review', None)
    return 'concept_pending' if new_concept else 'draft_pending'


def finish(stage, data, receipt, config, domain):
    """Pure transition policy. API faults never ask the generator to replace a judged draft."""
    if not receipt['ok']:
        data['error'] = receipt['error']
        if stage in ('judge', 'concept_novelty', 'final_novelty'):
            key = stage + '_errors'
            data[key] = data.get(key, 0) + 1
            data['resume_state'] = PENDING[stage]
            if data[key] <= config['transport_retries']:
                return PENDING[stage]
            return 'unjudged'
        data['attempt'] += 1
        data['retry'] = 'Previous API call failed to finish. Produce a complete compact response.\n' + data.get('retry','')
        return PENDING[stage] if data['attempt'] <= config['retries']+1 else 'failed'
    data.pop('error', None)
    result = receipt['result']
    if stage == 'concept':
        data['concept'] = result['concept']
        return 'draft_pending' if config.get('skip_novelty') else 'concept_novelty_pending'
    if stage == 'draft':
        data['draft'] = result['draft']
        if result['problems']:
            d = Draft.model_validate(data['draft'])
            return next_attempt(data, config, retry_note(d, '\n'.join(result['problems'])))
        return 'judge_pending'
    if stage == 'judge':
        data['review'] = result['review']
        rv = judge.Review.model_validate(data['review'])
        ok, failed, hard = judge.passes(rv, domain.get('astra_target', .5))
        if ok:
            return 'accepted' if config.get('skip_novelty') else 'final_novelty_pending'
        d = Draft.model_validate(data['draft'])
        reason = retry_note(d, judge.feedback(rv, failed, hard))
        reason += '\nPrevious complete draft (edit the same engineering where sound):\n' + json.dumps(data['draft'])
        return next_attempt(data, config, reason)
    nr = result['novelty']
    data.setdefault('novelty', {})[stage] = nr
    if nr['verdict'] == 'distinct':
        if stage == 'concept_novelty':
            data['concept_reserved'] = True
            return 'draft_pending'
        return 'accepted'
    if nr['verdict'] == 'unclear':
        data['resume_state'] = PENDING[stage]
        return 'unjudged'
    return next_attempt(data, config, 'Engineering duplicate: '+nr['evidence']+'\nRequired difference: '+nr['change_needed'], new_concept=True)
