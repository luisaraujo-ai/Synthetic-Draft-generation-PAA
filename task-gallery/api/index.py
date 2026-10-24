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

def esc(value): return html.escape(str(value),quote=True)
def config():
    password=os.environ.get('GALLERY_PASSWORD','')
    secret=os.environ.get('GALLERY_SESSION_SECRET','')
    if len(password)<16 or len(secret)<32: raise ValueError('Access configuration missing')
    return password,secret

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
    logout='<form class="logout" action="/logout" method="post"><button>Sign out</button></form>' if logged else ''
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
            self.send(503,page('Setup required','<h1>Access is not configured.</h1><p>Set the two gallery environment variables in Vercel, then redeploy.</p>'))
    def secure_cookie(self,value,maxage=28800):
        secure='' if os.environ.get('GALLERY_LOCAL_HTTP')=='1' else '; Secure'
        return f'gallery_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={maxage}'+secure
    def do_POST(self):
        cfg=self.setup_config()
        if cfg is None:return
        # Reject cross-site forms, including login CSRF. SameSite protects subsequent navigation.
        origin=self.headers.get('Origin','')
        scheme='http' if os.environ.get('GALLERY_LOCAL_HTTP')=='1' else 'https'
        if origin!=scheme+'://'+self.headers.get('Host',''):
            return self.send(403,page('Forbidden','<h1>Invalid request origin.</h1>'))
        if self.path=='/logout':return self.send(303,headers={'Location':'/login','Set-Cookie':self.secure_cookie('',0)})
        if self.path!='/login':return self.send(404)
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:return self.send(400)
        if not 0<length<=4096:return self.send(400)
        submitted=parse_qs(self.rfile.read(length).decode(errors='replace')).get('password',[''])[0]
        # Fixed-cost digest comparison, independent of password length and prefix.
        expected=hashlib.scrypt(cfg[0].encode(),salt=b'forge-gallery-login-v1',n=16384,r=8,p=1)
        actual=hashlib.scrypt(submitted.encode(),salt=b'forge-gallery-login-v1',n=16384,r=8,p=1)
        if not hmac.compare_digest(actual,expected):
            time.sleep(.5)
            return self.send(401,self.login('Incorrect access password.'))
        return self.send(303,headers={'Location':'/','Set-Cookie':self.secure_cookie(session(*cfg))})
    def login(self,error=''):
        return page('Welcome','<section class="panel narrow"><span class="tag">Private collection</span><h1>Engineering worth exploring.</h1><p class="muted">Enter your shared access password to browse the task library.</p><p>'+esc(error)+'</p><form method="post" action="/login"><label>Access password</label><input aria-label="Access password" type="password" name="password" autocomplete="current-password" required maxlength="1024" style="width:100%"><p><button>Open library →</button></p></form></section>')
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
            query=parse_qs(url.query);q=query.get('q',[''])[0];domain=query.get('domain',[''])[0]
            visible=[t for t in tasks if (not domain or t['domain']==domain) and q.lower() in (t['title']+' '+t['description']+' '+t['domain']).lower()]
            options='<option value="">Every domain</option>'+''.join('<option '+('selected ' if d==domain else '')+'value="'+esc(d)+'">'+esc(d)+'</option>' for d in sorted({t['domain'] for t in tasks}))
            body='<span class="tag">Reviewed engineering tasks</span><h1>A library of design challenges.</h1><p class="muted">'+str(len(tasks))+' selected tasks · Briefs, engineering assessments, and downloadable assets.</p><form class="toolbar"><input name="q" placeholder="Search tasks…" value="'+esc(q)+'"><select aria-label="Domain" name="domain">'+options+'</select><button>Filter</button></form><div class="grid">'
            for t in visible:
                body+='<article class="card"><div><span class="tag">'+esc(t['domain'])+'</span><span class="tag">Value '+str(t['rank'])+'/10</span></div><h2><a href="/task/'+t['key']+'">'+esc(t['title'])+'</a></h2><p>'+esc(t['description'])+'</p><a href="/task/'+t['key']+'">Explore task →</a></article>'
            body+='</div>' if visible else '</div><p>No tasks match these filters.</p>'
            return self.send(200,page('Library',body,True))
        prefix,key= (url.path.rsplit('/',1) if '/' in url.path else ('',''))
        task=next((t for t in tasks if t['key']==key),None)
        if task is None:return self.send(404,page('Not found','<h1>Task not found.</h1>',True))
        if prefix=='/download':
            data=(ROOT/'gallery_data'/f'{key}.zip').read_bytes()
            return self.send(200,data,'application/zip',{'Content-Disposition':f'attachment; filename="task-{key}.zip"'})
        if prefix!='/task':return self.send(404)
        body='<a href="/">← All tasks</a><h1>'+esc(task['title'])+'</h1><span class="tag">'+esc(task['domain'])+'</span><span class="tag">Stage 1 advance</span><p>'+esc(task['description'])+'</p><a class="button" href="/download/'+key+'">Download task ZIP ↓</a><div class="meta">'
        for label,value in [('Feasibility assessment',task['feasibility']),('Difficulty estimate',task['difficulty']),('Main concern',task['concern']),('Reference validation','Not independently established by Stage 1 review')]:
            body+='<section class="panel"><strong>'+label+'</strong><p>'+esc(value)+'</p></section>'
        body+='</div><section class="panel"><h2>Task brief</h2><pre>'+esc(task['brief'])+'</pre></section>'
        for label,value in [('Handbook',task['handbook']),('Full Stage 1 review',task['review'])]:
            body+='<details class="panel"><summary>'+label+'</summary><pre>'+esc(value)+'</pre></details>'
        return self.send(200,page(task['title'],body,True))

if __name__=='__main__':
    os.environ['GALLERY_LOCAL_HTTP']='1'
    print('Local gallery: http://localhost:8000',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8000),handler).serve_forever()
