"""Steps shared by the serial loop, the parallel workers and re-judging: timing, wording fixes, judging, concepts."""
import json
import time

from taskgen import llm
from taskgen.generation import lint
from taskgen.generation.generator import fix_text
from taskgen.review import judge


def timed(label, call):
    """Run one model call, announcing it first and its duration after (the OpenRouter calls are not streamed)."""
    print(f'    {label} ...', flush=True)
    started = time.monotonic()
    result = call()
    print(f'    {label}: {time.monotonic() - started:.0f}s', flush=True)
    return result


def judge_call(d, lim, domain):
    """The judge with a capped thinking budget; on a timeout or a bad answer, once more with half the budget.
    None if both fail."""
    for budget in (llm.settings.JUDGE_REASONING_TOKENS, llm.settings.JUDGE_REASONING_TOKENS // 2):
        try:
            return timed(f'judge with {llm.settings.REVIEW_MODEL} (thinking cap {budget} tok, limit {llm.settings.JUDGE_TIMEOUT_S}s)',
                         lambda: judge.review(d.model_dump(), lim, concept_models(domain), reasoning_tokens=budget))
        except (TimeoutError, RuntimeError) as exc:
            print(f'  judge failed with cap {budget}: {type(exc).__name__}: {str(exc)[:200]}', flush=True)
    return None


def polish_style(domain, d, model, rounds=3):
    """Lint the draft; fix wording problems (tools, settings, preferences, benchmark words, length) with edit calls
    on the brief and handbook. Returns what is left: structural problems, or style ones that survived every round."""
    problems = lint.check(d, domain)
    for n in range(rounds):
        style, struct = lint.split(problems)
        if not style or struct:  # nothing to fix, or the draft is regenerated anyway
            return problems
        print(f'  wording: {len(style)} problem(s), edit {n + 1}/{rounds}', flush=True)
        for p in style:
            print(f'    - {p[:200]}', flush=True)
        timed('edit brief/handbook', lambda: fix_text(domain, d, style, model))
        problems = lint.check(d, domain)
    return problems


def concept_models(domain):
    return json.loads(domain.limitations_file.read_text()).get('models', [])
