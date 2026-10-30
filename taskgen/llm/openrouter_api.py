"""OpenRouter over urllib: streamed responses with live progress, retries on transient HTTP errors, and a
validated-JSON loop. Requests set data_collection "deny" so only providers that neither store nor train on prompts
serve them: the drafts are unpublished benchmark tasks."""
import json
import os
import time
import urllib.error
import urllib.request

from pydantic import BaseModel, ValidationError

from taskgen.llm import settings
from taskgen.llm.ledger import log_call
from taskgen.llm.progress import Progress
from taskgen.llm.structured import schema_system


def _read_stream(r, label, deadline_s):
    """Read an OpenRouter SSE response into the non-streamed shape, showing live progress."""
    prog, text, usage, finish, error = Progress(label, deadline_s), [], {}, None, None
    for raw in r:
        line = raw.decode(errors='replace').strip()
        if not line.startswith('data:') or line == 'data: [DONE]':
            prog.tick()  # keep-alive comments still advance the clock and the deadline
            continue
        chunk = json.loads(line[5:])
        error = chunk.get('error') or error
        usage = chunk.get('usage') or usage
        for ch in chunk.get('choices') or []:
            delta = ch.get('delta') or {}
            if delta.get('reasoning'):
                prog.add('thinking', delta['reasoning'])
            if delta.get('content'):
                prog.add('answer', delta['content'])
                text.append(delta['content'])
            finish = ch.get('finish_reason') or finish
        prog.tick()
    prog.done()
    if error and not text:
        return {'error': error}
    return {'choices': [{'message': {'content': ''.join(text)}, 'finish_reason': finish}], 'usage': usage}


def _openrouter(model, messages, max_tokens, effort, retries=4, purpose='', request_timeout_s=900,
                reasoning_tokens=None):
    key = os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_API_KEY')
    if not key:
        raise SystemExit('set OPENROUTER_API_KEY (or OR_API_KEY) for the reviewer, or TASKGEN_REVIEW_MODEL=claude-...')
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens,
               'reasoning': {'max_tokens': reasoning_tokens} if reasoning_tokens else {'effort': effort},
               'provider': {'data_collection': 'deny'}, 'usage': {'include': True}, 'stream': True}  # streamed for live progress
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(settings.OPENROUTER_URL, data=body, headers={
            'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'X-Title': 'taskgen_poc'})
        try:
            with urllib.request.urlopen(req, timeout=request_timeout_s) as r:
                out = _read_stream(r, f'{purpose or "model"} ({model})', request_timeout_s)
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f'OpenRouter {e.code}: {e.read().decode(errors="replace")[:500]}') from e
        if 'error' in out:  # errors can also arrive with HTTP 200
            if attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f"OpenRouter error: {out['error']}")
        u = out.get('usage') or {}
        log_call(model, purpose, u.get('prompt_tokens', 0), u.get('completion_tokens', 0),
             (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0), 0, u.get('cost'))
        choice = out['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise RuntimeError(f'{purpose or "model"} call to {model} hit the {max_tokens}-token output limit '
                               'before its response was complete')
        return choice['message'].get('content') or ''
    raise RuntimeError('OpenRouter: no response')


def openrouter_json(system: str, prompt: str, schema: type[BaseModel], model: str, effort: str = 'high',
                    max_tokens: int = 32000, repairs: int = 2, purpose: str = '', transport_retries: int = 4,
                    request_timeout_s: int = 900, reasoning_tokens: int | None = None) -> BaseModel:
    """Same contract as claude_json, over OpenRouter's OpenAI-compatible API: schema in the system prompt, the
    reply validated with pydantic and sent back with the errors when it does not validate."""
    system = schema_system(system, schema)
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]
    for attempt in range(repairs + 1):
        text = _openrouter(model, messages, max_tokens, effort, retries=transport_retries, purpose=purpose,
                           request_timeout_s=request_timeout_s, reasoning_tokens=reasoning_tokens).strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        try:
            return schema.model_validate_json(text[text.find('{'):text.rfind('}') + 1])
        except ValidationError as e:
            if attempt == repairs:
                raise RuntimeError(f'no valid {schema.__name__} JSON from {model} after {repairs + 1} tries: {e}') from e
            messages += [{'role': 'assistant', 'content': text},
                         {'role': 'user', 'content': f'That JSON does not validate against the schema:\n{e}\n'
                                                     'Send the corrected, complete JSON object only.'}]

