"""Claude through the official anthropic SDK: streamed calls with a 1-hour cached system prompt, structured output
by constrained decoding, and a validated-JSON fallback for schemas whose grammar the API rejects."""
import anthropic
from pydantic import BaseModel, ValidationError

from taskgen.llm import settings
from taskgen.llm.ledger import log_call
from taskgen.llm.progress import Progress
from taskgen.llm.structured import json_text, schema_system

_client = None


def claude():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


_too_large = set()  # schemas whose grammar the API rejected: go straight to the validated-JSON path


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
    log_call(kwargs['model'], purpose, u.input_tokens, u.output_tokens, getattr(u, 'cache_read_input_tokens', 0) or 0,
         getattr(u, 'cache_creation_input_tokens', 0) or 0)
    if resp.stop_reason == 'refusal':
        raise RuntimeError(f'refused: {getattr(resp.stop_details, "category", None)}')
    if resp.stop_reason == 'max_tokens':
        raise RuntimeError(f'hit max_tokens ({kwargs["max_tokens"]}) before the JSON was complete; '
                           f'used {u.output_tokens} output tokens')
    return resp


def claude_parse(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high',
                 max_tokens: int = 64000, model: str = settings.CLAUDE_MODEL, purpose: str = '',
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


def claude_json(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high', max_tokens: int = 64000,
                repairs: int = 2, model: str = settings.CLAUDE_MODEL, purpose: str = '',
                timeout_s: int | None = None) -> BaseModel:
    """Unconstrained call that must return JSON for schema: the JSON Schema goes in the system prompt, the reply is
    validated with pydantic, and a reply that does not validate is sent back with the errors (append-only history)."""
    system = schema_system(system, schema)
    messages = [{'role': 'user', 'content': prompt}]
    for attempt in range(repairs + 1):
        resp = _stream(purpose=purpose, timeout_s=timeout_s, model=model, max_tokens=max_tokens, system=system,
                       output_config={'effort': effort},
                       messages=messages)
        try:
            return schema.model_validate_json(json_text(resp))
        except ValidationError as e:
            if attempt == repairs:
                raise RuntimeError(f'no valid {schema.__name__} JSON after {repairs + 1} tries: {e}') from e
            messages += [{'role': 'assistant', 'content': resp.content},
                         {'role': 'user', 'content': f'That JSON does not validate against the schema:\n{e}\n'
                                                     'Send the corrected, complete JSON object only.'}]
