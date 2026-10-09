#!/usr/bin/env python3
"""Qwen Studio gateway: login-protected UI + Bearer-key-protected OpenAI-compatible API.
Runs on the Kaggle kernel in front of local Ollama; cloudflared tunnels to THIS server.
Usage: gateway.py <auth.json> <chat.html> [port]
"""
import json, hashlib, hmac, time, sys, os, threading, signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import requests

CONF = json.load(open(sys.argv[1], encoding='utf-8'))
UI = open(sys.argv[2], encoding='utf-8').read().encode('utf-8')
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8000
OLLAMA = os.environ.get('GATEWAY_UPSTREAM', 'http://127.0.0.1:11434')
CORS = [('Access-Control-Allow-Origin', '*'),
        ('Access-Control-Allow-Headers', 'Authorization, Content-Type, ngrok-skip-browser-warning'),
        ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')]

# ---------------------------------------------------------------- dead-man switch
# The desktop app arms this with POST /watchdog {"ttl": seconds}. While armed, the kernel
# shuts itself down if it hears nothing for ttl seconds - the only protection against the
# controlling app being force-killed (TerminateProcess cannot be intercepted client-side).
# Real API traffic also counts as a heartbeat, so an active chat session is never killed.
VOICE_CACHE = None
# One-shot sign-in tickets for the desktop app: it holds the API key, so it can trade it
# for a 60-second single-use ticket and hand that to the embedded browser. Keeps the
# long-lived key out of URLs and tunnel logs.
TICKETS = {}          # ticket -> expiry timestamp
WATCH = {'armed': False, 'ttl': 1800, 'last_beat': 0.0, 'last_req': 0.0}
STOP_FILE = os.environ.get('GATEWAY_STOP_FILE', '/kaggle/tmp/STOP')

def touch_request():
    WATCH['last_req'] = time.time()

def terminate_kernel(reason):
    """Stop the whole Kaggle session.

    This process is a CHILD of the kernel script, which supervises and restarts it - so
    os._exit() here would only bounce the gateway and leave the GPU session running.
    We drop a sentinel the supervisor watches for, then signal the parent directly.
    """
    print(f'[gateway] terminating kernel: {reason}', flush=True)
    try:
        with open(STOP_FILE, 'w') as f:
            f.write(reason)
    except Exception as e:
        print(f'[gateway] could not write {STOP_FILE}: {e}', flush=True)
    try:
        ppid = os.getppid()
        if ppid > 1:
            os.kill(ppid, signal.SIGTERM)
    except Exception as e:
        print(f'[gateway] could not signal parent: {e}', flush=True)
    time.sleep(1.0)
    os._exit(0)

def watchdog_loop():
    while True:
        time.sleep(15)
        if not WATCH['armed']:
            continue
        quiet = time.time() - max(WATCH['last_beat'], WATCH['last_req'])
        if quiet > WATCH['ttl']:
            terminate_kernel(f'watchdog: no heartbeat or traffic for {quiet:.0f}s')

def sign(user, exp):
    msg = f'{user}|{exp}'
    return msg + '|' + hmac.new(CONF['secret'].encode(), msg.encode(), hashlib.sha256).hexdigest()

def verify_token(tok):
    try:
        user, exp, sig = tok.split('|')
        good = hmac.new(CONF['secret'].encode(), f'{user}|{exp}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig) and int(exp) > time.time()
    except Exception:
        return False

def check_password(user, pw):
    if user != CONF['user']:
        return False
    h = hashlib.sha256((CONF['salt'] + pw).encode()).hexdigest()
    return hmac.compare_digest(h, CONF['pwhash'])

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1c1a18"><title>Qwen Studio — Sign in</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:grid;place-items:center;background:#1c1a18;color:#ede8e1;
 font:15px/1.6 "Segoe UI",system-ui,sans-serif;overflow:hidden;padding:16px}
.blob{position:fixed;width:60vmax;height:60vmax;border-radius:50%;pointer-events:none;opacity:.5}
.b1{background:radial-gradient(circle,rgba(217,155,92,.30),transparent 60%);top:-22vmax;left:-15vmax;animation:d1 24s ease-in-out infinite alternate}
.b2{background:radial-gradient(circle,rgba(176,105,62,.24),transparent 60%);bottom:-25vmax;right:-15vmax;animation:d2 30s ease-in-out infinite alternate}
@keyframes d1{to{transform:translate(8vmax,6vmax)}}@keyframes d2{to{transform:translate(-7vmax,-5vmax)}}
@media (prefers-reduced-motion:reduce){.blob{animation:none}}
.card{position:relative;width:100%;max-width:380px;padding:36px 32px;border-radius:24px;
 background:rgba(255,255,255,.07);backdrop-filter:blur(28px) saturate(1.5);-webkit-backdrop-filter:blur(28px) saturate(1.5);
 border:1px solid rgba(255,255,255,.1);box-shadow:0 24px 70px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.08);
 animation:in .55s cubic-bezier(.34,1.45,.5,1) both}
@keyframes in{from{opacity:0;transform:translateY(18px) scale(.97)}to{opacity:1}}
.logo{width:44px;height:44px;border-radius:13px;background:linear-gradient(135deg,#d99b5c,#e5c07b);
 display:grid;place-items:center;margin:0 auto 14px;box-shadow:0 8px 24px rgba(217,155,92,.4)}
h1{font-size:19px;text-align:center;font-weight:650}
.sub{text-align:center;color:#a8a096;font-size:12.5px;margin:4px 0 24px}
label{display:block;font-size:11.5px;font-weight:600;color:#a8a096;margin:14px 0 5px}
input[type=text],input[type=password]{width:100%;padding:11px 13px;border-radius:12px;border:1px solid rgba(255,255,255,.12);
 background:rgba(255,244,228,.06);color:#ede8e1;font:14px inherit;outline:0;transition:border-color .2s}
input:focus{border-color:#d99b5c}
.row{display:flex;align-items:center;gap:8px;margin:16px 0 20px;font-size:12.5px;color:#a8a096;user-select:none}
.row input{accent-color:#d99b5c;width:15px;height:15px}
button{width:100%;padding:12px;border:0;border-radius:12px;cursor:pointer;color:#241a0d;font:600 14px inherit;
 background:linear-gradient(135deg,#d99b5c,#e5c07b);box-shadow:0 8px 22px rgba(217,155,92,.35);
 transition:transform .25s cubic-bezier(.34,1.45,.5,1)}
button:hover{transform:translateY(-2px)}button:active{transform:scale(.98)}
.err{background:rgba(232,131,111,.13);border:1px solid rgba(232,131,111,.38);color:#f3b9a8;
 border-radius:10px;padding:9px 12px;font-size:12.5px;margin-bottom:8px;text-align:center}
.foot{text-align:center;color:#7a736a;font-size:10.5px;margin-top:18px}
</style></head><body>
<div class="blob b1"></div><div class="blob b2"></div>
<form class="card" method="POST" action="/login">
<div class="logo"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#241a0d" stroke-width="2.2" stroke-linecap="round"><path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/></svg></div>
<h1>Qwen Studio</h1><div class="sub">your model · your GPU · your rules</div>
{ERR}
<label>Username</label><input type="text" name="user" autocomplete="username" required autofocus>
<label>Password</label><input type="password" name="pw" autocomplete="current-password" required>
<div class="row"><input type="checkbox" name="remember" id="rm" checked><label for="rm" style="margin:0;font-weight:400">Remember me for 30 days</label></div>
<button type="submit">Sign in</button>
<div class="foot">private server · unauthorized access prohibited</div>
</form></body></html>"""

class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass

    # ---------- auth helpers ----------
    def has_cookie(self):
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'qs' and verify_token(v):
                return True
        return False
    def has_key(self):
        # compare bytes: http.server decodes headers as latin-1 and compare_digest
        # raises TypeError on non-ASCII str, which would 500 before auth is even decided
        got = (self.headers.get('Authorization', '') or '').encode('utf-8', 'replace')
        want = ('Bearer ' + CONF['apikey']).encode('utf-8')
        return hmac.compare_digest(got, want)

    # ---------- response helpers ----------
    def out(self, code, body=b'', ctype='text/html; charset=utf-8', extra=None):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or []): self.send_header(k, v)
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass
    def redirect(self, to, cookie=None):
        extra = [('Location', to)]
        if cookie: extra.append(('Set-Cookie', cookie))
        self.out(302, b'', extra=extra)

    # ---------- request fixups ----------
    def fixup(self, body):
        """Returns (body, native_think_off).

        Two problems this works around, both caused by the model's chat template / Ollama:

        1. The Qwen chat template raises "no user query found in messages" when the request
           has no user message. Agent harnesses (opencode) hit this on the follow-up call
           after a tool result, which breaks every tool loop. We inject a minimal user turn.
        2. Ollama's /v1 endpoint ignores every way of disabling thinking. Only the native
           /api/chat endpoint honours {"think": false}. A client asking for "think": false
           on /v1 gets routed through the native endpoint instead (see proxy()).
        """
        if not body:
            return body, False
        try:
            j = json.loads(body)
        except Exception:
            return body, False
        if not isinstance(j, dict) or 'messages' not in j:
            return body, False
        changed = False
        msgs = j.get('messages') or []
        if not any(isinstance(m, dict) and m.get('role') == 'user' for m in msgs):
            i = 0
            while i < len(msgs) and isinstance(msgs[i], dict) and msgs[i].get('role') == 'system':
                i += 1
            msgs.insert(i, {'role': 'user', 'content': 'Continue.'})
            j['messages'] = msgs
            changed = True
        think_off = j.pop('think', None) is False
        if think_off:
            changed = True
        # only translate when there are no tools - native tool payloads differ
        if think_off and j.get('tools'):
            think_off = False
        return (json.dumps(j).encode() if changed else body), think_off

    def native_stream(self, j):
        """Fast mode: run through Ollama's native /api/chat with think=False and
        re-emit the reply as OpenAI-shaped SSE so clients need no special handling."""
        payload = {
            'model': j.get('model'),
            'messages': j.get('messages', []),
            'stream': True,
            'think': False,
            'options': {k: v for k, v in (
                ('temperature', j.get('temperature')),
                ('top_p', j.get('top_p')),
                ('num_predict', j.get('max_tokens')),
            ) if v is not None},
        }
        want_stream = j.get('stream', True) is not False
        try:
            hdr = {}
            if self.headers.get('Authorization'):
                hdr['Authorization'] = self.headers['Authorization']
            up = requests.post(OLLAMA + '/api/chat', json=payload, headers=hdr, stream=True, timeout=(15, 900))
        except Exception as e:
            return self.out(502, json.dumps({'error': 'upstream unreachable: ' + str(e)}), 'application/json', CORS)
        if up.status_code >= 400:
            body = up.text[:400]
            up.close()
            return self.out(up.status_code, json.dumps({'error': {'message': body}}),
                            'application/json', CORS)
        if not want_stream:
            # non-streaming client (opencode, SDKs): collect and return one completion object
            text = ''
            try:
                for line in up.iter_lines():
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    touch_request()
                    text += (d.get('message') or {}).get('content') or ''
            finally:
                up.close()
            return self.out(200, json.dumps({
                'id': 'chatcmpl-native', 'object': 'chat.completion', 'model': payload['model'],
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': text}}]}),
                'application/json', CORS)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        for k, v in CORS:
            self.send_header(k, v)
        self.end_headers()

        def emit(delta, finish=None):
            chunk = {'id': 'chatcmpl-native', 'object': 'chat.completion.chunk',
                     'model': payload['model'],
                     'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            self.wfile.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
            self.wfile.flush()
        try:
            emit({'role': 'assistant', 'content': ''})
            for line in up.iter_lines():
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get('message') or {}
                if msg.get('content'):
                    touch_request()
                    emit({'content': msg['content']})
                if d.get('done'):
                    emit({}, d.get('done_reason') or 'stop')
                    break
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except Exception:
            pass
        finally:
            up.close()
        self.close_connection = True

    # ---------- proxy ----------
    def proxy(self):
        if not (self.has_key() or self.has_cookie()):
            self.drain()
            return self.out(401, b'{"error":"unauthorized: supply Bearer API key or sign in"}',
                            'application/json', CORS)
        touch_request()
        ln = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(ln) if ln else None
        if self.command == 'POST' and self.path.startswith('/v1/chat/completions'):
            body, think_off = self.fixup(body)
            if think_off:
                try:
                    return self.native_stream(json.loads(body))
                except Exception as e:
                    return self.out(500, json.dumps({'error': 'native route failed: ' + str(e)}),
                                    'application/json', CORS)
        try:
            fwd = {'Content-Type': self.headers.get('Content-Type', 'application/json')}
            if self.headers.get('Authorization'):     # only matters when chaining gateways in tests
                fwd['Authorization'] = self.headers['Authorization']
            up = requests.request(self.command, OLLAMA + self.path, data=body,
                                  headers=fwd, stream=True, timeout=(15, 900))
        except Exception as e:
            return self.out(502, json.dumps({'error': 'upstream unreachable: ' + str(e)}), 'application/json', CORS)
        self.send_response(up.status_code)
        self.send_header('Content-Type', up.headers.get('Content-Type', 'application/json'))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        for k, v in CORS: self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in up.iter_content(chunk_size=None):
                if chunk:
                    touch_request()          # a live stream counts as presence
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            up.close()
            try: self.wfile.flush()
            except Exception: pass
        self.close_connection = True

    # ---------- routes ----------
    def do_OPTIONS(self):
        if self.path.startswith('/v1') or self.path.startswith('/api'):
            return self.out(204, b'', 'text/plain', CORS)
        self.out(204, b'')
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ('/', '/index.html'):
            if self.has_cookie(): return self.out(200, UI)
            return self.redirect('/login')
        if p == '/login':
            bad = 'bad=1' in (urlparse(self.path).query or '')
            page = LOGIN.replace('{ERR}', '<div class="err">Wrong username or password.</div>' if bad else '')
            return self.out(200, page)
        if p == '/autologin':
            # single-use, short-lived: sets the session cookie then bounces to the app
            q = parse_qs(urlparse(self.path).query)
            tick = (q.get('t') or [''])[0]
            exp = TICKETS.pop(tick, 0)
            if not tick or exp < time.time():
                return self.redirect('/login')
            exp_at = int(time.time() + 30 * 86400)
            cookie = f'qs={sign(CONF["user"], exp_at)}; Path=/; HttpOnly; Secure; SameSite=None; Max-Age={30*86400}'
            return self.redirect('/', cookie)
        if p == '/logout':
            return self.redirect('/login', 'qs=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=None')
        if p == '/healthz':
            return self.out(200, b'ok', 'text/plain')
        if p == '/readyz':
            # Deliberately NOT routed through proxy(): proxy() calls touch_request(),
            # so a client polling readiness would keep feeding the dead-man switch and
            # the session could never time out. Unauthenticated on purpose - it leaks
            # nothing but "is a model resident" - and it reports VRAM residency rather
            # than mere presence in the model store, which /v1/models cannot distinguish
            # (the tag is listed the moment the pull ends, minutes before warm-up).
            try:
                r = requests.get(OLLAMA + '/api/ps', timeout=5)
                want = os.environ.get('GATEWAY_MODEL', '')
                ready = False
                for m in (r.json() or {}).get('models', []):
                    if want and m.get('name') != want:
                        continue          # a leftover warm-up model is not our answer
                    size, vram = (m.get('size') or 0), (m.get('size_vram') or 0)
                    # Residency, not mere presence: a 30%-resident model answers at CPU
                    # speed, which is not "ready" by any useful definition.
                    if size and vram / size >= 0.99:
                        ready = True
                        break
                # Anonymous callers get a bare boolean: the served tag names an
                # uncensored build and is not something to publish on a public URL.
                return self.out(200, json.dumps({'ready': ready}), 'application/json', CORS)
            except Exception:
                return self.out(200, json.dumps({'ready': False}),
                                'application/json', CORS)
        if p == '/tts/voices':
            return self.tts_voices()
        if p == '/apikey':
            # Only a signed-in browser session may read the key. A Bearer caller already
            # has it, so cookie auth is required here rather than has_key().
            if not self.has_cookie():
                return self.out(401, b'{"error":"sign in first"}', 'application/json')
            return self.out(200, json.dumps({'user': CONF['user'], 'apikey': CONF['apikey']}),
                            'application/json')
        if p.startswith('/v1') or p.startswith('/api'):
            return self.proxy()
        self.out(404, b'not found', 'text/plain')

    def shutdown_now(self):
        """Stop the whole Kaggle session.

        Bearer key ONLY - deliberately not cookie-auth. A cookie-authenticated
        destructive endpoint is reachable by a cross-site simple POST (no preflight),
        leaving SameSite=None as the only defence on a shared *.trycloudflare.com host.
        A browser cannot set an Authorization header without a preflight we refuse.
        """
        if not self.has_key():
            self.drain()
            return self.out(401, b'{"error":"API key required"}', 'application/json', CORS)
        self.drain()
        self.out(200, b'{"status":"stopping kernel"}', 'application/json', CORS)
        try:
            self.wfile.flush()
        except Exception:
            pass
        def bye():
            time.sleep(0.8)          # let the response reach the client first
            terminate_kernel('shutdown requested')
        threading.Thread(target=bye, daemon=True).start()

    # ---------- text to speech (Microsoft Edge neural voices) ----------
    def tts_voices(self):
        global VOICE_CACHE
        if VOICE_CACHE is None:
            try:
                import asyncio, edge_tts
                allv = asyncio.run(edge_tts.list_voices())
                VOICE_CACHE = [{'id': v['ShortName'], 'gender': v.get('Gender', ''),
                                'locale': v.get('Locale', '')}
                               for v in allv if v.get('Locale', '').startswith('en-')]
                VOICE_CACHE.sort(key=lambda v: v['id'])
            except Exception as e:
                return self.out(503, json.dumps({'error': 'tts unavailable: ' + str(e)}),
                                'application/json', CORS)
        return self.out(200, json.dumps({'voices': VOICE_CACHE}), 'application/json', CORS)

    def do_tts(self):
        if not (self.has_key() or self.has_cookie()):
            self.drain()
            return self.out(401, b'{"error":"unauthorized"}', 'application/json', CORS)
        ln = int(self.headers.get('Content-Length') or 0)
        try:
            j = json.loads(self.rfile.read(ln) or b'{}')
        except Exception:
            return self.out(400, b'{"error":"bad json"}', 'application/json', CORS)
        text = (j.get('text') or '').strip()[:4000]
        if not text:
            return self.out(400, b'{"error":"no text"}', 'application/json', CORS)
        voice = j.get('voice') or 'en-US-AriaNeural'
        rate = j.get('rate') or '+0%'
        try:
            import asyncio, edge_tts
            async def synth():
                buf = bytearray()
                comm = edge_tts.Communicate(text, voice, rate=rate)
                async for ch in comm.stream():
                    if ch['type'] == 'audio':
                        buf += ch['data']
                return bytes(buf)
            audio = asyncio.run(synth())
        except Exception as e:
            return self.out(503, json.dumps({'error': 'tts failed: ' + str(e)}),
                            'application/json', CORS)
        touch_request()
        return self.out(200, audio, 'audio/mpeg', CORS)

    def drain(self):
        """Consume an unread request body so keep-alive stays in sync on reject paths."""
        try:
            ln = int(self.headers.get('Content-Length') or 0)
            if ln > 0:
                self.rfile.read(ln)
        except Exception:
            self.close_connection = True

    def do_POST(self):
        p = urlparse(self.path).path
        if p == '/shutdown':
            return self.shutdown_now()
        if p == '/authticket':
            self.drain()
            if not self.has_key():
                return self.out(401, b'{"error":"API key required"}', 'application/json', CORS)
            import secrets
            now = time.time()
            for t, exp in list(TICKETS.items()):        # sweep expired
                if exp < now:
                    TICKETS.pop(t, None)
            tick = secrets.token_urlsafe(24)
            TICKETS[tick] = now + 60
            return self.out(200, json.dumps({'ticket': tick, 'ttl': 60}),
                            'application/json', CORS)
        if p == '/tts':
            return self.do_tts()
        if p == '/watchdog':
            ln = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(ln) if ln > 0 else b'{}'
            if not self.has_key():
                return self.out(401, b'{"error":"API key required"}', 'application/json', CORS)
            try:
                req = json.loads(raw or b'{}')
                if not isinstance(req, dict):
                    req = {}
            except Exception:
                req = {}
            if req.get('disarm'):
                WATCH['armed'] = False
                WATCH['last_beat'] = time.time()
            else:
                try:
                    ttl = int(req.get('ttl') or 1800)
                except (TypeError, ValueError):
                    return self.out(400, b'{"error":"ttl must be an integer"}',
                                    'application/json', CORS)
                # floor must exceed the upstream read timeout (900s) so a long generation
                # can never be guillotined; ceiling stops a silly value neutering the fuse
                ttl = min(21600, max(1200, ttl))   # allow up to 6 h; the app asks for 3 h
                WATCH['last_beat'] = time.time()   # feed BEFORE arming, never the reverse
                WATCH['ttl'] = ttl
                WATCH['armed'] = True
            return self.out(200, json.dumps({'armed': WATCH['armed'], 'ttl': WATCH['ttl']}),
                            'application/json', CORS)
        if p == '/login':
            ln = int(self.headers.get('Content-Length') or 0)
            form = parse_qs(self.rfile.read(ln).decode('utf-8', 'replace'))
            user = (form.get('user') or [''])[0].strip()
            pw = (form.get('pw') or [''])[0]
            remember = bool(form.get('remember'))
            if check_password(user, pw):
                days = 30 if remember else 0.5
                exp = int(time.time() + days * 86400)
                cookie = f'qs={sign(user, exp)}; Path=/; HttpOnly; Secure; SameSite=None'
                if remember: cookie += f'; Max-Age={30*86400}'
                return self.redirect('/', cookie)
            time.sleep(1.2)  # slow brute force
            return self.redirect('/login?bad=1')
        if p.startswith('/v1') or p.startswith('/api'):
            return self.proxy()
        self.out(404, b'not found', 'text/plain')

if __name__ == '__main__':
    threading.Thread(target=watchdog_loop, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    print(f'[gateway] listening on :{PORT} -> {OLLAMA}', flush=True)
    srv.serve_forever()
