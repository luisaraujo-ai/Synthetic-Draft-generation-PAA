"""Evaluator build: a cheap model (llm.EVALUATOR_MODEL over OpenRouter, tool calling) designs the circuit in the domain
image and grades it with the task's kforge grader.

Two modes:
- informed (default): it gets the brief, the handbook, the generated inputs, the reference outline and a grade tool.
  Its job is a good run: a design that passes every check, which shows the task is feasible and the grader works,
  and records which checks were hard to meet. Its passing design is kept for the SME as a starting point.
- probe (optional): brief only (plus the inputs), no outline and no grade tool, like a real agent run. Its reward is
  a cheap difficulty signal.
Budget: domain.json "evaluator" (max_turns, tool_timeout_s, wall_minutes). The transcript, grade history and the
final design go to <draft>/eval/<mode>/.
"""
import json
import shutil
import signal
from contextlib import contextmanager
import time

import llm
import sandbox

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'bash', 'description': 'Run a shell command in the workstation (cwd /app). Output is truncated.',
        'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']}}},
    {'type': 'function', 'function': {
        'name': 'write_file', 'description': 'Write a text file at an absolute path under /app.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                       'required': ['path', 'content']}}},
    {'type': 'function', 'function': {
        'name': 'grade', 'description': 'Grade /app/output against the task grader; returns every check with its '
                                        'status, measured value, limit, unit and margin, and the reward.',
        'parameters': {'type': 'object', 'properties': {}}}},
    {'type': 'function', 'function': {
        'name': 'submit', 'description': 'Finish: the design in /app/output is final.',
        'parameters': {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary']}}},
]

SYSTEM = """You are an expert electronics engineer working on a Linux workstation with KiCad 10.0.6 (kicad-cli),
ngspice 47 and Python 3.12 (numpy, scipy). There is no network. Work in /app:
- /app/starter: optional starting files; use only those relevant to the brief.
- /app/input_assets: fixed task inputs.
- /app/output: put every deliverable at the exact path required by the brief.
The brief determines the format: a SPICE netlist task needs a netlist, a KiCad task needs its project.
For KiCad schematics you may use kforge.kicad.schbuild.SchematicBuilder; preserve published ports and roles.
For netlists, honor the published subcircuit name, pins, model references and allowed elements.
Read the inputs before designing. Never invent missing process models or fixed external components.
{mode_note}
Be efficient: you have {budget_s:g} seconds total, including simulation and grading, and at most {max_turns} tool calls. Call submit when the design is final."""

INFORMED = """You also have the task author's reference outline and a grade tool that runs the real grader. Your goal
is a design that passes every check. Use the outline as a starting point, grade often, and fix what fails."""
PROBE = """Design it as you would for a customer: verify it yourself; there is no grade tool."""

USER = """{brief}

{handbook}{outline}"""


class EvaluatorDeadline(BaseException):
    """Wall budget exhausted; bypass retry and tool error handlers."""


@contextmanager
def wall_deadline(deadline):
    """Interrupt blocking model calls and tools at the deadline (POSIX main thread)."""
    def expired(signum, frame):
        raise EvaluatorDeadline()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvaluatorDeadline()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _call_tool(sb, name, args, timeout, grades):
    if name == 'bash':
        code, out = sb.exec(args.get('command', ''), timeout=timeout)
        return f'exit {code}\n{out[-6000:]}'
    if name == 'write_file':
        try:
            sb.write(args['path'], args['content'])
            return f"wrote {args['path']} ({len(args['content'])} chars)"
        except (ValueError, KeyError) as e:
            return f'error: {e}'
    if name == 'grade':
        code, checks, log = sb.grade(timeout=timeout)
        grades.append({'t': time.time(), 'reward': (checks or {}).get('reward'), 'checks': checks})
        return sandbox.summarize(checks) if checks else f'grade failed (exit {code}):\n{log[-2500:]}'
    return 'unknown tool'


def run(domain, draft_dir, draft, mode='informed'):
    """One evaluator build. Returns a dict: reward, turns, grades (reward per grade call), final checks summary,
    transcript tail, design path."""
    cfg = domain.get('evaluator', {})
    max_turns, timeout = cfg.get('max_turns', 40), cfg.get('tool_timeout_s', 600)
    budget_s = min(600.0, 60 * float(cfg.get('wall_minutes', 10)))
    if budget_s <= 0:
        raise ValueError('evaluator.wall_minutes must be positive')
    started = time.monotonic()
    deadline = started + budget_s
    kit_dir, out_dir = draft_dir / 'kit', draft_dir / 'eval' / mode
    shutil.rmtree(out_dir, ignore_errors=True)
    work = out_dir / 'work'
    shutil.copytree(kit_dir / 'starter', work / 'starter')
    shutil.copytree(kit_dir / 'input_assets', work / 'input_assets')
    (work / 'output').mkdir(parents=True)
    tools = TOOLS if mode == 'informed' else [t for t in TOOLS if t['function']['name'] != 'grade']
    messages = [{'role': 'system', 'content': SYSTEM.format(mode_note=INFORMED if mode == 'informed' else PROBE,
                                                            max_turns=max_turns, budget_s=budget_s)},
                {'role': 'user', 'content': USER.format(
                    brief=draft['instruction_md'],
                    handbook=f"Reference data (handbook):\n{draft['handbook_md']}\n" if mode == 'informed' else '',
                    outline=f"\nReference outline (from the task author):\n{draft['reference_outline']}"
                    if mode == 'informed' else '')}]
    grades, turns, summary = [], 0, ''
    final, timed_out = None, False
    with sandbox.Sandbox(domain, work, kit_dir / 'tests' if mode == 'informed' else None) as sb:
        try:
            with wall_deadline(deadline):
                while turns < max_turns and time.monotonic() < deadline:
                    choice = llm._openrouter(llm.EVALUATOR_MODEL, messages, 16000, 'medium', purpose=f'evaluator-{mode}',
                                             tools=tools)
                    msg = choice['message']
                    messages.append({k: v for k, v in msg.items() if k in ('role', 'content', 'tool_calls') and v is not None})
                    calls = msg.get('tool_calls') or []
                    if not calls:
                        messages.append({'role': 'user', 'content': 'Continue with a tool call, or call submit if you are done.'})
                        turns += 1
                        continue
                    done = False
                    for c in calls:
                        if time.monotonic() >= deadline:
                            raise EvaluatorDeadline()
                        if turns >= max_turns:
                            break
                        turns += 1
                        name = c['function']['name']
                        try:
                            args = json.loads(c['function'].get('arguments') or '{}')
                        except ValueError:
                            args = {}
                        if name == 'submit':
                            summary, done = args.get('summary', ''), True
                            result = 'submitted'
                        else:
                            result = _call_tool(sb, name, args, min(timeout, max(0.001, deadline - time.monotonic())), grades)
                        messages.append({'role': 'tool', 'tool_call_id': c['id'], 'content': result})
                        if mode == 'informed' and grades and grades[-1]['reward'] == 1:
                            done, summary = True, summary or 'grader passed'  # a good run: stop spending
                        if done:
                            break
                    if done:
                        break
                # Always grade the final artifacts, never reuse a pre-edit result.
                with sandbox.Sandbox(domain, work, kit_dir / 'tests') as gb:
                    _, final, _ = gb.grade()
        except EvaluatorDeadline:
            timed_out = True
            summary = 'Evaluator exceeded its wall-time budget'
    elapsed_s = time.monotonic() - started
    timed_out = timed_out or elapsed_s >= budget_s
    design = out_dir / 'design'
    shutil.copytree(work / 'output', design)
    if final and (not final.get('checks') or any(c.get('status') != 'pass' for c in final['checks'])):
        final['reward'] = 0
    res = {'mode': mode, 'model': llm.EVALUATOR_MODEL, 'reward': 0 if timed_out else (final or {}).get('reward', 0), 'turns': turns, 'submitted': bool(summary) and not timed_out,
           'timed_out': timed_out, 'elapsed_s': round(elapsed_s, 3), 'budget_s': budget_s,
           'summary': summary, 'grade_history': [g['reward'] for g in grades],
           'checks': sandbox.summarize(final), 'checks_raw': final,
           'transcript_tail': [m.get('content') for m in messages[-12:] if m.get('role') == 'assistant' and m.get('content')]}
    (out_dir / 'result.json').write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    (out_dir / 'transcript.json').write_text(json.dumps(messages, indent=1, ensure_ascii=False, default=str))
    shutil.rmtree(work, ignore_errors=True)
    return res
