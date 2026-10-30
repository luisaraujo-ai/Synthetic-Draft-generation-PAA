import unittest
from unittest.mock import patch
from taskgen.review import judge


class SplitJudge(unittest.TestCase):
    def test_partition_and_combine(self):
        calls = []
        def respond(system, prompt, schema, **kwargs):
            calls.append((prompt, kwargs))
            hard = len(calls) == 2
            data = dict(criteria=[dict(rule=n, verdict='pass', evidence='e', fix='')
                                  for n, _ in judge.CRITERIA if (n in judge.HARD_CRITERIA) == hard], summary='ok')
            if hard:
                data['difficulty'] = dict(easiest_route='r', where_it_fails='w', p_pass_per_attempt=.2,
                                          confidence='medium', harder_if='')
            return schema(**data)
        lim = {k: 'test' for k in ('id', 'name', 'mechanism', 'how_to_elicit', 'how_to_grade')}
        with patch.object(judge, 'draft_text', return_value='draft'), patch.object(judge.llm, 'review_parse', side_effect=respond):
            result = judge.review({}, lim)
        self.assertEqual([c.rule for c in result.criteria], [n for n, _ in judge.CRITERIA])
        self.assertTrue(judge.passes(result, .5)[0])
        self.assertIn('Prior specification findings', calls[1][0])
        self.assertTrue(all(k['max_tokens'] == 60000 for _, k in calls))

    def test_missing_criteria_rejected(self):
        lim = {k: 'test' for k in ('id', 'name', 'mechanism', 'how_to_elicit', 'how_to_grade')}
        with patch.object(judge, 'draft_text', return_value='draft'), patch.object(judge.llm, 'review_parse', return_value=judge.SpecificationReview(criteria=[], summary='')):
            with self.assertRaises(RuntimeError):
                judge.review({}, lim)

    def test_secondary_failures_and_unknown_feasibility_are_advisory(self):
        from test_pipeline import review
        rv = review(.95)
        for c in rv.criteria:
            if c.rule not in judge.MANDATORY_CRITERIA:
                c.verdict = 'unclear' if c.rule == 'feasible' else 'fail'
        self.assertEqual(judge.passes(rv, .5), (True, [], False))
        next(c for c in rv.criteria if c.rule == 'feasible').verdict = 'fail'
        self.assertEqual(judge.passes(rv, .5)[1], ['feasible'])

    def test_completed_specification_checkpoint_survives_difficulty_failure(self):
        import tempfile
        from pathlib import Path
        calls=[]
        def respond(system,prompt,schema,**kwargs):
            calls.append(kwargs['purpose'])
            hard=kwargs['purpose']=='judge-difficulty'
            if hard and calls.count('judge-difficulty')==1:raise TimeoutError('interrupted')
            value=dict(criteria=[dict(rule=n,verdict='pass',evidence='e',fix='') for n,_ in judge.CRITERIA if (n in judge.HARD_CRITERIA)==hard],summary='ok')
            if hard:value['difficulty']=dict(easiest_route='r',where_it_fails='w',p_pass_per_attempt=.2,confidence='medium',harder_if='')
            return schema(**value)
        lim={k:'test' for k in ('id','name','mechanism','how_to_elicit','how_to_grade')}
        with tempfile.TemporaryDirectory() as tmp, patch.object(judge,'draft_text',return_value='draft'), patch.object(judge.llm,'review_parse',side_effect=respond):
            with self.assertRaises(TimeoutError):judge.review({},lim,checkpoint_dir=Path(tmp))
            result=judge.review({},lim,checkpoint_dir=Path(tmp))
            self.assertTrue(judge.passes(result,.5)[0])
            self.assertEqual(calls.count('judge-specification'),1)
            self.assertEqual(calls.count('judge-difficulty'),2)
