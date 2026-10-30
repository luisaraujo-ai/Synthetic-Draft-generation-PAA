"""Helpers shared by the Anthropic and OpenRouter structured-output paths."""
import json

from pydantic import BaseModel


def json_text(resp):
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return text[text.find('{'):text.rfind('}') + 1]


def schema_system(system: str, schema: type[BaseModel]) -> str:
    return (f'{system}\n\nReply with exactly one JSON object and nothing else (no prose, no code fence). It must '
            f'validate against this JSON Schema:\n{json.dumps(schema.model_json_schema())}')
