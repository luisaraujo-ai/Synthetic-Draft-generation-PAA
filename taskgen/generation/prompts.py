"""Generator prompts: the system prompt, the draft prompt and the text-edit prompt."""
from taskgen.domain import guidelines


SYSTEM = """You write engineering design tasks ({discipline}) for a benchmark that measures AI agents. Each task
targets one limitation that real agent trials have shown, so it measures that weakness instead of general
difficulty. Every requirement must be measurable and every number must be something the stated tools could reproduce.
The solver works in {environment}. Tools the solver and checker have, for your feasibility reasoning and the check
plan only (never name them in the brief or handbook): {tools}.

The task is STANDALONE. This is the contract; the brief states its interface parts in plain words (delivered file
path, ports or fields, roles):
{contract}

The verified asset inventory the guidelines mention is EMPTY. Never state or imply that something exists unless the brief or one of your provided files holds it in full. If you
are not certain a real part, standard or document exists, do not name it: give the parameters instead.

Follow the guidelines below strictly. They override the concept, the cell and the targeted limitation wherever those
conflict with them.

<guidelines>
{guidelines}
</guidelines>"""

PROMPT = """Write one task draft from this concept.

{cell}

Concept (keep its engineering decision, fix anything that breaks the guidelines):
{concept}
{retry}
Already drafted in this run (do not repeat these topics): {previous}

Requirements for the draft (guidelines/04_generation_rules.md is checked mechanically; a draft that breaks it is
sent back):
- Brief (instruction_md): aim for 400 words and keep within 450 words. Move supporting detail to the handbook. Word count alone must not cause rejection or a revision request. A request in the voice of the person in the framing. Two or
  three sentences of context, then every requirement (metric, condition, location, limit), the fixed interfaces and
  each output file with its format. Nothing else: no software, tool, solver or setting names; no test bench; no
  procedure; no preference without a number; no benchmark words; no canary. Apply the three sentence tests.
- Handbook (handbook_md): at most {handbook_max_words} words. Data an engineer would know or look up (constants and
  practice as ranges), measurement definitions and reference conventions. Graded limits and interfaces stay in the
  brief; no hidden limits.
- Standalone inputs: every fixed input is a number the brief states or a small text file you write out in full in
  provided_assets (schema, units, ranges, consistent with the stated physics). No starter project, library, vendor
  model or part you cannot place. List anything real you name in external_references.
- Outputs: only files a check in the plan reads (name the file in the check's method), under {artifact_root}.
- One concrete {unit}, small enough to finish in one attempt (2 h, 400 turns, 10M tokens).
- An agent that shows the targeted limitation must fail at least one requirement; list those in catching_rows. No
  route around the limitation may pass every requirement. Do not point at the trap.
- The task must be hard for a strong agent for real engineering reasons: limits leave a narrow window around the
  targeted trade-off, not large margins for any sensible design, and no public design meets every requirement.
- The check plan measures the delivered files themselves, covers every requirement and operating state, accepts every
  valid design, and uses held-out points inside declared ranges where the limitation needs them.
- Complete JSON: at most {draft_max_words} words across all fields. Keep reference_outline at most
  {reference_max_words} words and all provided file content together at most {assets_max_chars} characters."""


def system_prompt(domain):
    return SYSTEM.format(discipline=domain['discipline'], environment=domain['environment'], tools=domain['tools'],
                         contract=domain['deliverable_contract'], guidelines=guidelines())


def user_prompt(domain, cell, concept, retry, previous):
    return PROMPT.format(cell=cell, concept=concept.model_dump_json(indent=1), retry=retry,
                         previous='; '.join(previous) or 'none', max_words=domain.get('brief_max_words', 400),
                         aim_words=int(domain.get('brief_max_words', 400) * 0.82),
                         handbook_max_words=domain.get('handbook_max_words', 1000),
                         reference_max_words=domain.get('reference_outline_max_words', 400),
                         draft_max_words=domain.get('draft_max_words', 6000),
                         assets_max_chars=domain.get('generated_assets_max_chars', 12000),
                         artifact_root=domain['artifact_root'], unit=domain['unit_of_design'])


FIX = """Edit the brief and handbook of this engineering task so they follow the rules. Change only what the feedback
names and keep everything else word for word. Never change a requirement's number, unit, limit or condition, the
fixed interfaces or the output files. A sentence that breaks a rule because of the words it uses (a tool, a solver
setting, a preference, a benchmark word) is rephrased as a plain engineering definition or, if it is only rationale
or procedure, deleted; a definition that changes pass or fail may move to the handbook. The brief must end up at most
{target} words and the handbook at most {handbook_max}.

Feedback:
{problems}

<brief words="{words}">
{brief}
</brief>

<handbook>
{handbook}
</handbook>

Return the complete instruction_md and handbook_md."""


def retry_note(d, why):
    return (f'\nYour previous attempt "{d.title}" was sent back:\n{why}\n\nYour previous brief, to edit rather than '
            f'rewrite from scratch:\n<previous_brief>\n{d.instruction_md}\n</previous_brief>\nFix every point and '
            'keep the engineering that was sound. Do not claim anything exists that the brief or your files do not '
            'hold in full.\n')
