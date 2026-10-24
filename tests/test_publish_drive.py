import json
import tempfile
import unittest
from pathlib import Path
from publish_drive import HEADERS, plan, publish
from evidence import draft_digest

class FakeGoogle:
    def __init__(self): self.rows=[]; self.children={}; self.uploads=0; self.fail=False
    def preflight(self, *a): return 'Tasks', list(self.rows)
    def child(self,parent,key,name,folder=False,path=None):
        self.children.setdefault((parent,key),str(len(self.children)+1))
        if path: self.uploads+=1
        return self.children[parent,key]
    def values(self,sheet,tab,method,rows,region):
        if self.fail and region!='A1:J1':
            self.fail=False
            raise RuntimeError('temporary Sheets failure')
        index=int(region.split(':')[0][1:])-1
        if index==len(self.rows): self.rows.extend(rows)
        else: self.rows[index:index+len(rows)]=rows

class Publisher(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.task=self.root/'run/tasks/a';self.task.mkdir(parents=True)
        draft={'title':'Example','instruction_md':'Build a circuit.'}
        self.digest=draft_digest(draft)
        self.write('draft.json',{'draft':draft,'domain':'electrical'})
        self.write('stage1_review.json',{'draft_digest':self.digest,'design_decision':True,'verdict':'advance','rank':8,'report_md':'What stops it:\n- Reference unverified.','main_reason':'Good idea'})
        self.write('novelty.json',{'verdict':'distinct','draft_digest':self.digest})
        (self.task/'instruction.md').write_text('Build a circuit.')
    def write(self,name,data): (self.task/name).write_text(json.dumps(data))
    def test_stale_review_cannot_publish(self):
        self.write('stage1_review.json',{'draft_digest':'old'})
        with self.assertRaisesRegex(ValueError,'stale'):plan(self.task)
    def test_deferred_novelty_needs_selection_and_digest(self):
        self.write('novelty.json',{'status':'deferred'})
        with self.assertRaises(ValueError):plan(self.task)
        with self.assertRaises(ValueError):plan(self.task,{'selected':['a']})
        item=plan(self.task,{'selected':['a'],'draft_digests':{'a':self.digest}})
        self.assertEqual(item['id'],'run/a')
    def test_repeat_upserts_one_row_and_folder(self):
        api=FakeGoogle();item=plan(self.task);state=self.root/'publication.json'
        publish(api,[item],state,'folder','sheet')
        children=dict(api.children)
        publish(api,[item],state,'folder','sheet')
        self.assertEqual(len(api.rows),2);self.assertEqual(api.rows[0],HEADERS)
        self.assertEqual(api.children,children)
        self.assertEqual(json.loads(state.read_text())['run/a']['status'],'published')
    def test_sheet_failure_resumes_after_uploaded_checkpoint(self):
        api=FakeGoogle();api.fail=True;item=plan(self.task);state=self.root/'publication.json'
        with self.assertRaises(RuntimeError):publish(api,[item],state,'folder','sheet')
        self.assertEqual(json.loads(state.read_text())['run/a']['status'],'uploaded')
        publish(api,[item],state,'folder','sheet')
        self.assertEqual(len(api.rows),2)
    def test_asset_escape_rejected(self):
        (self.task/'inputs').mkdir();(self.root/'secret').write_text('private')
        (self.task/'inputs/asset').symlink_to(self.root/'secret')
        with self.assertRaisesRegex(ValueError,'symlink'):plan(self.task)

if __name__=='__main__':unittest.main()
