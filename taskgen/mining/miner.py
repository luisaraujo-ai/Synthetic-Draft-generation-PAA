"""Stage 0: mine model limitations from evaluated tasks into domains/<d>/limitations.json.

  uv run mine.py --domain electrical            # analyse new trials, rebuild the catalogue
  uv run mine.py --domain electrical --force    # re-analyse every trial

Reads only the tasks hand-picked in domains/<d>/trials.json (real graders, complete trial folders). Two passes:
1. Per trial. Deterministic triage first: a harness exception or no verifier output makes the trial "infra" and it is
   skipped. Claude then reads the statement, the grader, the per-test results and a condensed trajectory, sorts every
   failed test into model_limitation / task_defect / infra / unclear with step-level evidence, and records what the
   agent did right (counter-evidence for the catalogue). Cached in domains/<d>/mined/<task>/<trial>.json.
2. Catalogue. Claude merges the per-trial findings into limitations stated in terms that transfer to new systems,
   drops candidates that passing trials contradict or that rest on task defects, and grades confidence.
"""
import json
import re
import time
from typing import List, Literal

from pydantic import BaseModel, Field

from taskgen import llm
from taskgen.domain import guidelines
from taskgen.paths import ROOT

REPO = ROOT.parent / 'forge-ee-verifiers-library'  # past trials are read from this sibling checkout

STEP_CHARS = 2500      # per tool call / observation in the condensed trajectory (head and tail kept)
GRADER_CHARS = 30000   # per grader file
FAILURES_CHARS = 20000


class TestVerdict(BaseModel):
    test: str
    cause: Literal['model_limitation', 'task_defect', 'infra', 'unclear']
    explanation: str = Field(description='why this cause, citing grader values and trajectory steps')
    step_ids: List[int]


class Observation(BaseModel):
    name: str
    mechanism: str = Field(description='what the agent did and why it matters, in terms that transfer to other tasks')
    evidence: str = Field(description='quotes, numbers and the graded values that show it')
    step_ids: List[int]
    tests: List[str]


class TrialAnalysis(BaseModel):
    summary: str = Field(description='two or three sentences: approach taken and outcome')
    failed_tests: List[TestVerdict]
    limitations: List[Observation] = Field(description='only behaviour behind model_limitation failures')
    strengths: List[Observation] = Field(description='things done right that a weaker agent could plausibly miss')
    self_report_vs_grader: str = Field(description="did the agent's final claims match what the grader found?")


class Evidence(BaseModel):
    task: str
    trial: str
    tests: List[str]
    step_ids: List[int]
    detail: str


class Limitation(BaseModel):
    id: str = Field(description='lowercase-hyphenated, stable, e.g. "unverified-steady-state"')
    name: str
    mechanism: str = Field(description='the failure in general terms, not tied to the source system')
    how_to_elicit: str = Field(description='what a new task must contain so an agent with this limitation goes wrong')
    how_to_grade: str = Field(description='what kind of graded row catches it')
    evidence: List[Evidence]
    counter_evidence: List[Evidence] = Field(description='trials where the model handled the same thing correctly')
    confidence: Literal['high', 'medium', 'low']
    confidence_reason: str


class SourceTask(BaseModel):
    id: str
    summary: str = Field(description='one line: the system and what was asked')


class Rejected(BaseModel):
    name: str
    why: str


class Catalogue(BaseModel):
    limitations: List[Limitation]
    source_tasks: List[SourceTask]
    rejected: List[Rejected] = Field(description='candidates dropped (task defect, contradicted, one-off noise)')


SYSTEM = """You analyse how AI agents fail at {discipline} tasks, for people who write harder evaluation tasks. You
read real trials: the task statement, the grader, which tests failed with what values, and the agent's trajectory.
Separate what the model got wrong from what the task or harness got wrong. A failure is a model limitation only when
a competent engineer with the same statement, files and tools would have passed that test. Cite trajectory steps by
id. Be specific about mechanisms; "made an error" is not a finding.

The project's task and grader guidelines decide what counts as a task defect:
<guidelines>
{guidelines}
</guidelines>"""

TRIAL_PROMPT = """Analyse this trial.

<statement>
{statement}
</statement>

<grader>
{grader}
</grader>

Agent model: {model}. Reward: {reward}.
Test results:
{results}

<failure_details>
{failures}
</failure_details>

<trajectory note="condensed: long commands and outputs keep their head and tail">
{trajectory}
</trajectory>

For every failed test decide the cause:
- model_limitation: the agent could have passed with what it was given; say what it did instead.
- task_defect: the task or grader breaks the guidelines in a way that decides this test. For example: it checks a
  value that is in neither the brief nor the handbook (hidden limit, hidden output format); it crashes or cannot
  evaluate (a grader bug, not a model failure); it scores a number the agent reported instead of measuring; it
  rejects a valid design type or demands an exact geometry the brief does not require; it judges handbook data
  against one value where common practice is a range; the statement contradicts itself or another file.
- infra: harness, timeout or environment problem.
- unclear: the evidence does not settle it.
Then list the limitations behind the model_limitation failures, and the strengths (correct choices a weaker agent
could miss), each with step ids. Compare the agent's final claims with the grader's findings."""

CATALOGUE_PROMPT = """Build a catalogue of model limitations from these per-trial analyses of {discipline} tasks.

Tasks (statement openings):
{tasks}

Trials skipped as infra before analysis: {skipped}

Per-trial analyses:
{analyses}

Rules:
- Merge the same mechanism seen in several trials or tasks into one limitation; keep different mechanisms apart.
- Use only model_limitation findings as evidence. Drop anything that rests on a task_defect, infra or unclear failure.
- Check every candidate against the strengths of the other trials; if a trial shows the model handling the same thing
  correctly, list it as counter_evidence and lower the confidence, or reject the candidate if it is contradicted.
- State mechanism and how_to_elicit so they transfer to a different system in the same discipline: no circuit names,
  component values or numbers from the source task.
- Confidence: high = repeated across trials with clear step evidence and no counter-evidence; medium = one task or
  some counter-evidence; low = a single trial or an indirect inference.
- List every analysed task in source_tasks with a one-line summary."""


def clip(text, n=STEP_CHARS):
    text = str(text)
    if len(text) <= n:
        return text
    head = int(n * 0.6)
    return f'{text[:head]}\n…[{len(text) - n} chars cut]…\n{text[-(n - head):]}'


def condense(traj):
    """Agent steps only (the statement is passed separately): what it said, ran and saw. The last message is kept
    whole because the final claims are compared with the grader."""
    steps = [s for s in traj['steps'] if s.get('source') == 'agent']
    lines = []
    for i, s in enumerate(steps):
        last = i == len(steps) - 1
        lines.append(f"[step {s['step_id']}]")
        if s.get('message'):
            lines.append('said: ' + (s['message'] if last else clip(s['message'])))
        for c in s.get('tool_calls') or []:
            args = c.get('arguments') or {}
            lines.append('ran: ' + clip(args.get('input') if isinstance(args, dict) and 'input' in args
                                        else json.dumps(args, ensure_ascii=False)))
        for r in (s.get('observation') or {}).get('results', []):
            lines.append('saw: ' + clip(r.get('content')))
    return '\n'.join(lines)


def test_results(stdout):
    res = {}
    for status, name in re.findall(r'^(PASSED|FAILED|ERROR) (\S+)', stdout, re.M):
        res[name.split('::')[-1]] = status
    return res


def failure_details(stdout):
    m = re.search(r'=+ FAILURES =+(.*?)(=+ (short test summary|PASSES)|\Z)', stdout, re.S)
    return clip(m.group(1), FAILURES_CHARS) if m else '(none)'


def grader_text(task_files):
    tests = task_files / 'tests'
    files = sorted(p for p in tests.iterdir() if p.suffix in ('.py', '.json', '.sh') and p.is_file())
    return '\n\n'.join(f'# {p.name}\n{clip(p.read_text(errors="replace"), GRADER_CHARS)}' for p in files)


def trial_meta(ev):
    """Triage from the harness files alone; returns (meta, reason-to-skip or None)."""
    ef = ev / 'eval_files'
    result = json.loads((ef / 'result.json').read_text())
    reward = ((result.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    exc = (result.get('exception_info') or {}).get('exception_type')
    traj = json.loads((ef / 'agent' / 'trajectory.json').read_text()) if (ef / 'agent' / 'trajectory.json').exists() else None
    model = next((s['model_name'] for s in (traj or {}).get('steps', []) if s.get('model_name')), ev.name.split('__')[-1])
    meta = {'trial': ev.name, 'model': model, 'reward': reward, 'exception': exc}
    if exc:
        return meta, f'harness exception {exc}'
    if reward is None or not (ef / 'verifier' / 'test-stdout.txt').exists():
        return meta, 'no verifier output'
    if traj is None:
        return meta, 'no trajectory'
    return meta, None


def analyse_trial(domain, task, ev, meta):
    ef = ev / 'eval_files'
    tf = REPO / task['dir'] / 'task_files'
    stdout = (ef / 'verifier' / 'test-stdout.txt').read_text(errors='replace')
    results = test_results(stdout)
    prompt = TRIAL_PROMPT.format(
        statement=(tf / 'instruction.md').read_text(), grader=grader_text(tf), model=meta['model'],
        reward=meta['reward'], results='\n'.join(f'- {k}: {v}' for k, v in results.items()) or '(no per-test lines)',
        failures=failure_details(stdout),
        trajectory=condense(json.loads((ef / 'agent' / 'trajectory.json').read_text())))
    a = llm.claude_parse(SYSTEM.format(discipline=domain['discipline'], guidelines=guidelines()), prompt, TrialAnalysis)
    return {**meta, 'task': task['id'], 'tests': results, 'analysis': a.model_dump()}


def mine(domain, force=False):
    tasks = json.loads(domain.trials_file.read_text())['tasks']
    cache = domain.dir / 'mined'
    analyses, skipped, triage = [], [], []
    for task in tasks:
        evals = sorted((REPO / task['dir'] / 'evaluations').glob('*/eval_files/result.json'))
        if not evals:
            print(f"  {task['id']}: no evaluations under {task['dir']}", flush=True)
        for rf in evals:
            ev = rf.parent.parent
            meta, skip = trial_meta(ev)
            triage.append({'task': task['id'], **meta, 'triage': skip or 'analysed'})
            if skip:
                skipped.append(f"{task['id']}/{ev.name} ({skip})")
                print(f"  skip {task['id']}/{ev.name}: {skip}", flush=True)
                continue
            out = cache / task['id'] / f'{ev.name}.json'
            if out.exists() and not force:
                analyses.append(json.loads(out.read_text()))
                print(f"  cached {task['id']}/{ev.name}", flush=True)
                continue
            print(f"  analysing {task['id']}/{ev.name} (reward {meta['reward']})", flush=True)
            r = analyse_trial(domain, task, ev, meta)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(r, indent=1, ensure_ascii=False))
            analyses.append(r)
            causes = [t['cause'] for t in r['analysis']['failed_tests']]
            print(f"    {len(r['analysis']['limitations'])} limitations, failed-test causes: "
                  f"{', '.join(causes) or 'none'}", flush=True)
    if not analyses:
        raise SystemExit('no analysable trials: check trials.json')
    openings = '\n'.join(f"- {t['id']}: {clip((REPO / t['dir'] / 'task_files' / 'instruction.md').read_text(), 1500)}"
                         for t in tasks)
    print(f'building the catalogue from {len(analyses)} trials', flush=True)
    cat = llm.claude_parse(
        SYSTEM.format(discipline=domain['discipline'], guidelines=guidelines()),
        CATALOGUE_PROMPT.format(discipline=domain['discipline'], tasks=openings, skipped=', '.join(skipped) or 'none',
                                analyses=json.dumps([{k: a[k] for k in ('task', 'trial', 'model', 'reward', 'tests',
                                                                        'analysis')} for a in analyses],
                                                    indent=1, ensure_ascii=False)),
        Catalogue)
    out = {'domain': domain.name, 'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
           'models': sorted({a['model'] for a in analyses}), 'trials': triage, **cat.model_dump()}
    domain.limitations_file.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for l in cat.limitations:
        print(f"  [{l.confidence}] {l.id}: {l.name} ({len(l.evidence)} evidence, {len(l.counter_evidence)} counter)")
    for r in cat.rejected:
        print(f'  rejected {r.name}: {r.why}')
    print(f'wrote {domain.limitations_file}')
    return out
