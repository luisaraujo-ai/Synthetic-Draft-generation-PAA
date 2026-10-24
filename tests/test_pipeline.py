"""Offline test of the loop: the model calls are stubbed, everything else (sampling, lint, judge gate, outputs) runs."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapter
import judge
import lint
import llm
import run
import sample
from domain import Domain

BRIEF = ('Our product team needs a buck stage. Input 12 V, output 3.3 V at 2 A. Output ripple must be at most 20 mV '
         'peak to peak at 1 MHz load steps of 0.5 A. Deliver the design as a SPICE netlist in /app/output/design.cir. '
         + 'Context. ' * 5)


def draft(**kw):
    d = dict(title='Buck stage', slug='buck', task_form='feasibility', design_freedom='configuration design',
             instruction_md=BRIEF, handbook_md='Inductor tolerance 10 to 20 percent.',
             requirements=[dict(rid='R1', metric='ripple', condition='0.5 A step', location='Vout', limit='<= 20 mV')],
             outputs=[dict(path='/app/output/design.cir', format='SPICE', contents='netlist')],
             provided_assets=[], external_references=[],
             grader_plan=[dict(cid='G1', covers=['R1'], method='simulates design.cir and measures ripple',
                               tolerance='any valid', held_out='')],
             distinct_valid_designs=['a', 'b', 'c'], reference_outline='x', catching_rows=['R1'], public_designs=[],
             mechanism_survival='x')
    d.update(kw)
    return sample.Draft(**d)


def review(p, verdict='pass'):
    return judge.Review(criteria=[judge.Criterion(rule=n, verdict=verdict, evidence='e', fix='') for n, _ in judge.CRITERIA],
                        difficulty=judge.Difficulty(easiest_route='r', where_it_fails='w', p_pass_per_attempt=p,
                                                    confidence='low', harder_if='narrow it'), summary='s')


class Loop(unittest.TestCase):
    def setUp(self):
        self.domain = Domain('electrical')
        self.tmp = Path(tempfile.mkdtemp())
        self.args = types.SimpleNamespace(n=1, subdomain='power electronics', retries=2)
        self.sampler = sample.Sampler(self.domain, 1)
        concept = adapter.Concept(device='d', use_context='u', function='f', operating_states=[], fixed_boundaries=[],
                                  open_decisions=[], where_limitation_bites='w', binding_tradeoff='b', inputs_needed=[],
                                  verification_sketch='v', why_not_public='n')
        self.saved = (adapter.adapt, llm.generate_parse, judge.review)
        adapter.adapt = lambda *a, **k: concept
        self.drafts, self.reviews = [], []
        llm.generate_parse = lambda *a, **k: self.drafts.pop(0)
        judge.review = lambda *a, **k: self.reviews.pop(0)  # accepts effort=

    def tearDown(self):
        adapter.adapt, llm.generate_parse, judge.review = self.saved

    def go(self):
        return run.candidate(self.domain, self.sampler, 0, self.args, self.tmp, [], 'claude-sonnet-5-5')

    def test_hard_and_clean_is_accepted(self):
        self.drafts, self.reviews = [draft()], [review(0.2)]
        e = self.go()
        self.assertEqual(e['status'], 'accepted')
        task = self.tmp / 'tasks' / '00-buck'
        for f in ('instruction.md', 'handbook.md', 'draft.json', 'judge.json'):
            self.assertTrue((task / f).is_file(), f)

    def test_difficulty_estimate_is_advisory(self):
        self.drafts, self.reviews = [draft()], [review(0.9)]
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertTrue((self.tmp / 'tasks' / '00-buck' / 'judge.json').is_file())

    def test_guideline_failure_is_rejected(self):
        self.drafts, self.reviews = [draft() for _ in range(3)], [review(0.1, 'unclear') for _ in range(3)]
        self.assertEqual(self.go()['status'], 'rejected')

    def edits(self, long_or_bad, fixed_brief=BRIEF, handbook='h'):
        """generate_parse stub: Draft calls pop self.drafts, Edited calls return the fixed text and are counted."""
        self.edit_calls = 0

        def stub(system, prompt, schema, model=None):
            if schema is sample.Edited:
                self.edit_calls += 1
                return sample.Edited(instruction_md=fixed_brief, handbook_md=handbook)
            return self.drafts.pop(0)
        llm.generate_parse = stub

    def test_tool_name_in_brief_is_edited_not_regenerated(self):
        self.drafts, self.reviews = [draft(instruction_md=BRIEF + ' Use ngspice to check it.')], [review(0.2)]
        self.edits(True)
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertEqual((self.edit_calls, self.drafts), (1, []))

    def test_long_brief_is_advisory_only(self):
        self.drafts, self.reviews = [draft(instruction_md=BRIEF + 'filler ' * 450)], [review(0.2)]
        self.edits(True)
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertEqual(self.edit_calls, 0)

    def test_structural_lint_problem_regenerates(self):
        bad = draft(handbook_md='See models.lib for the diode.')
        self.drafts, self.reviews = [bad, draft()], [review(0.2)]
        self.edits(False)
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertEqual((self.edit_calls, self.drafts), (0, []))

    def test_text_only_judge_failure_is_edited_and_rejudged(self):
        first = review(0.2)
        first.criteria[0].verdict = 'fail'  # function-first: a text criterion
        self.drafts, self.reviews = [draft()], [first, review(0.2)]
        self.edits(False)
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertEqual((self.edit_calls, self.reviews), (1, []))

    def test_judge_timeout_halves_thinking_budget_then_gives_up(self):
        calls = []

        def slow(*a, reasoning_tokens=None, **k):
            calls.append(reasoning_tokens)
            raise TimeoutError('slow')
        judge.review = slow
        self.drafts = [draft()]
        self.edits(False)
        e = self.go()
        self.assertEqual((e['status'], calls), ('unjudged', [llm.JUDGE_REASONING_TOKENS, llm.JUDGE_REASONING_TOKENS // 2]))

    def test_non_text_judge_failure_regenerates(self):
        first = review(0.2)
        first.criteria[[n for n, _ in judge.CRITERIA].index('standalone')].verdict = 'fail'
        self.drafts, self.reviews = [draft(), draft()], [first, review(0.2)]
        self.edits(False)
        self.assertEqual(self.go()['status'], 'accepted')
        self.assertEqual(self.edit_calls, 0)

    def test_missing_file_reference_is_flagged(self):
        d = draft(handbook_md='See models.lib for the diode.')
        self.assertTrue(any('models.lib' in p for p in lint.check(d, self.domain)))


if __name__ == '__main__':
    unittest.main()
