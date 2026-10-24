"""LLM access for the prototype.

Claude (generation): official anthropic SDK, credentials from ANTHROPIC_API_KEY (never stored in the repo). Requests
opt into server-side refusal fallbacks (fallbacks="default"), so a policy decline is retried on a fallback model
inside the same call instead of failing the draft.
Review (judge.py): a cheaper strong model through OpenRouter (default moonshotai/kimi-k3; set TASKGEN_REVIEW_MODEL,
key from OPENROUTER_API_KEY or OR_API_KEY). Requests set data_collection "deny", so only providers that neither store
nor train on prompts serve them: the drafts are unpublished benchmark tasks. A claude-* review model goes through the
Anthropic SDK instead.
Revision (refine.py): Claude Sonnet 5.5 at medium effort (TASKGEN_REVISE_MODEL): about a quarter of an Opus
high-effort revision. First drafts stay on Opus 5.5 at high effort.
Every Claude system prompt carries a 1-hour cache breakpoint: the guidelines (~19k tokens) repeat in every call.
Every call is logged in LEDGER with its tokens and cost; cost_summary() totals them per model and purpose.
"""
import json
import os
import time
import uuid
import urllib.error
import urllib.request

import anthropic
from pydantic import BaseModel, ValidationError

CLAUDE_MODEL = 'claude-sonnet-5-5'  # Opus 5.5 is too expensive for the prototype, and Haiku 5.5 is too small for the draft
GENERATOR_MODEL = os.environ.get('TASKGEN_GENERATOR_MODEL', CLAUDE_MODEL)
REVISE_MODEL = os.environ.get('TASKGEN_REVISE_MODEL', 'claude-sonnet-5-5')
REVISE_EFFORT = os.environ.get('TASKGEN_REVISE_EFFORT', 'medium')
PRICES = {'claude-opus-5-5': (4.0, 20.0, 0.20), 'claude-sonnet-5-5': (2.0, 10.0, 0.20),
          'claude-haiku-5-5': (0.10, 0.50, 0.01)}  # $/M input, output, cache read; a 1 h cache write is 2x input
LEDGER = []
JUDGE_EFFORT = os.environ.get('TASKGEN_JUDGE_EFFORT', 'medium')
JUDGE_REASONING_TOKENS = int(os.environ.get('TASKGEN_JUDGE_REASONING_TOKENS', '3000'))  # cap on the judge's thinking
JUDGE_TIMEOUT_S = int(os.environ.get('TASKGEN_JUDGE_TIMEOUT_S', '600'))  # wall clock per judge call
GENERATOR_EFFORT = os.environ.get('TASKGEN_GENERATOR_EFFORT', 'medium')
GENERATOR_MAX_TOKENS = int(os.environ.get('TASKGEN_GENERATOR_MAX_TOKENS', '32000'))
GENERATOR_TIMEOUT_S = int(os.environ.get('TASKGEN_GENERATOR_TIMEOUT_S', '420'))  # wall clock per generation call
REVIEW_MODEL = os.environ.get('TASKGEN_REVIEW_MODEL', 'xiaomi/mimo-v2.6-pro')
EVALUATOR_MODEL = os.environ.get('TASKGEN_EVALUATOR_MODEL', 'xiaomi/mimo-v2.6-pro')
ADAPTER_MODEL = os.environ.get('TASKGEN_ADAPTER_MODEL', 'claude-sonnet-5-5')
ADAPTER_MAX_TOKENS = int(os.environ.get('TASKGEN_ADAPTER_MAX_TOKENS', '12000'))
NOVELTY_MODEL = os.environ.get('TASKGEN_NOVELTY_MODEL', ADAPTER_MODEL)
INDEX_MODEL = os.environ.get('TASKGEN_INDEX_MODEL', ADAPTER_MODEL)
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
_client = None


def claude():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


_too_large = set()  # schemas whose grammar the API rejected: go straight to the validated-JSON path


def _log(model, purpose, inp, out, cache_read=0, cache_write=0, usd=None):
    if usd is None and model in PRICES:
        pin, pout, pread = PRICES[model]
        usd = (inp * pin + out * pout + cache_read * pread + cache_write * 2 * pin) / 1e6
    LEDGER.append({'call_id': uuid.uuid4().hex, 'model': model, 'purpose': purpose, 'input': inp, 'output': out, 'cache_read': cache_read,
                   'cache_write': cache_write, 'usd': usd})


def cost_summary(records=None):
    records = LEDGER if records is None else records
    by = {}
    for r in records:
        k = f"{r['purpose'] or '-'} ({r['model']})"
        b = by.setdefault(k, {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'usd': 0.0})
        b['calls'] += 1
        for f in ('input', 'output', 'cache_read'):
            b[f] += r[f]
        b['usd'] += r['usd'] or 0.0
    return {'total_usd': round(sum(b['usd'] for b in by.values()), 4),
            'by_purpose': {k: {**v, 'usd': round(v['usd'], 4)} for k, v in sorted(by.items())},
            'unpriced_calls': sum(r['usd'] is None for r in records)}


class Progress:
    """One live status line per streaming call: elapsed, tokens streamed so far (thinking vs answer), deadline."""

    def __init__(self, label, deadline_s):
        self.label, self.deadline_s, self.start = label, deadline_s, time.monotonic()
        self.chars = {'thinking': 0, 'answer': 0}
        self.last = 0.0

    def event(self, ev):
        if ev.type == 'content_block_delta':
            d = ev.delta
            self.add('thinking' if d.type == 'thinking_delta' else 'answer',
                     getattr(d, 'thinking', None) or getattr(d, 'text', None) or getattr(d, 'partial_json', None) or '')
        self.tick()

    def add(self, kind, text):
        self.chars[kind] += len(text or '')

    def tick(self):
        elapsed = time.monotonic() - self.start
        if self.deadline_s and elapsed > self.deadline_s:
            raise TimeoutError(f'{self.label}: no complete answer after {self.deadline_s}s '
                               f'(thinking ~{self.chars["thinking"] // 4} tok, answer ~{self.chars["answer"] // 4} tok)')
        if elapsed - self.last >= 5:
            self.last = elapsed
            print(f'\r    {self.label}: {elapsed:4.0f}s | thinking ~{self.chars["thinking"] // 4} tok | '
                  f'answer ~{self.chars["answer"] // 4} tok | limit {self.deadline_s}s   ', end='', flush=True)

    def done(self):
        print(f'\r    {self.label}: done in {time.monotonic() - self.start:.0f}s | thinking ~{self.chars["thinking"] // 4} '
              f'tok | answer ~{self.chars["answer"] // 4} tok' + ' ' * 20, flush=True)


def _stream(purpose='', timeout_s=None, **kwargs):
    """Streamed, so max_tokens can leave room for thinking plus a long JSON draft without hitting the SDK's
    non-streaming timeout guard. timeout_s is a wall-clock deadline for the whole call (TimeoutError). The system
    prompt gets a 1-hour cache breakpoint: calls minutes apart share the guidelines prefix."""
    if isinstance(kwargs.get('system'), str):
        kwargs['system'] = [{'type': 'text', 'text': kwargs['system'],
                             'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]
    prog = Progress(f'{purpose or "claude"} ({kwargs["model"]})', timeout_s)

    def consume(stream):
        for ev in stream:
            prog.event(ev)
        return stream.get_final_message()
    try:
        with claude().beta.messages.stream(betas=['server-side-fallback-2026-07-01'], fallbacks='default',
                                           **kwargs) as stream:
            resp = consume(stream)
    except (TypeError, AttributeError):  # SDK without the beta stream helper: plain stream, no fallback
        with claude().messages.stream(**kwargs) as stream:
            resp = consume(stream)
    prog.done()
    u = resp.usage
    _log(kwargs['model'], purpose, u.input_tokens, u.output_tokens, getattr(u, 'cache_read_input_tokens', 0) or 0,
         getattr(u, 'cache_creation_input_tokens', 0) or 0)
    if resp.stop_reason == 'refusal':
        raise RuntimeError(f'refused: {getattr(resp.stop_details, "category", None)}')
    if resp.stop_reason == 'max_tokens':
        raise RuntimeError(f'hit max_tokens ({kwargs["max_tokens"]}) before the JSON was complete; '
                           f'used {u.output_tokens} output tokens')
    return resp


def claude_parse(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high',
                 max_tokens: int = 64000, model: str = CLAUDE_MODEL, purpose: str = '',
                 timeout_s: int | None = None) -> BaseModel:
    """One structured call; returns a validated instance of schema.

    Uses constrained decoding (structured outputs). Large schemas (the task draft) exceed the API's grammar limit;
    those fall back to claude_json, and the fallback is remembered per schema."""
    if schema.__name__ not in _too_large:
        try:
            return _stream(purpose=purpose, timeout_s=timeout_s, model=model, max_tokens=max_tokens, system=system,
                           output_format=schema,
                           output_config={'effort': effort},
                           messages=[{'role': 'user', 'content': prompt}]).parsed_output
        except anthropic.BadRequestError as e:
            if 'grammar is too large' not in str(e):
                raise
            _too_large.add(schema.__name__)
    return claude_json(system, prompt, schema, effort, max_tokens, model=model, purpose=purpose,
                       timeout_s=timeout_s)


def _json_text(resp):
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return text[text.find('{'):text.rfind('}') + 1]


def _schema_system(system: str, schema: type[BaseModel]) -> str:
    return (f'{system}\n\nReply with exactly one JSON object and nothing else (no prose, no code fence). It must '
            f'validate against this JSON Schema:\n{json.dumps(schema.model_json_schema())}')


def claude_json(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high', max_tokens: int = 64000,
                repairs: int = 2, model: str = CLAUDE_MODEL, purpose: str = '',
                timeout_s: int | None = None) -> BaseModel:
    """Unconstrained call that must return JSON for schema: the JSON Schema goes in the system prompt, the reply is
    validated with pydantic, and a reply that does not validate is sent back with the errors (append-only history)."""
    system = _schema_system(system, schema)
    messages = [{'role': 'user', 'content': prompt}]
    for attempt in range(repairs + 1):
        resp = _stream(purpose=purpose, timeout_s=timeout_s, model=model, max_tokens=max_tokens, system=system,
                       output_config={'effort': effort},
                       messages=messages)
        try:
            return schema.model_validate_json(_json_text(resp))
        except ValidationError as e:
            if attempt == repairs:
                raise RuntimeError(f'no valid {schema.__name__} JSON after {repairs + 1} tries: {e}') from e
            messages += [{'role': 'assistant', 'content': resp.content},
                         {'role': 'user', 'content': f'That JSON does not validate against the schema:\n{e}\n'
                                                     'Send the corrected, complete JSON object only.'}]


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


def _openrouter(model, messages, max_tokens, effort, retries=4, purpose='', tools=None, request_timeout_s=900,
                reasoning_tokens=None):
    key = os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_API_KEY')
    if not key:
        raise SystemExit('set OPENROUTER_API_KEY (or OR_API_KEY) for the reviewer, or TASKGEN_REVIEW_MODEL=claude-...')
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens,
               'reasoning': {'max_tokens': reasoning_tokens} if reasoning_tokens else {'effort': effort},
               'provider': {'data_collection': 'deny'}, 'usage': {'include': True}}
    if tools:
        payload['tools'] = tools
    else:
        payload['stream'] = True  # live progress; tool loops (legacy) keep the plain response
    body = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
            'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'X-Title': 'taskgen_poc'})
        try:
            with urllib.request.urlopen(req, timeout=request_timeout_s) as r:
                out = _read_stream(r, f'{purpose or "model"} ({model})', request_timeout_s) if not tools \
                    else json.loads(r.read())
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
        _log(model, purpose, u.get('prompt_tokens', 0), u.get('completion_tokens', 0),
             (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0), 0, u.get('cost'))
        choice = out['choices'][0]
        if tools:
            return choice  # tool loops read the whole message (content and tool_calls)
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
    system = _schema_system(system, schema)
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


def generate_parse(system, prompt, schema, model=None):
    """Route draft generation: bare claude-* IDs use Anthropic; provider/model IDs use OpenRouter."""
    model = model or GENERATOR_MODEL
    if model.startswith('claude-'):
        return claude_parse(system, prompt, schema, effort=GENERATOR_EFFORT, max_tokens=GENERATOR_MAX_TOKENS,
                            model=model, purpose='generate', timeout_s=GENERATOR_TIMEOUT_S)
    # OpenRouter reasoning tokens share the response budget. High reasoning caused MiMo to consume 20k tokens
    # without completing the JSON. The concept already contains the engineering reasoning, so the writer only
    # needs low reasoning and a bounded response. Fail promptly instead of retrying a long provider call silently.
    return openrouter_json(system, prompt, schema, model=model, effort='low', max_tokens=20000, repairs=1,
                           transport_retries=0, request_timeout_s=300, purpose='generate')


def generation_request_record(system: str, prompt: str, schema: type[BaseModel], model: str | None = None) -> dict:
    """Serializable, secret-free record of what draft generation sends to the provider."""
    model = model or GENERATOR_MODEL
    schema_json = schema.model_json_schema()
    if model.startswith('claude-'):
        return {
            'provider': 'anthropic', 'model': model, 'purpose': 'generate', 'effort': GENERATOR_EFFORT,
            'max_tokens': GENERATOR_MAX_TOKENS, 'timeout_s': GENERATOR_TIMEOUT_S, 'system': system, 'user_prompt': prompt,
            'output_format': schema_json,
            'fallback_if_schema_is_too_large': {
                'system': _schema_system(system, schema), 'user_prompt': prompt,
            },
        }
    return {
        'provider': 'openrouter', 'model': model, 'purpose': 'generate', 'effort': 'low',
        'max_tokens': 20000, 'request_timeout_s': 300, 'transport_retries': 0, 'json_repairs': 1,
        'messages': [
            {'role': 'system', 'content': _schema_system(system, schema)},
            {'role': 'user', 'content': prompt},
        ],
        'output_schema': schema_json,
        'provider_policy': {'data_collection': 'deny'},
    }


def review_parse(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high',
                 purpose: str = 'review', max_tokens: int = 32000, transport_retries: int = 4,
                 request_timeout_s: int = 900, repairs: int = 2, model: str | None = None,
                 timeout_s: int | None = None, reasoning_tokens: int | None = None) -> BaseModel:
    """Structured model call. Defaults to REVIEW_MODEL; callers for other roles pass their dedicated model."""
    model = model or REVIEW_MODEL
    if model.startswith('claude-'):
        return claude_parse(system, prompt, schema, effort=effort, max_tokens=max_tokens, model=model,
                            purpose=purpose, timeout_s=timeout_s or request_timeout_s)
    return openrouter_json(system, prompt, schema, model=model, effort=effort, max_tokens=max_tokens,
                           repairs=repairs, transport_retries=transport_retries,
                           request_timeout_s=request_timeout_s, purpose=purpose,
                           reasoning_tokens=reasoning_tokens)
