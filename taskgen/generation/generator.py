"""Draft generation. The concept step is concept.py, the judge is review/judge.py, the loops are in pipeline/.

The generator writes the complete task from the concept, the cell and the guidelines. It never sees existing tasks and
has no execution backend to lean on: every input is stated in the brief or written out in full.
"""
from taskgen import llm
from taskgen.generation import lint
from taskgen.generation.prompts import FIX
from taskgen.generation.schema import Edited


def fix_text(domain, d, problems, model):
    """One edit call on the brief and handbook only; far cheaper than regenerating the whole draft."""
    limit = domain.get('brief_max_words', 400)
    out = llm.generate_parse('You edit engineering task text to meet rules and change nothing else.',
                             FIX.format(target=int(limit * 0.9), handbook_max=domain.get('handbook_max_words', 1000),
                                        problems='\n'.join(f'- {p}' for p in problems),
                                        words=lint.words(d.instruction_md), brief=d.instruction_md,
                                        handbook=d.handbook_md), Edited, model=model)
    d.instruction_md, d.handbook_md = lint.strip_canary(out.instruction_md), lint.strip_canary(out.handbook_md)
