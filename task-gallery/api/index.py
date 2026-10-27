"""Password-protected task gallery. Every route is served by this handler."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, quote
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import sqlite3
import csv
import io
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
CSS='''*{box-sizing:border-box}body{margin:0;background:#f5f6f2;color:#1c302d;font:16px/1.6 system-ui,sans-serif}header{background:#143c35;color:#fff;padding:30px max(5vw,20px)}header a{color:inherit}main{max-width:1180px;margin:40px auto;padding:0 24px}h1{font-size:clamp(30px,4vw,48px);line-height:1.15;letter-spacing:-1.5px}h2{line-height:1.3}a{color:#17674f;text-decoration:none}a:hover{text-decoration:underline}.muted{color:#61716b}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px}.card,.panel{background:white;border:1px solid #d9e1d9;border-radius:14px;padding:24px}.card{display:flex;flex-direction:column}.card h2{font-size:21px}.card p{flex:1}.tag{font-size:12px;letter-spacing:.04em;text-transform:uppercase;background:#e6eee7;padding:5px 9px;border-radius:5px;display:inline-block;margin-right:5px}input,select,button{font:inherit;border:1px solid #b9cbc2;border-radius:7px;padding:10px 14px}button,.button{background:#164f40;color:white;border:0;padding:11px 18px;border-radius:7px;display:inline-block;cursor:pointer}.toolbar{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0}.toolbar input{flex:1;min-width:200px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace}.narrow{max-width:440px;margin:10vh auto}details{margin:20px 0}summary{cursor:pointer;font-weight:600}.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin:24px 0}footer{padding:30px 0;color:#61716b;font-size:13px}.logout{float:right}.logout button{background:transparent;color:white;border:1px solid #7b9d8d}label{display:block;margin:12px 0}'''

CSS += """
body{background:#f3f5f7;color:#24332f}header{padding:20px max(5vw,20px);background:#163c35}header>a{font-weight:750;letter-spacing:.12em;font-size:12px}main{max-width:1280px}h1{max-width:950px}h2{letter-spacing:-.4px}.intro{max-width:730px;font-size:18px}.grid{grid-template-columns:repeat(auto-fit,minmax(320px,1fr));align-items:start}.card{padding:26px;box-shadow:0 3px 12px #143c3505}.card h2{margin:16px 0 12px}.card h2 a{color:#203d34}.card:hover{border-color:#8da99a}.card p{margin-top:0}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.1em;font-weight:700;color:#557568}.tag{font-size:11px;letter-spacing:.02em}.preview{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.card details{border-top:1px solid #e5eae7;padding-top:14px;font-size:14px;margin:16px 0}.card details p{margin:12px 0}.card-actions{display:flex;justify-content:space-between;gap:12px;font-size:14px;font-weight:600;margin-top:18px}.toolbar{padding:18px;background:white;border:1px solid #d9e1d9;border-radius:12px;align-items:end}.toolbar label{margin:0;flex:1;min-width:180px;font-size:12px;font-weight:650;color:#61716b}.toolbar input,.toolbar select{display:block;width:100%;margin-top:5px;min-width:0;background:#fff;color:#24332f}.results{display:flex;justify-content:space-between;align-items:center;margin:22px 0}.section-nav{display:flex;flex-wrap:wrap;gap:20px;padding:16px 0;margin:24px 0;border-top:1px solid #d9e1d9;border-bottom:1px solid #d9e1d9}.notice{border-left:3px solid #a8b8ac;padding:12px 18px;background:#eaf0ec;font-size:14px}.meta .panel{padding:22px}.meta strong{font-size:13px;color:#557568}.meta p{font-size:15px}.document{font:16px/1.8 system-ui,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere}.empty{text-align:center;padding:50px}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #bf8625;outline-offset:4px}section,details{scroll-margin-top:20px}@media(max-width:600px){main{padding:0 16px;margin-top:28px}.grid{grid-template-columns:1fr}.toolbar{display:block}.toolbar label{margin-bottom:14px}.toolbar button{width:100%}.meta{grid-template-columns:1fr}.logout button{padding:7px 12px}.card{padding:20px}h1{letter-spacing:-1px}}
"""

def esc(value): return html.escape(str(value),quote=True)
def config():
    password=os.environ.get('GALLERY_PASSWORD','')
    if not password: raise ValueError('GALLERY_PASSWORD is missing')
    # Derive the internal cookie key from the password; no second setting required.
    key=hashlib.scrypt(password.encode(),salt=b'forge-gallery-session-v2',n=16384,r=8,p=1).hex()
    return password,key

def signature(payload,password,secret):
    key=hashlib.sha256((secret+'\0'+password).encode()).digest()
    return hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()

def session(password,secret,now=None):
    payload=str(int(now if now is not None else time.time())+8*3600)+'.'+secrets.token_hex(16)
    return payload+'.'+signature(payload,password,secret)

def authorized(cookie,password,secret,now=None):
    try:
        jar=SimpleCookie();jar.load(cookie or '')
        token=jar['gallery_session'].value
        expiry,nonce,sig=token.split('.')
        current=now if now is not None else time.time()
        return current<int(expiry)<=current+8*3600 and hmac.compare_digest(sig,signature(expiry+'.'+nonce,password,secret))
    except (ValueError,KeyError):return False

COLUMNS = ('event_id','kind','task_key','task_id','task_title','version','created_at','visitor','name','category','feedback','feasible','difficulty','hardening_hours')
SCHEMA = "CREATE TABLE IF NOT EXISTS gallery_events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, task_key TEXT NOT NULL, task_id TEXT NOT NULL, task_title TEXT NOT NULL, version TEXT NOT NULL, created_at TEXT NOT NULL, visitor TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, feedback TEXT NOT NULL, feasible TEXT NOT NULL, difficulty TEXT NOT NULL, hardening_hours TEXT NOT NULL)"
CATEGORIES = ('General', 'Unclear instructions', 'Technical error', 'Difficulty', 'Other')

@contextmanager
def database():
    url=os.environ.get('GALLERY_DATABASE_URL','')
    if url:
        import psycopg
        conn=psycopg.connect(url,connect_timeout=5)
        placeholder='%s'
    else:
        path=os.environ.get('GALLERY_SQLITE_PATH','')
        if not path or os.environ.get('VERCEL'):
            raise RuntimeError('Persistent storage is not configured')
        conn=sqlite3.connect(path,timeout=5)
        placeholder='?'
    try:
        conn.execute(SCHEMA)
        yield conn,placeholder
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:conn.close()

def task_version(task):
    versioned=dict(task)
    if not versioned.get('bundle_digest'):
        versioned['bundle_digest']=hashlib.sha256((ROOT/'gallery_data'/f"{task['key']}.zip").read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(versioned,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def visitor_id(cookie):
    jar=SimpleCookie()
    try:
        jar.load(cookie or '')
        value=jar['gallery_visitor'].value
        if len(value)==32 and all(c in '0123456789abcdef' for c in value):return value
    except (KeyError,ValueError):pass
    return secrets.token_hex(16)

def record_event(kind,task,visitor,fields=None):
    fields=fields or {}
    values=(secrets.token_hex(16),kind,task['key'],task['id'],task['title'],task_version(task),
            datetime.now(timezone.utc).isoformat(),visitor,fields.get('name',''),fields.get('category',''),fields.get('feedback',''),fields.get('feasible',''),fields.get('difficulty',''),fields.get('hardening_hours',''))
    with database() as (conn,mark):
        conn.execute('INSERT INTO gallery_events ('+','.join(COLUMNS)+') VALUES ('+','.join([mark]*len(values))+')',values)

def event_rows():
    with database() as (conn,_):
        return [dict(zip(COLUMNS,row)) for row in conn.execute('SELECT '+','.join(COLUMNS)+' FROM gallery_events ORDER BY created_at DESC').fetchall()]

def admin_authorized(cookie):
    password=os.environ.get('GALLERY_ADMIN_PASSWORD','')
    jar=SimpleCookie()
    try:
        jar.load(cookie or '')
        token=jar['gallery_admin'].value
    except (KeyError,ValueError):return False
    return bool(password) and authorized('gallery_session='+token,password,password+':admin')

def form_token():
    cfg=config()
    return '<input type="hidden" name="csrf" value="'+session(cfg[0],cfg[1]+':csrf')+'">'


def page(title,body,logged=False):
    cfg=config() if logged else ('','')
    csrf='<input type="hidden" name="csrf" value="'+session(cfg[0],cfg[1]+':csrf')+'">'
    logout='<form class="logout" action="/logout" method="post">'+csrf+'<button>Sign out</button></form>' if logged else ''
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+esc(title)+' · Task Library</title><style>'+CSS+'</style><header>'+logout+
            '<a href="/">FORGE / TASK LIBRARY</a></header><main>'+body+
            '<footer><a href="/admin">Gallery administration</a> · Downloads are registered by name. Engineering review is an assessment. Reference validation is reported separately.</footer></main></html>').encode()

class handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass  # Never log passwords, cookies or request bodies.
    def send(self,status,body=b'',mime='text/html; charset=utf-8',headers=None):
        self.send_response(status)
        for k,v in {'Content-Type':mime,'Cache-Control':'private, no-store','Vary':'Cookie',
                    'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    **(headers or {})}.items():
            for value in (v if isinstance(v,list) else [v]):self.send_header(k,value)
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def setup_config(self):
        try:return config()
        except ValueError:
            self.send(503,page('Setup required','<h1>Access is not configured.</h1><p>Set GALLERY_PASSWORD in Vercel for this deployment environment, then redeploy.</p>'))
    def secure_cookie(self,value,maxage=28800):
        secure='' if os.environ.get('GALLERY_LOCAL_HTTP')=='1' else '; Secure'
        return f'gallery_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={maxage}'+secure
    def do_POST(self):
        cfg=self.setup_config()
        if cfg is None:return
        # A signed form token works across Vercel proxy hosts and browsers that
        # send Origin: null under a strict referrer policy. Never trust forwarded hosts.
        if self.headers.get('Sec-Fetch-Site')=='cross-site':
            return self.send(403,page('Forbidden','<h1>Please open the login page again.</h1>'))
        if self.path not in ('/login','/logout','/admin/login') and not self.path.startswith(('/feedback/','/download/')):return self.send(404)
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:return self.send(400)
        if not 0<length<=131072:return self.send(400)
        fields=parse_qs(self.rfile.read(length).decode(errors='replace'))
        token=fields.get('csrf',[''])[0]
        if not authorized('gallery_session='+token,cfg[0],cfg[1]+':csrf'):
            return self.send(403,page('Forbidden','<h1>Please reload the login page and try again.</h1>'))
        if self.path.startswith(('/feedback/','/download/')) or self.path=='/admin/login':
            if not authorized(self.headers.get('Cookie'),*cfg):return self.send(303,headers={'Location':'/login'})
            if self.path=='/admin/login':
                password=os.environ.get('GALLERY_ADMIN_PASSWORD','')
                submitted=fields.get('password',[''])[0]
                if not password or not hmac.compare_digest(hashlib.sha256(password.encode()).digest(),hashlib.sha256(submitted.encode()).digest()):
                    time.sleep(.5)
                    return self.send(403,page('Forbidden','<h1>Incorrect admin password.</h1>',True))
                cookie=self.secure_cookie(session(password,password+':admin')).replace('gallery_session=','gallery_admin=')
                return self.send(303,headers={'Location':'/admin','Set-Cookie':cookie})
            key=self.path.rsplit('/',1)[-1]
            try:task=next((t for t in json.loads((ROOT/'gallery_data/catalog.json').read_text())['tasks'] if t['key']==key),None)
            except (OSError,ValueError):return self.send(503)
            if task is None:return self.send(404)
            if self.path.startswith('/download/'):
                name=fields.get('name',[''])[0].strip()
                if not 1<=len(name)<=120 or any(ord(c)<32 for c in name):
                    return self.send(400,page('Name required','<h1>Enter your name (1–120 characters).</h1>',True))
                if fields.get('version',[''])[0]!=task_version(task):
                    return self.send(409,page('Task changed','<h1>The task changed. Open its download page again.</h1>',True))
                try:data=(ROOT/'gallery_data'/f'{key}.zip').read_bytes()
                except OSError:return self.send(503,page('Unavailable','<h1>The download is unavailable.</h1>',True))
                visitor=visitor_id(self.headers.get('Cookie'))
                try:record_event('download',task,visitor,{'name':name})
                except Exception:return self.send(503,page('Download unavailable','<h1>Your download could not be registered.</h1><p>Please try again later.</p>',True))
                return self.send(200,data,'application/zip',{'Content-Disposition':f'attachment; filename="task-{key}.zip"',
                    'Set-Cookie':self.secure_cookie(visitor,31536000).replace('gallery_session=','gallery_visitor=')})
            values={k:fields.get(k,[''])[0].strip() for k in ('name','category','feedback','version','feasible','difficulty','hardening_hours')}
            if not 0<=len(values['feedback'])<=10000 or len(values['name'])>120 or values['category'] not in CATEGORIES:
                return self.send(400,page('Invalid feedback','<h1>Enter feedback of up to 10,000 characters, a name up to 120 characters, and a listed category.</h1>',True))
            if values['feasible'] not in ('Yes','No','Unsure') or values['difficulty'] not in ('Easy','Moderate','Hard','Unsure'):
                return self.send(400,page('Missing answers','<h1>Please answer the feasibility and difficulty questions.</h1>',True))
            hours=values['hardening_hours']
            try:
                if hours:
                    import decimal
                    amount=decimal.Decimal(hours)
                    if not amount.is_finite() or not 0<=amount<=10000:raise ValueError()
                elif values['difficulty']=='Easy':raise ValueError()
            except (ValueError,ArithmeticError):
                return self.send(400,page('Invalid hours','<h1>For an easy task, estimate the hours needed to make it hard (0–10,000).</h1>',True))
            if values['version']!=task_version(task):
                return self.send(409,page('Task changed','<h1>This task has changed. Copy your feedback, reload the task, and submit again.</h1>',True))
            visitor=visitor_id(self.headers.get('Cookie'))
            try:record_event('feedback',task,visitor,values)
            except Exception:return self.send(503,page('Feedback unavailable','<h1>Feedback was not saved.</h1><p>Please keep a copy and try again later.</p>',True))
            cookie=self.secure_cookie(visitor,31536000).replace('gallery_session=','gallery_visitor=')
            return self.send(303,headers={'Location':'/task/'+key+'?feedback=saved#feedback','Set-Cookie':cookie})
        if self.path=='/logout':return self.send(303,headers={'Location':'/login','Set-Cookie':[self.secure_cookie('',0),self.secure_cookie('',0).replace('gallery_session=','gallery_admin=')]})
        submitted=fields.get('password',[''])[0]
        # Fixed-cost digest comparison, independent of password length and prefix.
        expected=hashlib.scrypt(cfg[0].encode(),salt=b'forge-gallery-login-v1',n=16384,r=8,p=1)
        actual=hashlib.scrypt(submitted.encode(),salt=b'forge-gallery-login-v1',n=16384,r=8,p=1)
        if not hmac.compare_digest(actual,expected):
            time.sleep(.5)
            return self.send(401,self.login('Incorrect access password.'))
        return self.send(303,headers={'Location':'/','Set-Cookie':self.secure_cookie(session(*cfg))})
    def login(self,error=''):
        cfg=config()
        csrf=session(cfg[0],cfg[1]+':csrf')
        return page('Welcome','<section class="panel narrow"><span class="tag">Private collection</span><h1>Engineering worth exploring.</h1><p class="muted">Enter your shared access password to browse the task library.</p><p>'+esc(error)+'</p><form method="post" action="/login"><input type="hidden" name="csrf" value="'+csrf+'"><label>Access password</label><input aria-label="Access password" type="password" name="password" autocomplete="current-password" required maxlength="1024" style="width:100%"><p><button>Open library →</button></p></form></section>')
    def do_GET(self):
        cfg=self.setup_config()
        if cfg is None:return
        url=urlsplit(self.path)
        if not authorized(self.headers.get('Cookie'),*cfg):
            if url.path in ('/','/login'):return self.send(200,self.login())
            return self.send(303,headers={'Location':'/login'})
        if url.path=='/login':return self.send(303,headers={'Location':'/'})
        if url.path in ('/admin','/admin/export.json','/admin/export.csv'):
            if not admin_authorized(self.headers.get('Cookie')):
                return self.send(200,page('Admin','<section class="panel narrow"><h1>Review downloads and feedback</h1><form method="post" action="/admin/login">'+form_token()+'<label>Admin password<input type="password" name="password" required></label><button>Open review</button></form></section>',True))
            try:rows=event_rows()
            except Exception:return self.send(503,page('Storage unavailable','<h1>Review storage is unavailable.</h1>',True))
            if url.path.endswith('.json'):
                return self.send(200,json.dumps(rows,ensure_ascii=False).encode(),'application/json',{'Content-Disposition':'attachment; filename="gallery-events.json"'})
            if url.path.endswith('.csv'):
                output=io.StringIO();writer=csv.writer(output);writer.writerow(COLUMNS)
                # Neutralize spreadsheet formulas in user-controlled cells.
                for row in rows:writer.writerow([("'"+str(row[k])) if str(row[k]).lstrip().startswith(('=','+','-','@')) else row[k] for k in COLUMNS])
                return self.send(200,output.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="gallery-events.csv"'})
            counts={}
            for row in rows:
                if row['kind']=='download':
                    k=(row['task_key'],row['version'],row['task_title']);counts[k]=counts.get(k,0)+1
            body='<h1>Downloads and feedback</h1><p><a href="/admin/export.csv">Export CSV</a> · <a href="/admin/export.json">Export JSON</a></p><h2>Download requests</h2>'
            for (key,version,title),count in counts.items():body+='<p>'+esc(title)+' · '+str(count)+' requests · version '+esc(version[:12])+'</p>'
            body+='<h2>Download history</h2>'
            for row in rows:
                if row['kind']=='download':body+='<p>'+esc(row['task_title'])+' · '+esc(row['name'])+' · '+esc(row['created_at'])+' · version '+esc(row['version'][:12])+'</p>'
            body+='<h2>Feedback</h2>'
            for row in rows:
                if row['kind']=='feedback':body+='<section class="panel"><h3>'+esc(row['task_title'])+'</h3><p>'+esc(row['created_at'])+' · '+esc(row['name'] or 'Anonymous')+' · '+esc(row['category'])+' · version '+esc(row['version'][:12])+'</p><p>Feasible: '+esc(row['feasible'])+' · Difficulty: '+esc(row['difficulty'])+' · Hours to make hard: '+esc(row['hardening_hours'] or 'Not specified')+'</p><pre>'+esc(row['feedback'])+'</pre></section>'
            if not rows:body+='<p>No activity recorded yet.</p>'
            return self.send(200,page('Review',body,True))
        try: catalog=json.loads((ROOT/'gallery_data/catalog.json').read_text())
        except (FileNotFoundError,ValueError):return self.send(503,page('Unavailable','<h1>The task catalog is unavailable.</h1>'))
        tasks=catalog['tasks']
        if url.path=='/':
            query=parse_qs(url.query);q=query.get('q',[''])[0].strip();domain=query.get('domain',[''])[0]
            sort=query.get('sort',['title'])[0]
            visible=[t for t in tasks if (not domain or t['domain']==domain) and q.casefold() in ' '.join(str(t.get(k,'')) for k in ('title','description','domain','id','feasibility','difficulty','concern')).casefold()]
            visible.sort(key=lambda t: ((t['domain'].casefold(),t['title'].casefold()) if sort=='domain' else (t['title'].casefold(),t['id'])))
            options='<option value="">Every domain</option>'+''.join('<option '+('selected ' if d==domain else '')+'value="'+esc(d)+'">'+esc(d)+'</option>' for d in sorted({t['domain'] for t in tasks}))
            sorts=''.join('<option value="'+v+'" '+('selected' if sort==v else '')+'>'+label+'</option>' for v,label in [('title','Title A–Z'),('domain','Domain, then title')])
            body='<div class="eyebrow">Engineering task collection</div><h1>Find your next design challenge.</h1><p class="intro muted">Browse the problem, assess the trade-offs, and inspect the review before downloading.</p><form class="toolbar"><label>Search tasks and assessments<input type="search" name="q" placeholder="Try: piezo, settling, leakage…" value="'+esc(q)+'"></label><label>Domain<select name="domain">'+options+'</select></label><label>Sort by<select name="sort">'+sorts+'</select></label><button>Apply filters</button></form><div class="results"><strong>'+str(len(visible))+' of '+str(len(tasks))+' tasks</strong><a href="/">Reset filters</a></div><div class="grid">'
            for t in visible:
                link='/task/'+quote(t['key'],safe='')
                body+='<article class="card"><span class="tag">'+esc(t['domain'])+'</span><h2><a href="'+link+'">'+esc(t['title'])+'</a></h2><p class="preview">'+esc(t['description'])+'</p><details><summary>Assessment at a glance</summary>'
                for label,field in [('Feasibility','feasibility'),('Difficulty','difficulty'),('Main concern','concern')]:
                    body+='<p><strong>'+label+'</strong><br>'+esc(t.get(field,'Not reported'))+'</p>'
                body+='</details><div class="card-actions"><a href="'+link+'">Read task →</a><a href="/download/'+quote(t['key'],safe='')+'" aria-label="Download '+esc(t['title'])+'">Download ZIP ↓</a></div></article>'
            body+='</div>'
            if not visible:body+='<section class="panel empty"><h2>No matching tasks</h2><p>Try a broader term or choose every domain.</p><a href="/">Show all tasks →</a></section>'
            return self.send(200,page('Library',body,True))
        prefix,key= (url.path.rsplit('/',1) if '/' in url.path else ('',''))
        task=next((t for t in tasks if t['key']==key),None)
        if task is None:return self.send(404,page('Not found','<h1>Task not found.</h1>',True))
        if prefix=='/download':
            body='<a href="/task/'+key+'">← Back to task</a><section class="panel narrow"><h1>Download '+esc(task['title'])+'</h1><p>Enter your name so other gallery members can see who has downloaded this task. Your name and download date will appear on the task page.</p><form method="post" action="/download/'+key+'">'+form_token()+'<input type="hidden" name="version" value="'+task_version(task)+'"><label>Your name<input name="name" required maxlength="120" autocomplete="name"></label><button>Register and download ZIP</button></form></section>'
            return self.send(200,page('Download task',body,True))
        if prefix!='/task':return self.send(404)
        body='<a href="/">← All tasks</a><h1>'+esc(task['title'])+'</h1><span class="tag">'+esc(task['domain'])+'</span><span class="tag">Stage 1 advance</span><p>'+esc(task['description'])+'</p><a class="button" href="/download/'+key+'">Download task ZIP ↓</a><nav class="section-nav" aria-label="Task sections"><a href="#assessment">Assessment</a><a href="#brief">Task brief</a><a href="#handbook">Handbook</a><a href="#review">Full review</a><a href="#downloaders">Downloaded by</a><a href="#feedback">Feedback</a></nav><p class="notice">Review estimates describe the task’s potential. They do not establish a passing reference or an Astra failure rate.</p><div class="meta" id="assessment">'
        for label,value in [('Feasibility assessment',task['feasibility']),('Difficulty estimate',task['difficulty']),('Main concern',task['concern']),('Reference validation','Not independently established by Stage 1 review')]:
            body+='<section class="panel"><strong>'+label+'</strong><p>'+esc(value)+'</p></section>'
        body+='</div><section class="panel" id="brief"><h2>Task brief</h2><pre class="document">'+esc(task['brief'])+'</pre></section>'
        for anchor,label,value in [('handbook','Handbook',task['handbook']),('review','Full Stage 1 review',task['review'])]:
            body+='<details class="panel" id="'+anchor+'"><summary>'+label+'</summary><pre class="document">'+esc(value)+'</pre></details>'
        body+='<section class="panel" id="downloaders"><h2>Downloaded by</h2><p>Check with these people before starting overlapping work. A download does not confirm that someone is still working on the task. Names are self-reported.</p>'
        try:
            with database() as (conn,mark):
                downloads=conn.execute('SELECT name, MAX(created_at), version FROM gallery_events WHERE kind='+mark+' AND task_key='+mark+' GROUP BY name, version ORDER BY MAX(created_at) DESC',('download',key)).fetchall()
            if not downloads:body+='<p>No downloads registered yet.</p>'
            for name,created,version in downloads:
                body+='<p><strong>'+esc(name)+'</strong> · '+esc(created[:10])+' · '+('current version' if version==task_version(task) else 'earlier version')+'</p>'
        except Exception:body+='<p>Download history is temporarily unavailable.</p>'
        body+='</section>'
        body+='<section class="panel" id="feedback"><h2>Task feedback</h2><p>Paste your feedback to help improve future tasks. Your name is optional for feedback. Answers and pasted feedback are visible only to the gallery administrator. A browser ID links submissions from this browser.</p>'
        if parse_qs(url.query).get('feedback')==['saved']:body+='<p role="status">Thank you. Your feedback was saved.</p>'
        body+='<form method="post" action="/feedback/'+key+'">'+form_token()+'<input type="hidden" name="version" value="'+task_version(task)+'"><label>Name (optional)<input name="name" maxlength="120"></label><label>Category<select name="category">'+''.join('<option>'+c+'</option>' for c in CATEGORIES)+'</select></label><label>Is this task feasible?<select name="feasible" required><option value="">Choose an answer</option><option>Yes</option><option>No</option><option>Unsure</option></select></label><label>How hard is this task?<select name="difficulty" required><option value="">Choose an answer</option><option>Easy</option><option>Moderate</option><option>Hard</option><option>Unsure</option></select></label><label>If easy, how many hours of work would make it hard?<input type="number" name="hardening_hours" min="0" max="10000" step="any"></label><p>Hours are required if you select Easy.</p><label>Additional feedback (optional)<textarea name="feedback" rows="10" maxlength="10000" style="display:block;width:100%;font:inherit"></textarea></label><button>Submit feedback</button></form></section>'
        return self.send(200,page(task['title'],body,True))

if __name__=='__main__':
    os.environ['GALLERY_LOCAL_HTTP']='1'
    os.environ.setdefault('GALLERY_SQLITE_PATH',str(ROOT/'gallery-events.sqlite3'))
    print('Local gallery: http://localhost:8000',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8000),handler).serve_forever()
