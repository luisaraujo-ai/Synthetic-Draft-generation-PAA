"""Offline tests only. No API calls or real process pools are required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from taskgen.pipeline.parallel.outputs import catalog_for
from taskgen.pipeline.parallel.queue import Queue
from taskgen.pipeline.parallel.transitions import finish, next_attempt
from taskgen.review import judge, novelty
from test_pipeline import draft, review

parallel = SimpleNamespace(Queue=Queue, finish=finish, next_attempt=next_attempt, catalog_for=catalog_for)


CONFIG = dict(domain='electrical', retries=2, transport_retries=1)
class DomainStub:
    def get(self, key, default): return default


def data():
    return dict(attempt=1, pick=dict(subdomain='power electronics',limitation='x'),
                concept=dict(device='regulator'), concept_reserved=True, draft=draft().model_dump())


class ParallelState(unittest.TestCase):
    def test_atomic_claim_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=parallel.Queue(Path(tmp)/'q.sqlite')
            q.put(0,'judge_pending',data())
            self.assertIsNotNone(q.claim(0,'judge'))
            self.assertIsNone(q.claim(0,'judge'))
            q.db.close()
            q=parallel.Queue(Path(tmp)/'q.sqlite');q.recover()
            self.assertEqual(q.rows()[0][1],'judge_pending')
            self.assertEqual(q.rows()[0][2]['draft'],data()['draft'])
            q.db.close()

    def test_cost_deduplicates_receipts_and_cached_stage_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=parallel.Queue(Path(tmp)/'q.sqlite')
            record=dict(call_id='call-a',model='m',purpose='judge',input=10,output=2,cache_read=0,usd=.1)
            for token in ['one','one','two']:
                q.account(dict(token=token,records=[record]))
            self.assertEqual(q.cost()['total_usd'],.1)
            q.db.close()

    def test_failed_judge_preserves_draft_and_never_regenerates(self):
        d=data();before=d['draft']
        bad=dict(ok=False,error='timeout')
        self.assertEqual(parallel.finish('judge',d,bad,CONFIG,DomainStub()),'judge_pending')
        self.assertEqual(parallel.finish('judge',d,bad,CONFIG,DomainStub()),'unjudged')
        self.assertEqual(d['attempt'],1)
        self.assertEqual(d['draft'],before)
        self.assertEqual(d['resume_state'],'judge_pending')

    def test_acceptance_requires_final_novelty(self):
        d=data()
        self.assertEqual(parallel.finish('judge',d,dict(ok=True,result={'review':review(.2).model_dump()}),CONFIG,DomainStub()),'final_novelty_pending')
        good=dict(ok=True,result={'novelty':dict(verdict='distinct',matches=[],evidence='different physical decision',change_needed='')})
        self.assertEqual(parallel.finish('final_novelty',d,good,CONFIG,DomainStub()),'accepted')

    def test_duplicate_requires_new_concept_and_consumes_retry(self):
        d=data()
        bad=dict(ok=True,result={'novelty':dict(verdict='duplicate',matches=['old'],evidence='same circuit and mistake',change_needed='different mechanism')})
        self.assertEqual(parallel.finish('final_novelty',d,bad,CONFIG,DomainStub()),'concept_pending')
        self.assertNotIn('concept',d)
        self.assertNotIn('draft',d)
        self.assertEqual(d['attempt'],2)

    def test_serial_novelty_catalog_includes_last_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=parallel.Queue(Path(tmp)/'q.sqlite')
            q.put(0,'accepted',data());q.put(1,'final_novelty_pending',data())
            catalog=parallel.catalog_for('final_novelty',1,q,[],Path(tmp))
            self.assertEqual(len(catalog),1)
            self.assertIn('regulator',json.dumps(catalog).lower())
            q.db.close()

    def test_unclear_novelty_waits_for_review_not_rejection(self):
        d=data()
        unclear=dict(ok=True,result={'novelty':dict(verdict='unclear',matches=[],evidence='insufficient evidence',change_needed='')})
        self.assertEqual(parallel.finish('final_novelty',d,unclear,CONFIG,DomainStub()),'unjudged')
        self.assertEqual(d['resume_state'],'final_novelty_pending')

    def test_exhausted_revisions_preserve_last_draft(self):
        d=data();d['attempt']=3
        state=parallel.next_attempt(d,CONFIG,'mandatory defect')
        self.assertEqual(state,'rejected')
        self.assertIn('draft',d)


class Novelty(unittest.TestCase):
    def test_shortlist_is_deterministic(self):
        docs=[{'id':'a','brief':'regulator cable warm start'}, {'id':'b','brief':'oscillator phase noise'}]
        self.assertEqual(novelty.shortlist({'brief':'cable regulator'},docs,count=1)[0]['id'],'a')

    def test_no_prior_tasks_skips_model_call(self):
        with patch.object(novelty.llm,'review_parse') as call:
            self.assertEqual(novelty.review({'id':'new'},[]).verdict,'distinct')
            call.assert_not_called()

    def test_hallucinated_comparison_rejected(self):
        result=novelty.NoveltyReview(verdict='duplicate',matches=['invented'],evidence='same',change_needed='change')
        with patch.object(novelty.llm,'review_parse',return_value=result):
            with self.assertRaises(RuntimeError):novelty.review({'id':'new'},[{'id':'real','brief':'regulator'}])

class Budget(unittest.TestCase):
    def test_reservations_bound_dispatch_and_release_on_known_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=parallel.Queue(Path(tmp)/'q.sqlite')
            self.assertTrue(q.reserve('a','draft',5))
            self.assertFalse(q.reserve('b','draft',5))
            q.settle('a',dict(ok=True,records=[]))
            self.assertTrue(q.reserve('b','draft',5))
            q.db.close()

    def test_prior_spend_and_unknown_failures_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=parallel.Queue(Path(tmp)/'q.sqlite')
            q.account(dict(token='prior',records=[dict(call_id='old',model='m',purpose='judge',input=0,output=0,cache_read=0,usd=47)]))
            self.assertFalse(q.reserve('a','draft',50))
            self.assertTrue(q.reserve('b','judge',50))
            q.settle('b',dict(ok=False,records=[]))
            self.assertEqual(q.budget_usage()['committed_usd'],48)
            q.db.close()

    def test_skipping_novelty_routes_directly_to_drafting_and_acceptance(self):
        config={**CONFIG,'skip_novelty':True}
        d=data()
        self.assertEqual(parallel.finish('concept',d,dict(ok=True,result={'concept':d['concept']}),config,DomainStub()),'draft_pending')
        self.assertEqual(parallel.finish('judge',d,dict(ok=True,result={'review':review(.2).model_dump()}),config,DomainStub()),'accepted')
