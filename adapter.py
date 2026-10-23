"""Prompt adapter (Rainbow Teaming's mutator): turns a sampled grid cell into a task concept for the generator.

Inputs are abstract only: the limitation's mechanism (never the tasks it was mined from), the subdomain and its
descriptors, the guidelines, and evaluator feedback stored in the archive. Two modes, as in Rainbow Teaming:
- seed: the cell has no elite yet, so the adapter writes a fresh concept for it;
- mutate: the cell has an elite, so the adapter takes the elite's concept as the parent and changes it so that it
  should score higher on what the evaluator marked down (fitness: rules, executable run, Astra estimate), with a
  different engineering decision rather than reworded text.
The adapter model (llm.ADAPTER_MODEL) does this; the selected generator then writes the full draft.
"""
import json
from typing import List

from pydantic import BaseModel, Field

import llm
from domain import guidelines


class Concept(BaseModel):
    device: str = Field(description='what is designed, stated as a function, no mechanism or topology named')
    use_context: str
    function: str = Field(description='input -> transformation -> measurable output')
    operating_states: List[str] = Field(description='states and transitions that matter, each with what must hold')
    fixed_boundaries: List[str] = Field(description='interfaces, loads, resources and limits that are frozen')
    open_decisions: List[str] = Field(description='what the solver chooses')
    where_limitation_bites: str = Field(description='the decisive condition where an agent with the limitation goes '
                                                    'wrong, and why its own checks would miss it')
    binding_tradeoff: str = Field(description='the requirements that pull against each other and leave a narrow window')
    verification_sketch: str = Field(description='how open-source headless checks (the domain tools, the verifier '
                                                 'library) would measure every requirement from the delivered files')
    why_not_public: str = Field(description='why no well-known public design meets it')
    change_from_parent: str = Field(description='mutate mode: what differs from the parent concept and why it should '
                                                'score higher; empty in seed mode')


SYSTEM = """You are the prompt adapter in a Rainbow-Teaming-style generator of engineering design tasks
({discipline}). You turn a target cell (a model limitation x a subdomain, plus descriptors) into one task concept.
A separate writer turns your concept into the full task, and an evaluator scores it. Aim for concepts that are
genuinely hard for a frontier agent for real engineering reasons, checkable by open-source headless tools, and
different from anything common in textbooks or vendor reference designs. Follow the guidelines strictly.

<guidelines>
{guidelines}
</guidelines>"""

SEED = """Write a task concept for this cell.

{cell}
{failed}
Tools the solver and grader have (for the verification sketch only): {tools}"""

MUTATE = """Mutate the parent concept for this cell into a new one that should score higher.

{cell}

Parent concept (the cell's current elite, fitness {fitness:.2f}):
{parent}

What the evaluator marked down on the parent:
{weak}
{failed}
Change the engineering decision (device, operating states, binding trade-off, where the limitation bites), not just
numbers or wording. Tools the solver and grader have (for the verification sketch only): {tools}"""


def cell_text(lim, subdomain, family, descriptors, profile):
    return '\n'.join([
        'Target cell:', f"- limitation \"{lim['id']}\": {lim['name']}", f"  mechanism: {lim['mechanism']}",
        f"  what a task needs to elicit it: {lim['how_to_elicit']}", f"  what kind of check catches it: {lim['how_to_grade']}",
        f'- subdomain: {subdomain}', f'- circuit family: {family}',
        *[f'- {k}: {v}' for k, v in descriptors.items()], *[f'- {k}: {v}' for k, v in profile.items()]])


def adapt(domain, lim, subdomain, family, descriptors, profile, parent=None, failed=()):
    """Returns (Concept, mode)."""
    system = SYSTEM.format(discipline=domain['discipline'], guidelines=guidelines())
    cell = cell_text(lim, subdomain, family, descriptors, profile)
    failed_txt = ('\nEarlier attempts in this cell that failed (avoid repeating them):\n' +
                  '\n'.join(f"- {e['summary']} ({e['status']}): {'; '.join(e['notes'])}" for e in failed) + '\n') \
        if failed else ''
    if parent and parent.get('concept'):
        prompt = MUTATE.format(cell=cell, fitness=parent.get('fitness') or 0,
                               parent=json.dumps(parent['concept'], indent=1, ensure_ascii=False),
                               weak='\n'.join(f'- {n}' for n in parent.get('notes') or []) or '- (no notes)',
                               failed=failed_txt, tools=domain['tools'])
        mode = 'mutate'
    else:
        prompt = SEED.format(cell=cell, failed=failed_txt, tools=domain['tools'])
        mode = 'seed'
    return llm.review_parse(system, prompt, Concept, model=llm.ADAPTER_MODEL, effort='medium',
                            max_tokens=llm.ADAPTER_MAX_TOKENS, repairs=1,
                            transport_retries=0, request_timeout_s=240, purpose='adapter'), mode
