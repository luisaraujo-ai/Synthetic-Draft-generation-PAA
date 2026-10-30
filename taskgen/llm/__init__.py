"""Model access: Anthropic SDK for claude-* IDs, OpenRouter for the rest, one cost ledger.

Callers import this package and call `llm.generate_parse`, `llm.review_parse` and `llm.cost_summary`; tests replace
those attributes. Tunable names (models, efforts, timeouts) live in `llm.settings` and must be read from there.
Every Claude system prompt carries a 1-hour cache breakpoint because the guidelines (~19k tokens) repeat in every
call. Every call is logged in `LEDGER` with its tokens and cost.
"""
from taskgen.llm import settings
from taskgen.llm.anthropic_api import claude_parse
from taskgen.llm.ledger import LEDGER, PRICES, cost_summary
from taskgen.llm.routing import generate_parse, generation_request_record, review_parse

__all__ = ['LEDGER', 'PRICES', 'claude_parse', 'cost_summary', 'generate_parse', 'generation_request_record', 'review_parse',
           'settings']
