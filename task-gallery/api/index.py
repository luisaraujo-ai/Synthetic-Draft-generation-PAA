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

def page(title,body,logged=False):
    cfg=config() if logged else ('','')
    csrf='<input type="hidden" name="csrf" value="'+session(cfg[0],cfg[1]+':csrf')+'">'
    logout='<form class="logout" action="/logout" method="post">'+csrf+'<button>Sign out</button></form>' if logged else ''
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+esc(title)+' · Task Library</title><style>'+CSS+'</style><header>'+logout+
            '<a href="/">FORGE / TASK LIBRARY</a></header><main>'+body+
            '<footer>Engineering review is an assessment. Reference validation is reported separately.</footer></main></html>').encode()

class handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass  # Never log passwords, cookies or request bodies.
    def send(self,status,body=b'',mime='text/html; charset=utf-8',headers=None):
        self.send_response(status)
        for k,v in {'Content-Type':mime,'Cache-Control':'private, no-store','Vary':'Cookie',
                    'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
                    'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    **(headers or {})}.items(): self.send_header(k,v)
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
        if self.path not in ('/login','/logout'):return self.send(404)
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:return self.send(400)
        if not 0<length<=4096:return self.send(400)
        fields=parse_qs(self.rfile.read(length).decode(errors='replace'))
        token=fields.get('csrf',[''])[0]
        if not authorized('gallery_session='+token,cfg[0],cfg[1]+':csrf'):
            return self.send(403,page('Forbidden','<h1>Please reload the login page and try again.</h1>'))
        if self.path=='/logout':return self.send(303,headers={'Location':'/login','Set-Cookie':self.secure_cookie('',0)})
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
            data=(ROOT/'gallery_data'/f'{key}.zip').read_bytes()
            return self.send(200,data,'application/zip',{'Content-Disposition':f'attachment; filename="task-{key}.zip"'})
        if prefix!='/task':return self.send(404)
        body='<a href="/">← All tasks</a><h1>'+esc(task['title'])+'</h1><span class="tag">'+esc(task['domain'])+'</span><span class="tag">Stage 1 advance</span><p>'+esc(task['description'])+'</p><a class="button" href="/download/'+key+'">Download task ZIP ↓</a><nav class="section-nav" aria-label="Task sections"><a href="#assessment">Assessment</a><a href="#brief">Task brief</a><a href="#handbook">Handbook</a><a href="#review">Full review</a></nav><p class="notice">Review estimates describe the task’s potential. They do not establish a passing reference or an Astra failure rate.</p><div class="meta" id="assessment">'
        for label,value in [('Feasibility assessment',task['feasibility']),('Difficulty estimate',task['difficulty']),('Main concern',task['concern']),('Reference validation','Not independently established by Stage 1 review')]:
            body+='<section class="panel"><strong>'+label+'</strong><p>'+esc(value)+'</p></section>'
        body+='</div><section class="panel" id="brief"><h2>Task brief</h2><pre class="document">'+esc(task['brief'])+'</pre></section>'
        for anchor,label,value in [('handbook','Handbook',task['handbook']),('review','Full Stage 1 review',task['review'])]:
            body+='<details class="panel" id="'+anchor+'"><summary>'+label+'</summary><pre class="document">'+esc(value)+'</pre></details>'
        return self.send(200,page(task['title'],body,True))

if __name__=='__main__':
    os.environ['GALLERY_LOCAL_HTTP']='1'
    print('Local gallery: http://localhost:8000',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8000),handler).serve_forever()
