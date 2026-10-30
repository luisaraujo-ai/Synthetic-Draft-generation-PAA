"""Model names, effort levels, token caps and timeouts. Every value can be overridden with a TASKGEN_* variable.

The values are module attributes read at call time, so a worker process can reconfigure them (see
pipeline/parallel/worker.py). Read them as `settings.NAME`; a `from ... import NAME` would keep a stale copy.
"""
import os

CLAUDE_MODEL = 'claude-sonnet-5-5'  # Opus 5.5 is too expensive for the prototype, and Haiku 5.5 is too small for the draft
GENERATOR_MODEL = os.environ.get('TASKGEN_GENERATOR_MODEL', CLAUDE_MODEL)
GENERATOR_EFFORT = os.environ.get('TASKGEN_GENERATOR_EFFORT', 'medium')
GENERATOR_MAX_TOKENS = int(os.environ.get('TASKGEN_GENERATOR_MAX_TOKENS', '32000'))
GENERATOR_TIMEOUT_S = int(os.environ.get('TASKGEN_GENERATOR_TIMEOUT_S', '420'))  # wall clock per generation call
REVIEW_MODEL = os.environ.get('TASKGEN_REVIEW_MODEL', 'xiaomi/mimo-v2.6-pro')
JUDGE_EFFORT = os.environ.get('TASKGEN_JUDGE_EFFORT', 'medium')
JUDGE_REASONING_TOKENS = int(os.environ.get('TASKGEN_JUDGE_REASONING_TOKENS', '3000'))  # cap on the judge's thinking
JUDGE_TIMEOUT_S = int(os.environ.get('TASKGEN_JUDGE_TIMEOUT_S', '600'))  # wall clock per judge call
ADAPTER_MODEL = os.environ.get('TASKGEN_ADAPTER_MODEL', 'claude-sonnet-5-5')
ADAPTER_MAX_TOKENS = int(os.environ.get('TASKGEN_ADAPTER_MAX_TOKENS', '12000'))
NOVELTY_MODEL = os.environ.get('TASKGEN_NOVELTY_MODEL', ADAPTER_MODEL)
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
