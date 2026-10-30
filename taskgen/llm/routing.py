"""Provider routing: bare claude-* model IDs go to Anthropic, provider/model IDs to OpenRouter."""
from pydantic import BaseModel

from taskgen.llm import settings
from taskgen.llm.anthropic_api import claude_parse
from taskgen.llm.openrouter_api import openrouter_json
from taskgen.llm.structured import schema_system


def generate_parse(system, prompt, schema, model=None):
    """Route draft generation: bare claude-* IDs use Anthropic; provider/model IDs use OpenRouter."""
    model = model or settings.GENERATOR_MODEL
    if model.startswith('claude-'):
        return claude_parse(system, prompt, schema, effort=settings.GENERATOR_EFFORT, max_tokens=settings.GENERATOR_MAX_TOKENS,
                            model=model, purpose='generate', timeout_s=settings.GENERATOR_TIMEOUT_S)
    # OpenRouter reasoning tokens share the response budget. High reasoning caused MiMo to consume 20k tokens
    # without completing the JSON. The concept already contains the engineering reasoning, so the writer only
    # needs low reasoning and a bounded response. Fail promptly instead of retrying a long provider call silently.
    return openrouter_json(system, prompt, schema, model=model, effort='low', max_tokens=20000, repairs=1,
                           transport_retries=0, request_timeout_s=300, purpose='generate')


def generation_request_record(system: str, prompt: str, schema: type[BaseModel], model: str | None = None) -> dict:
    """Serializable, secret-free record of what draft generation sends to the provider."""
    model = model or settings.GENERATOR_MODEL
    schema_json = schema.model_json_schema()
    if model.startswith('claude-'):
        return {
            'provider': 'anthropic', 'model': model, 'purpose': 'generate', 'effort': settings.GENERATOR_EFFORT,
            'max_tokens': settings.GENERATOR_MAX_TOKENS, 'timeout_s': settings.GENERATOR_TIMEOUT_S, 'system': system, 'user_prompt': prompt,
            'output_format': schema_json,
            'fallback_if_schema_is_too_large': {
                'system': schema_system(system, schema), 'user_prompt': prompt,
            },
        }
    return {
        'provider': 'openrouter', 'model': model, 'purpose': 'generate', 'effort': 'low',
        'max_tokens': 20000, 'request_timeout_s': 300, 'transport_retries': 0, 'json_repairs': 1,
        'messages': [
            {'role': 'system', 'content': schema_system(system, schema)},
            {'role': 'user', 'content': prompt},
        ],
        'output_schema': schema_json,
        'provider_policy': {'data_collection': 'deny'},
    }


def review_parse(system: str, prompt: str, schema: type[BaseModel], effort: str = 'high',
                 purpose: str = 'review', max_tokens: int = 32000, transport_retries: int = 4,
                 request_timeout_s: int = 900, repairs: int = 2, model: str | None = None,
                 timeout_s: int | None = None, reasoning_tokens: int | None = None) -> BaseModel:
    """Structured model call. Defaults to settings.REVIEW_MODEL; callers for other roles pass their dedicated model."""
    model = model or settings.REVIEW_MODEL
    if model.startswith('claude-'):
        return claude_parse(system, prompt, schema, effort=effort, max_tokens=max_tokens, model=model,
                            purpose=purpose, timeout_s=timeout_s or request_timeout_s)
    return openrouter_json(system, prompt, schema, model=model, effort=effort, max_tokens=max_tokens,
                           repairs=repairs, transport_retries=transport_retries,
                           request_timeout_s=request_timeout_s, purpose=purpose,
                           reasoning_tokens=reasoning_tokens)
