import subprocess, time, os, re, sys, threading, base64, signal, json

# ====== Qwen Studio on Kaggle: Ollama + auth gateway + web UI + cloudflared ======
TMP = '/kaggle/tmp'
os.makedirs(TMP, exist_ok=True)
os.environ['OLLAMA_MODELS'] = f'{TMP}/ollama-models'
os.environ['OLLAMA_HOST'] = '127.0.0.1:11434'
os.environ['OLLAMA_KEEP_ALIVE'] = '-1'
os.environ['OLLAMA_FLASH_ATTENTION'] = '1'      # required for KV cache quantisation
os.environ['OLLAMA_KV_CACHE_TYPE'] = 'q8_0'     # halves KV cache VRAM -> aim for 100% GPU residency
os.environ['OLLAMA_NUM_PARALLEL'] = '1'         # one request at a time = all VRAM for one context
os.environ['OLLAMA_MAX_LOADED_MODELS'] = '1'
GATEWAY_PORT = 8000
STOP_FILE = f'{TMP}/STOP'          # gateway drops this to ask the whole kernel to stop
os.environ['GATEWAY_STOP_FILE'] = STOP_FILE
try:
    os.remove(STOP_FILE)           # never inherit a stale sentinel
except OSError:
    pass

def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)

class FakeResult:
    returncode = 127; stdout = ''; stderr = ''

def run(cmd, timeout=None, shell=False):
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log(f'run() exception: {e} cmd={cmd}')
        fr = FakeResult(); fr.stderr = str(e); return fr
    if r.returncode != 0:
        log(f'rc={r.returncode} cmd={cmd if shell else " ".join(cmd)} err={r.stderr[-400:]}')
    return r

# ---------- 1. GPU inventory -> quant that fits ----------
r = run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'])
gpus = [l for l in r.stdout.strip().splitlines() if l.strip()]
total_mb = sum(int(l.split(',')[1]) for l in gpus) if gpus else 0
n_gpu = len(gpus)
log(f'GPUs: {gpus} | count {n_gpu} | total VRAM: {total_mb} MB')

# Use every GPU Kaggle gave us. OLLAMA_SCHED_SPREAD makes Ollama distribute the model's
# layers across all visible GPUs instead of cramming it into one (and spilling to CPU).
os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(str(i) for i in range(max(n_gpu, 1)))
if n_gpu >= 2:
    os.environ['OLLAMA_SCHED_SPREAD'] = '1'   # spread layers over every visible GPU
    log(f'MULTI-GPU OK: {n_gpu} GPUs bound, CUDA_VISIBLE_DEVICES={os.environ["CUDA_VISIBLE_DEVICES"]}, SCHED_SPREAD=1')
else:
    log('SINGLE GPU ONLY: Kaggle granted 1 device despite machine_shape=NvidiaTeslaT4. '
        'Free-tier scheduling decides this; a T4 x2 box would give 32 GB and full residency.')

if total_mb >= 26000:
    TAG = 'huihui_ai/Qwen3.8-abliterated:27b'        # q4_K_M 18 GB - fits 2xT4 32 GB
elif total_mb >= 15000:
    TAG = 'huihui_ai/Qwen3.8-abliterated:27b-q3_K'   # 14 GB - single 16 GB card
else:
    TAG = 'huihui_ai/Qwen3.8-abliterated:27b-q2_K'
log(f'Selected model tag: {TAG}')
# Context window. Qwen3.8 is a HYBRID: only 16 of its 64 layers are real attention
# layers that keep a growing KV cache (the other 48 are Gated DeltaNet with a
# fixed-size state), so a token costs ~32 KB with our q8_0 cache instead of the
# ~128 KB a normal 27B would cost. On 2xT4 that leaves ~11 GB free after the
# weights - 128K fits with ~9 GB to spare. Scale down on smaller cards.
if total_mb >= 26000:
    NUM_CTX = 131072
elif total_mb >= 15000:
    NUM_CTX = 32768
else:
    NUM_CTX = 16384
os.environ['OLLAMA_CONTEXT_LENGTH'] = str(NUM_CTX)   # applies to /v1 too, which
# has no per-request num_ctx knob - the OpenAI-compatible path uses the server default
log(f'Context window: {NUM_CTX} tokens')
# /readyz answers about THIS tag, so a stale warm-up model left resident from an
# earlier load cannot make the gateway claim it is serving.
os.environ['GATEWAY_MODEL'] = TAG

# ---------- 2. Ollama static bundle ----------
log('Downloading Ollama static bundle...')
DIST = f'{TMP}/ollama-dist'; os.makedirs(DIST, exist_ok=True)
OLLAMA_URL = 'https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst'
for attempt in range(3):
    r = run(['wget', '-q', OLLAMA_URL, '-O', f'{TMP}/ollama.tar.zst'], timeout=1200)
    if r.returncode == 0 and os.path.getsize(f'{TMP}/ollama.tar.zst') > 500_000_000:
        break
    log(f'ollama download attempt {attempt+1} failed (rc={r.returncode}), retrying...'); time.sleep(10)
else:
    log('FATAL: could not download ollama tarball'); sys.exit(1)
r = run(['tar', '--zstd', '-xf', f'{TMP}/ollama.tar.zst', '-C', DIST], timeout=600)
if r.returncode != 0:
    log('tar --zstd failed, installing zstd and retrying...')
    run(['apt-get', 'install', '-y', 'zstd'], timeout=300)
    r = run(['tar', '--zstd', '-xf', f'{TMP}/ollama.tar.zst', '-C', DIST], timeout=600)
OLLAMA = f'{DIST}/bin/ollama'
os.environ['LD_LIBRARY_PATH'] = f'{DIST}/lib/ollama:' + os.environ.get('LD_LIBRARY_PATH', '')
r = run([OLLAMA, '--version'])
if r.returncode != 0:
    log('FATAL: ollama binary not working after extract'); sys.exit(1)
log(f'Ollama ready: {r.stdout.strip()} {r.stderr.strip()}')

# ---------- 3. ollama serve ----------
ollama_log = open(f'{TMP}/ollama.log', 'w')

def start_serve():
    """(Re)start ollama serve with the CURRENT env.

    OLLAMA_CONTEXT_LENGTH is read once at startup and is what the OpenAI-compatible
    /v1 path uses (it has no per-request num_ctx), so changing the context means
    restarting the server - setting the variable afterwards would do nothing."""
    global serve
    p = subprocess.Popen([OLLAMA, 'serve'], stdout=ollama_log,
                         stderr=subprocess.STDOUT, env=os.environ.copy())
    for _ in range(30):
        if run('curl -s http://127.0.0.1:11434/api/version', shell=True).returncode == 0:
            return p
        time.sleep(2)
    return p

serve = start_serve()
if run('curl -s http://127.0.0.1:11434/api/version', shell=True).returncode != 0:
    log('FATAL: ollama serve never answered'); sys.exit(1)
log('ollama serve is up.')

# ---------- 4. Write UI + gateway + auth, then start the gateway ----------
APP = f'{TMP}/app'; os.makedirs(APP, exist_ok=True)
open(f'{APP}/chat.html', 'wb').write(base64.b64decode(HTML_B64))
open(f'{APP}/gateway.py', 'wb').write(base64.b64decode(GATEWAY_B64))
open(f'{APP}/auth.json', 'wb').write(base64.b64decode(AUTH_B64))
log('UI + gateway written.')

log('Installing edge-tts (neural voices for the read-aloud button)...')
run('pip install -q edge-tts', shell=True, timeout=300)

gw_log = open(f'{TMP}/gateway.log', 'w')
def start_gateway():
    return subprocess.Popen([sys.executable, f'{APP}/gateway.py', f'{APP}/auth.json', f'{APP}/chat.html', str(GATEWAY_PORT)],
                            stdout=gw_log, stderr=subprocess.STDOUT, env=os.environ.copy())
gw = start_gateway()
for i in range(30):
    if run(f'curl -s http://127.0.0.1:{GATEWAY_PORT}/healthz', shell=True).stdout.strip() == 'ok': break
    time.sleep(2)
else:
    log('FATAL: gateway never answered on /healthz'); log(open(f'{TMP}/gateway.log').read()[-1500:]); sys.exit(1)
log(f'gateway is up on :{GATEWAY_PORT}')

# ---------- 5. Tunnel: fixed subdomain via localtunnel, cloudflared as fallback ----------
# A stable hostname matters: browser cookies AND the web app's chat history (localStorage)
# are keyed to the origin, so a random URL each boot wipes both.
FIXED_SUB = 'luzaqwen'
FIXED_URL = f'https://{FIXED_SUB}.loca.lt'
# Cloudflare named tunnel (set in gateway/auth.json). When present this wins: it is a
# hostname we own, so it survives restarts and cannot be taken by anyone else.
_auth_cfg = json.loads(base64.b64decode(AUTH_B64).decode('utf-8'))
CF_TOKEN = _auth_cfg.get('cf_tunnel_token', '')
CF_HOSTNAME = _auth_cfg.get('cf_hostname', '')
# ngrok reserved (static) domain - one is free per account and never changes. Same
# guarantee as a Cloudflare named tunnel, without needing to own a domain.
NGROK_TOKEN = _auth_cfg.get('ngrok_token', '')
NGROK_DOMAIN = _auth_cfg.get('ngrok_domain', '')
# Installed copies (no ngrok account): always a cloudflared quick tunnel, and the address is
# announced on a private ntfy.sh topic that only this user's app knows.
CLOUD_ONLY = _auth_cfg.get('tunnel') == 'cloudflared'
NTFY_TOPIC = _auth_cfg.get('ntfy_topic', '')

def announce(url):
    if not (NTFY_TOPIC and url):
        return
    try:
        run(['curl', '-s', '--max-time', '20', '-d', url, f'https://ntfy.sh/{NTFY_TOPIC}'], timeout=40)
    except Exception:
        pass
TUNNEL_URL = None
tunnel = None
tunnel_kind = None

def _reader(proc, pattern):
    """Scrape the first URL a tunnel client prints, on a daemon thread."""
    def run():
        global TUNNEL_URL
        for line in proc.stdout:
            m = re.search(pattern, line)
            if m and not TUNNEL_URL:
                TUNNEL_URL = m.group(0)
    threading.Thread(target=run, daemon=True).start()

def ensure_node():
    if run(['which', 'npx']).returncode == 0:
        return True
    log('npx missing - installing nodejs...')
    run('apt-get install -y -q nodejs npm', shell=True, timeout=600)
    return run(['which', 'npx']).returncode == 0

def try_localtunnel():
    """One attempt. Returns (proc, url) - url may be a random name we then reject."""
    global TUNNEL_URL
    TUNNEL_URL = None
    p = subprocess.Popen(['npx', '--yes', 'localtunnel', '--port', str(GATEWAY_PORT),
                          '--subdomain', FIXED_SUB],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _reader(p, r'https://[a-z0-9-]+\.loca\.lt')
    for _ in range(40):
        if TUNNEL_URL:
            return p, TUNNEL_URL
        if p.poll() is not None:
            return None, None
        time.sleep(1)
    return p, None

def start_localtunnel(attempts=12, wait=20):
    """localtunnel's --subdomain is best-effort: a name released moments ago is still on
    cooldown and you silently get a random one instead. Measured locally: only ~1 in 3
    immediate re-claims wins. So verify what we actually got and retry until it is ours."""
    if not ensure_node():
        log('node/npx unavailable - cannot use localtunnel')
        return None, None
    for i in range(1, attempts + 1):
        p, url = try_localtunnel()
        if url == FIXED_URL:
            log(f'localtunnel bound to {FIXED_URL} (attempt {i})')
            return p, url
        if p:
            log(f'attempt {i}: got {url or "no url"} instead of {FIXED_SUB} - retrying in {wait}s')
            try:
                p.terminate()
            except Exception:
                pass
        else:
            log(f'attempt {i}: localtunnel exited immediately - retrying in {wait}s')
        time.sleep(wait)
    return None, None

def get_cloudflared():
    """Download the cloudflared binary once; return its path or None."""
    CF = f'{TMP}/cloudflared'
    if not os.path.exists(CF):
        for attempt in range(3):
            r = run(['wget', '-q', 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64', '-O', CF], timeout=300)
            if r.returncode == 0 and os.path.getsize(CF) > 10_000_000:
                break
            time.sleep(5)
        else:
            return None
        os.chmod(CF, 0o755)
    return CF

def start_named_tunnel():
    """A Cloudflare NAMED tunnel: a permanent hostname we own.

    This is the only tunnel here whose address cannot change. The token carries the
    hostname->localhost:8000 mapping configured in the Cloudflare dashboard, so there
    is no URL to scrape, no subdomain cooldown to lose, and no interstitial password
    page. Everything else in this file exists to cope with not having that.
    """
    global TUNNEL_URL
    if not (CF_TOKEN and CF_HOSTNAME):
        return None, None
    CF = get_cloudflared()
    if not CF:
        log('could not download cloudflared for the named tunnel')
        return None, None
    p = subprocess.Popen([CF, 'tunnel', '--no-autoupdate', 'run', '--token', CF_TOKEN],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f'https://{CF_HOSTNAME}'
    # the hostname is fixed, so "up" means Cloudflare answers on it, not a scraped URL
    for _ in range(45):
        if p.poll() is not None:
            log('named tunnel exited immediately - check the token')
            return None, None
        probe = run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '10',
                     url.rstrip('/') + '/healthz'], timeout=25)
        if (probe.stdout or '').strip() == '200':
            TUNNEL_URL = url
            return p, url
        time.sleep(4)
    log('named tunnel did not become reachable in 3 min')
    try:
        p.terminate()
    except Exception:
        pass
    return None, None

def start_ngrok():
    """A reserved ngrok domain: fixed, ours, and it cannot be taken by anyone else.

    Unlike localtunnel's --subdomain (best-effort, lost to cooldown after a restart),
    a reserved domain is bound to the account, so the address is identical every
    session and neither the app nor opencode ever needs repointing.
    """
    global TUNNEL_URL
    if not (NGROK_TOKEN and NGROK_DOMAIN):
        return None, None
    NG = f'{TMP}/ngrok'
    if not os.path.exists(NG):
        r = run(['wget', '-q', 'https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz',
                 '-O', f'{TMP}/ngrok.tgz'], timeout=300)
        if r.returncode != 0:
            log('could not download ngrok'); return None, None
        run(['tar', '-xzf', f'{TMP}/ngrok.tgz', '-C', TMP], timeout=120)
        if not os.path.exists(NG):
            log('ngrok archive did not contain the binary'); return None, None
        os.chmod(NG, 0o755)
    env = os.environ.copy()
    env['NGROK_AUTHTOKEN'] = NGROK_TOKEN
    p = subprocess.Popen([NG, 'http', str(GATEWAY_PORT), '--domain', NGROK_DOMAIN,
                          '--log', 'stdout'],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    url = f'https://{NGROK_DOMAIN}'
    for _ in range(30):
        if p.poll() is not None:
            log('ngrok exited immediately - check the authtoken and domain')
            return None, None
        probe = run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '10',
                     url.rstrip('/') + '/healthz'], timeout=25)
        if (probe.stdout or '').strip() == '200':
            TUNNEL_URL = url
            return p, url
        time.sleep(4)
    log('ngrok did not become reachable in 2 min')
    try:
        p.terminate()
    except Exception:
        pass
    return None, None

def start_cloudflared():
    """Fallback so a localtunnel outage never leaves the box unreachable."""
    global TUNNEL_URL
    CF = get_cloudflared()
    if not CF:
        return None, None
    TUNNEL_URL = None
    p = subprocess.Popen([CF, 'tunnel', '--url', f'http://127.0.0.1:{GATEWAY_PORT}', '--no-autoupdate'],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _reader(p, r'https://[a-z0-9-]+\.trycloudflare\.com')
    for _ in range(60):
        if TUNNEL_URL:
            return p, TUNNEL_URL
        time.sleep(2)
    return None, None

def open_tunnel():
    global tunnel_kind
    # 1. Reserved ngrok domain - permanent, needs no domain of our own.
    if NGROK_TOKEN and NGROK_DOMAIN:
        p, url = start_ngrok()
        if url:
            tunnel_kind = 'ngrok'
            log(f'NGROK up on the permanent address {url}')
            return p, url
        log('ngrok failed - falling back.')
    # 2. Cloudflare named tunnel on a domain we own - also permanent.
    if CF_TOKEN and CF_HOSTNAME:
        p, url = start_named_tunnel()
        if url:
            tunnel_kind = 'named'
            log(f'NAMED TUNNEL up on the permanent address {url}')
            return p, url
        log('named tunnel failed - falling back to the throwaway tunnels.')
    if CLOUD_ONLY:
        p, url = start_cloudflared()
        tunnel_kind = 'cloudflared' if url else None
        return p, url
    # 2. Best-effort fixed subdomain on the free service.
    p, url = start_localtunnel()
    if url:
        tunnel_kind = 'localtunnel'
        return p, url
    # 3. Random cloudflared URL - reachable, but nothing we own points at it.
    log('FALLING BACK to a random cloudflared URL - the fixed subdomain was unavailable.')
    p, url = start_cloudflared()
    tunnel_kind = 'cloudflared' if url else None
    return p, url

tunnel, TUNNEL_URL = open_tunnel()
if not TUNNEL_URL:
    log('FATAL: no tunnel could be established'); sys.exit(1)
announce(TUNNEL_URL)

# The loca.lt interstitial asks a first-time browser for a "tunnel password", which is
# this machine's public IP. Print it so it is one click, once, per browser.
TUNNEL_PW = ''
if tunnel_kind == 'localtunnel':
    r = run(['curl', '-s', '--max-time', '20', 'https://loca.lt/mytunnelpassword'], timeout=40)
    TUNNEL_PW = (r.stdout or '').strip()

def banner(state='starting'):
    print('\n' + '=' * 66, flush=True)
    print('  QWEN STUDIO  ({})'.format(state), flush=True)
    print('  WEB APP  : {}            <- open this, sign in'.format(TUNNEL_URL), flush=True)
    print('  API BASE : {}/v1         <- for opencode / any OpenAI client'.format(TUNNEL_URL), flush=True)
    print('  MODEL    : {}'.format(TAG), flush=True)
    print('  LOGIN    : user "{}" - password + API key are in your local CREDENTIALS.txt'.format(USERNAME), flush=True)
    print('  TUNNEL   : {}'.format(tunnel_kind), flush=True)
    if TUNNEL_PW:
        print('  LT PASSWORD: {}   <- only for the loca.lt "Tunnel website ahead" page'.format(TUNNEL_PW), flush=True)
    if tunnel_kind in ('named', 'ngrok'):
        print('  NOTE     : permanent address - it will be the same every session', flush=True)
    elif TUNNEL_URL != FIXED_URL:
        print('  NOTE     : this is NOT the fixed URL - cookies/history will not carry over', flush=True)
    print('=' * 66 + '\n', flush=True)
banner('model still loading')

# ---------- 6. Pull + warm ----------
log(f'Pulling {TAG} (long step)...')
for attempt in range(3):
    r = run([OLLAMA, 'pull', TAG], timeout=3600)
    if r.returncode == 0: break
    log(f'pull attempt {attempt+1} failed, retrying in 15s...'); time.sleep(15)
else:
    log('FATAL: model pull failed 3x'); sys.exit(1)
log('Model pulled. Warming up (loading into VRAM)...')
# warm up AT the real context size, so the cache is allocated once here rather than
# forcing a reload (and a residency surprise) on the user's first message
r = run(['curl', '-s', 'http://127.0.0.1:11434/api/chat', '-d',
         '{"model":"%s","messages":[{"role":"user","content":"Say ready."}],'
         '"stream":false,"keep_alive":-1,"options":{"num_ctx":%d}}' % (TAG, NUM_CTX)],
        timeout=900)
log(f'Warm-up: {r.stdout[-160:]}')
def residency():
    """(percent, ctx) for our tag, or (None, None) if it cannot be read."""
    ps = run(['curl', '-s', 'http://127.0.0.1:11434/api/ps'], timeout=60)
    try:
        import json as _j
        for m in _j.loads(ps.stdout).get('models', []):
            if m.get('name') != TAG:
                continue
            sz, vr = m.get('size', 0), m.get('size_vram', 0)
            return ((100 * vr / sz) if sz else 0), m.get('context_length')
    except Exception as e:
        log(f'residency check failed: {e}')
    return None, None

# A context that does not fit spills model layers to CPU and generation speed
# collapses - far worse than a smaller window. Step down until it is fully resident.
for ctx_try in (NUM_CTX, 65536, 32768):
    if ctx_try != NUM_CTX:
        log(f'Context {NUM_CTX} did not fit - restarting at {ctx_try}...')
        NUM_CTX = ctx_try
        os.environ['OLLAMA_CONTEXT_LENGTH'] = str(ctx_try)
        try:
            serve.terminate(); serve.wait(timeout=30)
        except Exception:
            pass
        time.sleep(3)
        serve = start_serve()          # must restart: /v1 reads the value at startup
        run(['curl', '-s', 'http://127.0.0.1:11434/api/chat', '-d',
             '{"model":"%s","messages":[{"role":"user","content":"Say ready."}],'
             '"stream":false,"keep_alive":-1,"options":{"num_ctx":%d}}' % (TAG, ctx_try)],
            timeout=900)
    pct, actual = residency()
    if pct is None:
        break
    log(f'RESIDENCY: {pct:.0f}% at context {actual}' +
        ('  <-- FULL GPU' if pct > 99 else '  <-- spilling to CPU'))
    if pct > 99:
        break
log(f'Serving with a {NUM_CTX}-token context window.')
log('READY. Model is loaded and serving.')
banner('READY')

# ---------- 7. Keepalive + self-heal ----------
def _sigterm(*_):
    log('SIGTERM received - stopping the kernel.')
    try:
        gw.terminate(); tunnel.terminate(); serve.terminate()
    except Exception:
        pass
    os._exit(0)
signal.signal(signal.SIGTERM, _sigterm)

hb = 0
tick = 0
unreachable = 0        # consecutive heartbeats where the public URL did not answer
rebinds = 0            # tunnel rebuilds this session; enough of them = give up on loca.lt
while True:
    time.sleep(5)
    tick += 1
    # a shutdown request must take effect in seconds, not at the next 5-min heartbeat
    if os.path.exists(STOP_FILE):
        try:
            reason = open(STOP_FILE).read()[:200]
        except Exception:
            reason = '?'
        log(f'STOP requested ({reason}) - shutting the kernel down.')
        for p in (gw, tunnel, serve):
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(2)
        sys.exit(0)
    if gw.poll() is not None:
        log('gateway died - restarting'); gw = start_gateway(); time.sleep(3)
    if tick % 60:        # everything below runs every 5 minutes
        continue
    hb += 1
    # poll() alone is not enough: localtunnel's favourite failure is a live process
    # whose link to the edge is gone, so loca.lt answers 502 forever while poll() stays
    # None and this supervisor sees nothing wrong. Probe the public URL as well.
    dead = tunnel is None or tunnel.poll() is not None
    if not dead and TUNNEL_URL:
        probe = run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                     '--max-time', '20', TUNNEL_URL.rstrip('/') + '/healthz'], timeout=40)
        code = (probe.stdout or '').strip()
        if code not in ('200', '511'):     # 511 = loca.lt interstitial, tunnel is fine
            unreachable += 1
            log(f'tunnel unreachable from outside (HTTP {code or "none"}) '
                f'- strike {unreachable}/2')
            # two consecutive misses, i.e. ~10 min, before tearing a tunnel down: a
            # single 502 really is transient and re-binding costs the fixed subdomain.
            if unreachable >= 2:
                dead = True
        else:
            unreachable = 0
            # Healthy but on the WRONG address (subdomain cooldown at boot forced a
            # random or cloudflared URL). Everything - app, opencode, cookies - points
            # at the fixed name, so keep trying to win it back, once per heartbeat.
            # a named tunnel IS the permanent address - never trade it for loca.lt
            if not CLOUD_ONLY and tunnel_kind not in ('named', 'ngrok') and TUNNEL_URL != FIXED_URL:
                log(f'on fallback URL {TUNNEL_URL} - trying to reclaim {FIXED_SUB}...')
                cur = TUNNEL_URL
                np_, nurl = try_localtunnel()      # clobbers the TUNNEL_URL global
                if nurl == FIXED_URL:
                    try:
                        tunnel.terminate()
                    except Exception:
                        pass
                    tunnel, TUNNEL_URL, tunnel_kind = np_, FIXED_URL, 'localtunnel'
                    log(f'RECLAIMED {FIXED_URL} - the fixed address works again.')
                    print(f'  TUNNEL_URL: {FIXED_URL}', flush=True)
                else:
                    if np_:
                        try:
                            np_.terminate()
                        except Exception:
                            pass
                    TUNNEL_URL = cur               # restore; reader guard keeps it ours
    if dead:
        rebinds += 1
        log(f'tunnel dead - re-binding (rebind #{rebinds})')
        try:
            if tunnel is not None:
                tunnel.terminate(); time.sleep(2)
        except Exception:
            pass
        unreachable = 0
        # localtunnel is the flaky link in this whole chain. After it has failed
        # twice in one session, stop paying the ~4 min retry cost for the pretty
        # name and take cloudflared, which is far steadier; the app can discover
        # the new address from this log.
        if rebinds >= 3 and tunnel_kind not in ('named', 'ngrok'):
            log('localtunnel has failed repeatedly - switching to cloudflared.')
            tunnel, TUNNEL_URL = start_cloudflared()
            tunnel_kind = 'cloudflared' if TUNNEL_URL else None
        else:
            tunnel, TUNNEL_URL = open_tunnel()
        if TUNNEL_URL:
            log(f'TUNNEL_URL: {TUNNEL_URL}')
            print(f'  TUNNEL_URL: {TUNNEL_URL}', flush=True)
            announce(TUNNEL_URL)
        else:
            log('could not re-establish a tunnel; will retry next heartbeat')
    g = run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader'])
    log(f'heartbeat #{hb} | GPU mem: {g.stdout.strip().replace(chr(10), " / ")}')
    announce(TUNNEL_URL)       # keeps the latest address fresh for an app that opens late
    banner('READY')
