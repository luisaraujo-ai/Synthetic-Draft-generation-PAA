"""Concept step: turns a sampled cell (limitation x subdomain x descriptors) into a task concept for the generator.

Inputs are abstract only: the limitation's mechanism (never the tasks it was mined from), the subdomain and its
descriptors, the guidelines, and the tasks already drafted in this cell (to avoid). Nothing about an execution
backend: the concept must be checkable with generic open-source headless tools and standalone.
"""
from typing import List

from pydantic import BaseModel, Field

from taskgen import llm
from taskgen.domain import guidelines


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
    inputs_needed: List[str] = Field(description='every fixed input the solver needs, each either a number the brief '
                                                 'states or a small file the task writes out in full; nothing that '
                                                 'has to already exist somewhere')
    verification_sketch: str = Field(description='how open-source headless checks would measure every requirement '
                                                 'from the delivered files')
    why_not_public: str = Field(description='why no well-known public design meets it')


SYSTEM = """You turn a target cell (a model limitation x a subdomain, plus descriptors) into one engineering design
task concept ({discipline}). A separate writer turns your concept into the full task, and a judge scores it. Aim for
concepts that are genuinely hard for a frontier agent for real engineering reasons, checkable by open-source headless
tools, and different from textbook or vendor reference designs. The concept must be standalone: it may rely only on
numbers the brief states and small files the task writes out itself. Never rely on a starter project, a parts
library, a vendor model, a datasheet or any file or part that must already exist. Follow the guidelines strictly.

<guidelines>
{guidelines}
</guidelines>"""

PROMPT = """Write a task concept for this cell.

{cell}
{avoid}
Tools the solver and checker have (for the verification sketch only): {tools}"""


def cell_text(lim, subdomain, family, descriptors, profile):
    return '\n'.join([
        'Target cell:', f"- limitation \"{lim['id']}\": {lim['name']}", f"  mechanism: {lim['mechanism']}",
        f"  what a task needs to elicit it: {lim['how_to_elicit']}", f"  what kind of check catches it: {lim['how_to_grade']}",
        f'- subdomain: {subdomain}', f'- circuit family: {family}',
        *[f'- {k}: {v}' for k, v in descriptors.items()], *[f'- {k}: {v}' for k, v in profile.items()]])


def adapt(domain, cell, avoid=()):
    avoid_txt = ('\nTasks already drafted for this subdomain (do not repeat their engineering decision):\n' +
                 '\n'.join(f'- {a}' for a in avoid) + '\n') if avoid else ''
    return llm.review_parse(SYSTEM.format(discipline=domain['discipline'], guidelines=guidelines()),
                            PROMPT.format(cell=cell, avoid=avoid_txt, tools=domain['tools']), Concept,
                            model=llm.settings.ADAPTER_MODEL, effort='medium', max_tokens=llm.settings.ADAPTER_MAX_TOKENS, repairs=1,
                            transport_retries=0, request_timeout_s=240, purpose='adapter')
