"""The task draft the generator returns (pydantic schema) and the text-only edit result."""
from typing import List, Literal

from pydantic import BaseModel, Field


class Requirement(BaseModel):
    rid: str = Field(description='R1, R2, ...')
    metric: str = Field(description='the measured quantity, e.g. "output ripple, peak to peak"')
    condition: str = Field(description='state, load, corner or configuration it must hold under')
    location: str = Field(description='where it is measured or extracted')
    limit: str = Field(description='minimum, maximum or band, with units, e.g. "<= 8 mV"')


class OutputFile(BaseModel):
    path: str = Field(description='exact path of the delivered file, under the artifact root')
    format: str
    contents: str = Field(description='fields, units and structure the checker reads; published in the brief')


class CheckPlan(BaseModel):
    cid: str = Field(description='G1, G2, ...')
    covers: List[str] = Field(description='requirement rids')
    method: str = Field(description='what the checker runs itself on the delivered files, with which open-source '
                                    'tool; never a number the agent reports; names the output file it reads')
    tolerance: str = Field(description='how it accepts handbook ranges and reasonable method differences, so any '
                                       'sound design passes')
    held_out: str = Field(description='test points sampled inside a declared range, if any; empty otherwise')


class PublicDesign(BaseModel):
    name: str
    fails: str = Field(description='the requirement rid it fails and by roughly how much')


class Asset(BaseModel):
    path: str = Field(description='path under the input root (model-visible)')
    purpose: str
    content: str = Field(description='the complete file text (small text files only)')


class Draft(BaseModel):
    title: str = Field(description='functional title: the required outcome and context, no open mechanism named')
    slug: str = Field(description='lowercase-hyphenated, under 50 characters')
    task_form: Literal['feasibility', 'target matching', 'tight target']
    design_freedom: Literal['parameter selection', 'configuration design', 'concept synthesis']
    instruction_md: str = Field(description='the brief: a short request in the voice of the person asking, stating '
                                            'every requirement, fixed interface and output file briefly; no hint of '
                                            'a benchmark, grader or environment, no solution hints')
    handbook_md: str = Field(description='the separate handbook: data a skilled engineer knows or looks up, constants '
                                         'as ranges; no requirement, interface or solution hint lives only here')
    requirements: List[Requirement]
    outputs: List[OutputFile]
    provided_assets: List[Asset] = Field(description='input files the task writes out in full; empty when the brief '
                                                     'states every fixed input')
    external_references: List[str] = Field(description='every real-world thing the brief or handbook names (part '
                                                       'number, standard, datasheet, library, vendor model), each '
                                                       'with why you are certain it exists; empty when none')
    grader_plan: List[CheckPlan]
    distinct_valid_designs: List[str] = Field(description='at least three materially different designs that would '
                                                          'pass, one line each (evaluator-only)')
    reference_outline: str = Field(description='how a reference solution solves it (evaluator-only, never in the '
                                               'brief or handbook)')
    catching_rows: List[str] = Field(description='rids of the requirements an agent showing the targeted limitation '
                                                 'fails')
    public_designs: List[PublicDesign] = Field(description='well-known public designs a solver would reach for and '
                                                           'the requirement each fails')
    mechanism_survival: str = Field(description='cite the limitation id and the requirement rows that catch it; why '
                                                'an agent with it still fails although it can simulate and '
                                                'measure its own design (rule 7)')


class Edited(BaseModel):
    instruction_md: str
    handbook_md: str
