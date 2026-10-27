import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from evidence import draft_digest
from export_gallery import export
from publish_drive import atomic

spec=importlib.util.spec_from_file_location('gallery',Path(__file__).resolve().parents[1]/'task-gallery/api/index.py')
web=importlib.util.module_from_spec(spec);spec.loader.exec_module(web)

class Socket:
    def __init__(self,request):self.input=io.BytesIO(request);self.output=b''
    def makefile(self,*a,**kw):return self.input
    def sendall(self,data):self.output+=data

class Gallery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); data=self.root/'gallery_data';data.mkdir()
        task={'key':'abc','id':'run/task','title':'<script>bad</script>','description':'Design','domain':'EE','rank':8,
              'feasibility':'Unverified','difficulty':'Estimate','concern':'Check corners','brief':'<img src=x>','handbook':'notes','review':'review'}
        atomic(data/'catalog.json',{'tasks':[task]});(data/'abc.zip').write_bytes(b'PK-test')
        self.env=patch.dict(os.environ,{'GALLERY_PASSWORD':'a-long-shared-test-password','GALLERY_SQLITE_PATH':str(self.root/'events.sqlite3'),'GALLERY_ADMIN_PASSWORD':'admin-test-password'},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.path=patch.object(web,'ROOT',self.root);self.path.start();self.addCleanup(self.path.stop)
    def request(self,path='/',method='GET',body='',cookie='',origin='https://example.test',csrf=True,host='example.test'):
        if method=='POST' and csrf:
            cfg=web.config();body+='&csrf='+web.session(cfg[0],cfg[1]+':csrf')
        site='cross-site' if origin=='https://evil.test' else 'same-origin'
        raw=f'{method} {path} HTTP/1.0\r\nHost: {host}\r\nSec-Fetch-Site: {site}\r\nOrigin: {origin}\r\nCookie: {cookie}\r\nContent-Length: {len(body.encode())}\r\n\r\n{body}'
        sock=Socket(raw.encode());web.handler(sock,('127.0.0.1',1000),object())
        headers,content=sock.output.split(b'\r\n\r\n',1)
        return headers.decode(),content
    def cookie(self):return 'gallery_session='+web.session(*web.config())
    def test_only_password_required_and_legacy_secret_ignored(self):
        cfg=web.config()
        with patch.dict(os.environ,{'GALLERY_SESSION_SECRET':'unused'}):
            self.assertEqual(web.config(),cfg)
        self.assertIn('200',self.request('/')[0])
    def test_protected_catalog_download_and_private_path(self):
        for path in ['/task/abc','/download/abc','/gallery_data/catalog.json','/api/index.py','/gallery_data/abc.zip']:
            headers,body=self.request(path)
            self.assertIn('303',headers);self.assertNotIn(b'PK-test',body);self.assertNotIn(b'Design',body)
    def test_login_cookie_and_download(self):
        headers,_=self.request('/login','POST','password=a-long-shared-test-password')
        self.assertIn('303',headers)
        self.assertIn('HttpOnly; SameSite=Strict',headers);self.assertIn('; Secure',headers)
        headers,body=self.request('/download/abc','POST',self.feedback(name='Alice'),cookie=self.cookie())
        self.assertIn('200',headers);self.assertIn('private, no-store',headers);self.assertEqual(body,b'PK-test')
    def test_wrong_password_and_cross_origin_denied(self):
        with patch.object(web.time,'sleep'):
            self.assertIn('401',self.request('/login','POST','password=wrong')[0])
        self.assertIn('403',self.request('/login','POST','password=a-long-shared-test-password',origin='https://evil.test')[0])
    def test_proxy_and_null_origin_with_csrf(self):
        self.assertIn('303',self.request('/login','POST','password=a-long-shared-test-password',origin='null',host='internal.vercel')[0])
        self.assertIn('403',self.request('/login','POST','password=a-long-shared-test-password',csrf=False)[0])
    def test_expiry_tampering_rotation(self):
        password,secret=web.config();token=web.session(password,secret,now=100)
        self.assertTrue(web.authorized('gallery_session='+token,password,secret,now=101))
        self.assertFalse(web.authorized('gallery_session='+token,password,secret,now=30000))
        self.assertFalse(web.authorized('gallery_session='+token+'x',password,secret,now=101))
        self.assertFalse(web.authorized('gallery_session='+token,password+'changed',secret,now=101))
    def test_html_escaped_logout_and_fail_closed(self):
        headers,body=self.request('/task/abc',cookie=self.cookie())
        self.assertIn('200',headers);self.assertIn(b'&lt;script&gt;',body);self.assertNotIn(b'<script>',body)
        self.assertIn('Max-Age=0',self.request('/logout','POST',cookie=self.cookie())[0])
        with patch.dict(os.environ,{'GALLERY_PASSWORD':''}):self.assertIn('503',self.request('/download/abc')[0])
    def test_browse_searches_assessments_and_resets_empty_results(self):
        _,body=self.request('/?q=corners',cookie=self.cookie())
        self.assertIn(b'1 of 1 tasks',body)
        self.assertIn(b'Assessment at a glance',body)
        self.assertNotIn(b'Value 8/10',body)
        self.assertIn(b'&lt;script&gt;',body)
        _,body=self.request('/?domain=Other',cookie=self.cookie())
        self.assertIn(b'0 of 1 tasks',body)
        self.assertIn(b'Show all tasks',body)

    def test_alphabetical_order_and_detail_navigation(self):
        path=self.root/'gallery_data/catalog.json'
        data=json.loads(path.read_text());first=data['tasks'][0]
        first['title']='Zebra';data['tasks'].append({**first,'title':'Alpha','key':'def','id':'run/other','rank':7})
        atomic(path,data)
        _,body=self.request('/',cookie=self.cookie())
        self.assertLess(body.index(b'Alpha'),body.index(b'Zebra'))
        _,body=self.request('/task/abc',cookie=self.cookie())
        for anchor in ('assessment','brief','handbook','review'):
            self.assertIn(('id="'+anchor+'"').encode(),body)
        self.assertIn(b'/download/abc',body)

    def feedback(self,**overrides):
        task=json.loads((self.root/'gallery_data/catalog.json').read_text())['tasks'][0]
        values={'version':web.task_version(task),'category':'General','feedback':'<script>details</script>',
                'name':'=FORMULA()','feasible':'Yes','difficulty':'Easy','hardening_hours':'3.5'}
        values.update(overrides)
        return urlencode(values)

    def admin_cookie(self):
        password=os.environ['GALLERY_ADMIN_PASSWORD']
        return self.cookie()+'; gallery_admin='+web.session(password,password+':admin')

    def test_download_requires_name_and_displays_names(self):
        headers,body=self.request('/download/abc',cookie=self.cookie())
        self.assertIn(b'Register and download ZIP',body)
        self.assertEqual(web.event_rows(),[])
        self.assertIn('400',self.request('/download/abc','POST',self.feedback(name=' '),cookie=self.cookie())[0])
        self.assertIn('403',self.request('/download/abc','POST',self.feedback(name='Alice'),cookie=self.cookie(),csrf=False)[0])
        self.assertIn('409',self.request('/download/abc','POST',self.feedback(name='Alice',version='stale'),cookie=self.cookie())[0])
        self.request('/download/abc','POST',self.feedback(name='<img src=x>'),cookie=self.cookie())
        _,body=self.request('/task/abc',cookie=self.cookie())
        self.assertIn(b'Downloaded by',body);self.assertIn(b'&lt;img src=x&gt;</strong>',body)
        self.assertIn(b'current version',body)
        self.assertEqual(web.event_rows()[0]['name'],'<img src=x>')

    def test_download_persistence_and_visitor(self):
        cookie=self.cookie()+'; gallery_visitor='+'a'*32
        headers,_=self.request('/download/abc','POST',self.feedback(name='Alice'),cookie=cookie)
        self.assertIn('gallery_visitor='+'a'*32,headers)
        rows=web.event_rows()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['kind'],'download')
        self.assertEqual(rows[0]['visitor'],'a'*32)
        self.assertEqual(rows[0]['task_id'],'run/task')
        self.assertEqual(len(rows[0]['version']),64)
        self.request('/download/missing',cookie=cookie)
        self.assertEqual(len(web.event_rows()),1)

    def test_feedback_persistence_review_and_exports(self):
        headers,_=self.request('/feedback/abc','POST',self.feedback(),cookie=self.cookie())
        self.assertIn('303',headers)
        row=web.event_rows()[0]
        self.assertEqual(row['feasible'],'Yes');self.assertEqual(row['difficulty'],'Easy')
        self.assertEqual(row['hardening_hours'],'3.5')
        _,body=self.request('/admin',cookie=self.admin_cookie())
        self.assertIn(b'&lt;script&gt;details',body);self.assertNotIn(b'<script>',body)
        self.assertIn(b'Hours to make hard: 3.5',body)
        headers,body=self.request('/admin/export.json',cookie=self.admin_cookie())
        self.assertEqual(json.loads(body)[0]['feedback'],'<script>details</script>')
        headers,body=self.request('/admin/export.csv',cookie=self.admin_cookie())
        self.assertIn(b"'=FORMULA()",body)
        self.assertIn('attachment;',headers)

    def test_feedback_auth_csrf_validation_and_version(self):
        self.assertIn('303',self.request('/feedback/abc','POST',self.feedback())[0])
        self.assertIn('403',self.request('/feedback/abc','POST',self.feedback(),cookie=self.cookie(),csrf=False)[0])
        self.assertIn('403',self.request('/feedback/abc','POST',self.feedback(),cookie=self.cookie(),origin='https://evil.test')[0])
        for overrides in ({'hardening_hours':''},{'hardening_hours':'NaN'},{'hardening_hours':'-1'},
                          {'feasible':'invalid'},{'difficulty':''},{'feedback':'x'*10001}):
            self.assertIn('400',self.request('/feedback/abc','POST',self.feedback(**overrides),cookie=self.cookie())[0])
        self.assertIn('409',self.request('/feedback/abc','POST',self.feedback(version='old'),cookie=self.cookie())[0])
        self.assertEqual(web.event_rows(),[])
        self.assertIn('303',self.request('/feedback/abc','POST',self.feedback(difficulty='Hard',hardening_hours='',feedback=''),cookie=self.cookie())[0])

    def test_admin_separate_password_and_logout(self):
        for path in ('/admin','/admin/export.csv','/admin/export.json'):
            headers,body=self.request(path,cookie=self.cookie())
            self.assertIn(b'Admin password',body);self.assertNotIn(b'event_id',body)
        with patch.object(web.time,'sleep'):
            self.assertIn('403',self.request('/admin/login','POST','password=wrong',cookie=self.cookie())[0])
        headers,_=self.request('/admin/login','POST','password=admin-test-password',cookie=self.cookie())
        self.assertIn('gallery_admin=',headers);self.assertIn('303',headers)
        headers,_=self.request('/logout','POST',cookie=self.admin_cookie())
        self.assertIn('gallery_admin=; ',headers)
        self.assertEqual(headers.count('Set-Cookie:'),2)

    def test_storage_failure_does_not_claim_feedback_saved(self):
        with patch.object(web,'record_event',side_effect=RuntimeError('private detail')):
            headers,body=self.request('/feedback/abc','POST',self.feedback(),cookie=self.cookie())
            self.assertIn('503',headers);self.assertNotIn(b'private detail',body)
            headers,body=self.request('/download/abc','POST',self.feedback(name='Alice'),cookie=self.cookie())
            self.assertIn('503',headers);self.assertNotEqual(body,b'PK-test')

    def test_export_gating_and_zip(self):
        run=self.root/'run';folder=run/'tasks/a';draft={'title':'Example','instruction_md':'Design.'};digest=draft_digest(draft)
        atomic(folder/'draft.json',{'draft':draft})
        atomic(folder/'stage1_review.json',{'draft_digest':digest,'design_decision':True,'verdict':'advance','rank':8})
        atomic(run/'novelty_selection.json',{'selected':['a'],'draft_digests':{'a':digest}})
        (folder/'instruction.md').write_text('Design.')
        dest=self.root/'export';tasks=export([run],dest)
        import zipfile
        with zipfile.ZipFile(dest/(tasks[0]['key']+'.zip')) as z:self.assertIn('instruction.md',z.namelist())
        atomic(run/'novelty_selection.json',{'selected':[],'draft_digests':{}})
        with self.assertRaises(ValueError):export([run],dest)
        self.assertEqual(len(json.loads((dest/'catalog.json').read_text())['tasks']),1)

if __name__=='__main__':unittest.main()
