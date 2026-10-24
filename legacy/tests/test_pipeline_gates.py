import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import evaluate
import evidence
import judge
import kit
import llm
import package
import refine
import sample
import solver
import verifier_audit


class PipelineGates(unittest.TestCase):
    def review(self, verdict='ready', criterion='pass'):
        return SimpleNamespace(verdict=verdict, criteria=[SimpleNamespace(rule='physics', required=True, verdict=criterion)],
                               astra=SimpleNamespace(p_pass_per_attempt=0.2))

    def test_unclear_required_blocks(self):
        self.assertFalse(judge.passes(self.review(criterion='unclear'), 1)[0])

    def test_sampling_phase_timer_persists_clean_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'timing.json'
            timing = {'status': 'running', 'current_phase': None, '_monotonic_start': time.monotonic()}
            result, elapsed = sample._timed_phase(timing, path, 'concept', lambda: 'done')
            saved = json.loads(path.read_text())
            self.assertEqual(result, 'done')
            self.assertGreaterEqual(elapsed, 0)
            self.assertNotIn('_monotonic_start', saved)
            self.assertIsNone(saved['current_phase'])

    def test_cost_summary_can_measure_one_pipeline_step(self):
        records = [{'model': 'm', 'purpose': 'p', 'input': 10, 'output': 5,
                    'cache_read': 0, 'cache_write': 0, 'usd': 0.01234}]
        summary = llm.cost_summary(records)
        self.assertEqual(summary['total_usd'], 0.0123)
        self.assertEqual(summary['by_purpose']['p (m)']['calls'], 1)

    def test_fix_first_blocks(self):
        self.assertFalse(judge.passes(self.review(verdict='fix first'), 1)[0])

    def test_explicit_deferred_package_check_allowed(self):
        self.assertTrue(judge.passes(self.review(criterion='deferred'), 1)[0])

    def test_readiness_needs_all_evidence(self):
        good = dict(outcome='feasible', kit=dict(ok=True,audit_ok=True), informed=dict(reward=1,timed_out=False))
        self.assertTrue(evidence.physically_ready(good))
        for field,value in [('kit',dict(ok=True)),('informed',dict(reward=1,timed_out=True)),('blocking_issues',['model not validated'])]:
            bad=copy.deepcopy(good);bad[field]=value
            self.assertFalse(evidence.physically_ready(bad))
        self.assertFalse(evidence.physically_ready(None))

    def test_difficulty_cannot_increase_fitness(self):
        self.assertEqual(evaluate.fitness('feasible',1,.01,True),evaluate.fitness('feasible',1,.99,True))
        self.assertEqual(evaluate.fitness('inconclusive',0,.01,True),0)

    def test_draft_changes_invalidate_evidence(self):
        self.assertNotEqual(evidence.draft_digest({'limit':1}), evidence.draft_digest({'limit':2}))

    def test_package_uses_extension_runner(self):
        self.assertIn('python /tests/run_kforge.py grade', package.TEST_SH)
        self.assertNotIn('python -m kforge grade', package.TEST_SH)

    def test_missing_docker_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);(d/'draft.json').write_text(json.dumps({'domain':'electrical'}))
            with patch('refine.sandbox.available',return_value=False), patch('refine.refine') as call:
                with self.assertRaisesRegex(RuntimeError,'unavailable'):
                    refine.refine_drafts([d])
                call.assert_not_called()

    def test_timeout_never_assessed_as_difficulty(self):
        k=dict(ok=True,audit_ok=True,coverage=[],uncovered=[],notes='',problems=[])
        r=dict(reward=0,turns=1,grade_history=[],checks='',submitted=False,timed_out=True,elapsed_s=600,budget_s=600)
        with patch('evaluate.kit.build',return_value=k),patch('evaluate.solver.run',return_value=r),patch('evaluate.llm.review_parse') as model:
            out=evaluate.evaluate({},None,{},None,{})
        self.assertEqual(out['outcome'],'inconclusive');self.assertFalse(out['ready']);model.assert_not_called()

    def test_invalid_assets_block_before_model(self):
        domain={'input_root':'/app/input_assets'}
        with tempfile.TemporaryDirectory() as tmp, patch('kit.lint.artifacts',return_value=['missing model']), patch('kit.llm.claude_parse') as model:
            # System construction reads domain catalogue; keep this test independent of its data.
            with patch('kit.catalog.compact',return_value=''):
                out=kit.build(domain,Path(tmp),{'provided_assets':[]})
            self.assertFalse(out['ok']); model.assert_not_called()

    def test_solver_prompt_is_format_neutral(self):
        prompt=solver.SYSTEM.format(mode_note='',max_turns=40,budget_s=600)
        self.assertIn('SPICE netlist task needs a netlist',prompt)
        self.assertNotIn('Copy it to /app/output/design',prompt)


if __name__ == '__main__':
    unittest.main()

class StepExecution(unittest.TestCase):
    def test_step_selection(self):
        import run
        self.assertEqual(run.selected_steps(), ('sample',))
        self.assertEqual(run.selected_steps(['stage1']), ('stage1',))
        self.assertEqual(run.selected_steps(full=True), ('sample','stage1','evaluate','package'))
        self.assertEqual(run.selected_steps(start='review'), ('evaluate','package'))
        with self.assertRaises(ValueError):
            run.selected_steps(['stage1'], full=True)

    def test_bare_run_id_resolves_under_runs(self):
        import run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'runs' / 'run-id' / 'drafts').mkdir(parents=True)
            with patch('run.HERE', root):
                self.assertEqual(run.resolve_run_path(Path('run-id')), root / 'runs' / 'run-id')

    def test_stage1_excludes_full_task_rules(self):
        import idea_review
        text = idea_review.stage1_text(Path('SKILL.md').read_text())
        self.assertIn('Missing numbers are expected',text)
        self.assertNotIn('## Stage 2',text)
        with self.assertRaises(ValueError):
            idea_review.stage1_text('No stages here')

    def test_stage1_preserves_existing_readiness(self):
        import idea_review
        result=idea_review.IdeaReview(design_decision=True,verdict='advance',rank=8,main_reason='checkable',report_md='Review')
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp); original=json.dumps({'ready':True,'draft':{'title':'Circuit'}})
            (d/'draft.json').write_text(original)
            (d/'evaluation.json').write_text('existing evidence')
            with patch('idea_review.llm.review_parse',return_value=result) as model:
                rows=idea_review.review_drafts([d],Path('SKILL.md'))
            self.assertEqual(rows[0]['verdict'],'advance')
            self.assertEqual((d/'draft.json').read_text(),original)
            self.assertEqual((d/'evaluation.json').read_text(),'existing evidence')
            self.assertEqual(model.call_args.kwargs['purpose'],'stage1')

class GeneratorRouting(unittest.TestCase):
    def test_anthropic_generator(self):
        with patch('llm.claude_parse') as call, patch('llm.openrouter_json') as other:
            llm.generate_parse('system','prompt',object,model='claude-example')
            self.assertEqual(call.call_args.kwargs['model'],'claude-example')
            self.assertEqual(call.call_args.kwargs['purpose'],'generate')
            other.assert_not_called()

    def test_openrouter_generator(self):
        with patch('llm.openrouter_json') as call, patch('llm.claude_parse') as other:
            llm.generate_parse('system','prompt',object,model='xiaomi/mimo-v2.6-pro')
            self.assertEqual(call.call_args.kwargs['model'],'xiaomi/mimo-v2.6-pro')
            self.assertEqual(call.call_args.kwargs['max_tokens'],20000)
            self.assertEqual(call.call_args.kwargs['effort'],'low')
            self.assertEqual(call.call_args.kwargs['request_timeout_s'],300)
            self.assertEqual(call.call_args.kwargs['transport_retries'],0)
            self.assertEqual(call.call_args.kwargs['repairs'],1)
            self.assertEqual(call.call_args.kwargs['purpose'],'generate')
            other.assert_not_called()

    def test_dedicated_non_review_model_routing(self):
        with patch('llm.openrouter_json') as call:
            llm.review_parse('system', 'prompt', object, model='moonshotai/kimi-k3', purpose='adapter')
            self.assertEqual(call.call_args.kwargs['model'], 'moonshotai/kimi-k3')
            self.assertEqual(call.call_args.kwargs['purpose'], 'adapter')

    def test_generation_request_record_matches_openrouter_route(self):
        from pydantic import BaseModel

        class Output(BaseModel):
            value: str

        record = llm.generation_request_record('SYSTEM', 'USER', Output, 'xiaomi/mimo-v2.6-pro')
        self.assertEqual(record['provider'], 'openrouter')
        self.assertEqual(record['effort'], 'low')
        self.assertEqual(record['max_tokens'], 20000)
        self.assertEqual(record['messages'][1], {'role': 'user', 'content': 'USER'})
        self.assertIn('Reply with exactly one JSON object', record['messages'][0]['content'])
        self.assertEqual(record['output_schema']['title'], 'Output')

class SingleIdea(unittest.TestCase):
    def test_instruction_only_review(self):
        import idea_review
        result=idea_review.IdeaReview(design_decision=True,verdict='advance',rank=7,main_reason='plausible',report_md='Review')
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);(d/'instruction.md').write_text('Design a circuit.')
            with patch('idea_review.llm.review_parse',return_value=result):
                rows=idea_review.review_drafts([d],Path('SKILL.md'))
            self.assertEqual(len(rows),1)
            self.assertFalse((d/'draft.json').exists())
            self.assertTrue((d/'stage1_review.md').exists())


class EvaluationReplacement(unittest.TestCase):
    def test_replaces_only_executable_evaluation_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp)
            for name in refine.EVALUATION_PATHS:
                (draft / name).mkdir()
                (draft / name / 'old').write_text('old')
            for name in refine.EVALUATION_FILES:
                (draft / name).write_text('old')
            for name in ('stage1_review.json', 'stage1_review.md', 'static_review.json', 'draft.json'):
                (draft / name).write_text(name)

            refine.replace_evaluation_artifacts(draft)

            for name in (*refine.EVALUATION_PATHS, *refine.EVALUATION_FILES):
                self.assertFalse((draft / name).exists())
            for name in ('stage1_review.json', 'stage1_review.md', 'static_review.json', 'draft.json'):
                self.assertEqual((draft / name).read_text(), name)

    def test_static_review_does_not_replace_evaluation(self):
        meta = {'domain': 'electrical', 'draft': {'title': 'kept'}}
        verdict = SimpleNamespace(
            verdict='ready', reasons=[], criteria=[], fixes=[],
            astra=SimpleNamespace(p_pass_per_attempt=0.2, model_dump=lambda: {
                'p_pass_per_attempt': 0.2, 'easiest_route': '', 'where_it_fails': '', 'confidence': 'low'}))
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp)
            (draft / 'draft.json').write_text(json.dumps(meta))
            (draft / 'evaluation.json').write_text('old executable evidence')
            with patch('refine.judge.review', return_value=verdict), \
                 patch('refine.judge.verdict_record', return_value={
                     'pass': True, 'failed_rules': [], 'top_fixes': []}), \
                 patch('refine.lint.check', return_value=[]):
                refine.static_review(draft, 'skill')
            self.assertEqual((draft / 'evaluation.json').read_text(), 'old executable evidence')
            self.assertEqual(json.loads((draft / 'draft.json').read_text()), meta)
            self.assertEqual(json.loads((draft / 'static_review.json').read_text())['executable_readiness'],
                             'not assessed')
