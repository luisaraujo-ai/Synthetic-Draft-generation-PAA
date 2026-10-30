import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from taskgen.pipeline import batch_novelty
from taskgen.review.novelty import NoveltyReview
from taskgen.pipeline.storage import atomic, read_json as read
from taskgen.pipeline.evidence import draft_digest

class Batch(unittest.TestCase):
    def test_rank_order_manifest_and_cached_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)/'run'
            for name,rank in [('a',7),('b',9)]:
                d={'title':name,'instruction_md':'Design a circuit'}
                folder=run/'tasks'/name
                atomic(folder/'draft.json',{'draft':d})
                atomic(folder/'stage1_review.json',{'verdict':'advance','design_decision':True,'rank':rank,'draft_digest':draft_digest(d)})
            def review(doc,catalog,**kw):
                return NoveltyReview(verdict='duplicate' if catalog else 'distinct',matches=[catalog[0]['id']] if catalog else [],evidence='comparison',change_needed='')
            with patch.object(batch_novelty.novelty,'review',side_effect=review) as call,patch.object(batch_novelty.llm,'cost_summary',return_value={}):
                batch_novelty.select([run]);self.assertEqual(call.call_count,2)
                batch_novelty.select([run]);self.assertEqual(call.call_count,2)
            self.assertEqual(read(run/'novelty_selection.json')['selected'],['b'])

if __name__=='__main__':unittest.main()
