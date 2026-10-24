import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import rejudge
from test_pipeline import draft, review


class Recovery(unittest.TestCase):
    def test_preserves_draft_packages_assets_and_skips_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'unjudged').mkdir()
            (root / 'entries').mkdir()
            d = draft(provided_assets=[dict(path='/app/input_assets/a.txt', content='saved asset', purpose='input')])
            meta = dict(limitation='self-consistency-only-verification', subdomain='power electronics', descriptors={})
            entry = dict(status='unjudged', title=d.title, run=root.name)
            for name, data in [('run.json', {'domain':'electrical'}), ('summary.json', {'candidates':[entry]}),
                               ('entries/00.json', entry), ('unjudged/00-attempt-1.json', {'draft':d.model_dump(),'meta':meta})]:
                (root / name).write_text(json.dumps(data))
            with patch.object(rejudge.judge, 'review', return_value=review(.2)) as call, \
                 patch.object(rejudge.Domain, 'save_ledger'), patch.object(rejudge.Domain, 'ledger', return_value=[]), \
                 patch.object(rejudge.llm, 'cost_summary', return_value={'total_usd':0,'by_purpose':{},'unpriced_calls':0}):
                rejudge.recover(root)
                rejudge.recover(root)
                self.assertEqual(call.call_count, 1)
            self.assertEqual((root/'tasks/buck/inputs/a.txt').read_text(), 'saved asset')
            self.assertEqual((root/'tasks/buck/instruction.md').read_text(), d.instruction_md)
            self.assertEqual(json.loads((root/'entries/00.json').read_text())['status'], 'accepted')
