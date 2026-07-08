#!/usr/bin/env python3
# Motor de NUVEM do Baixador — download + limpar + ver metadados
# Roda no Render via Docker. HTTP simples (Render termina HTTPS na frente).
import os, re, json, shutil, tempfile, subprocess, urllib.parse, urllib.request, threading, time, uuid, zipfile, base64, socket, ipaddress
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import limpa_midia

PORT = int(os.environ.get('PORT', '10000'))
MAX_UPLOAD = 300 * 1024 * 1024   # 300 MB para /clean e /meta
JOB_TTL    = 600                  # 10 min para baixar os arquivos do job
CONVERT_TIMEOUT = 100            # seg: teto da conversao HEVC->H.264 (nunca trava pra sempre)
# 1 conversao por vez: a instancia do Render tem pouca memoria e varias conversoes
# 1080p ao mesmo tempo estouravam o limite (OOM -> restart automatico).
_CONVERT_SEM = threading.Semaphore(1)

# Cookies do Instagram (login). Guarda no disco persistente do Render (/var/data)
# se existir, para sobreviver a restart/deploy; senao usa pasta temporaria.
COOKIE_DIR = '/var/data' if os.path.isdir('/var/data') else tempfile.gettempdir()
COOKIES    = os.path.join(COOKIE_DIR, 'cookies.txt')

def _cookie_path(user):
    """Arquivo de cookies ISOLADO por usuario (chave imprevisivel vinda do front).
    Sem chave, cai no arquivo legado compartilhado (compatibilidade)."""
    u = re.sub(r'[^A-Za-z0-9]', '', str(user or ''))[:64]
    return os.path.join(COOKIE_DIR, 'cookies_' + u + '.txt') if u else COOKIES

def cookie_args(user=None):
    """Usa o arquivo de cookies do usuario se existir (foto/carrossel do Instagram)."""
    p = _cookie_path(user)
    return ['--cookies', p] if os.path.exists(p) else []

def _cd(name):
    """Content-Disposition seguro: ASCII no filename + UTF-8 (acento/emoji) no filename*.
    Evita UnicodeEncodeError (header latin-1) que derrubava a resposta com nome com emoji."""
    ascii_name = re.sub(r'[^A-Za-z0-9 ._-]', '_', str(name)).strip() or 'arquivo'
    return 'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + urllib.parse.quote(str(name))

IMG_CLEAN = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.heic', '.tiff', '.tif'}
VID_CLEAN = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}
GIF_RE    = re.compile(r'\.(gif)(\?|$)', re.I)
IMG_RE    = re.compile(r'\.(jpe?g|png|webp|avif|heic|tiff?)(\?|$)', re.I)

SENSITIVE_RE = re.compile(
    r'GPS|Make|Model|Date|Owner|Author|Artist|Creator|Software|Serial|Lens|'
    r'HostComputer|Copyright|Comment|Location|City|Country|State|Title|Keyword|'
    r'Subject|Description|Device|UniqueID|Rights|By-line|Credit|Instruction|'
    r'FBMD|Caption|Headline|Source|Contact|Email|Phone|Identifier|PersonI', re.I)

VIDEO_AV = ['-S', 'res,vcodec:h264,acodec:aac', '--merge-output-format', 'mp4']

# ---------- jobs em memoria ----------
_jobs      = {}   # {job_id: {'dir': path, 'files': [paths], 'expires': ts}}
_jobs_lock = threading.Lock()

def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with _jobs_lock:
            dead = [jid for jid, j in _jobs.items() if j['expires'] < now]
            for jid in dead:
                shutil.rmtree(_jobs[jid]['dir'], ignore_errors=True)
                del _jobs[jid]

threading.Thread(target=_cleanup_loop, daemon=True).start()

def _new_job():
    jid  = str(uuid.uuid4())
    d    = tempfile.mkdtemp(prefix='job_')
    with _jobs_lock:
        _jobs[jid] = {'dir': d, 'files': [], 'expires': time.time() + JOB_TTL}
    return jid, d

def _job_add_file(jid, path):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['files'].append(path)

def _job_get(jid):
    with _jobs_lock:
        return _jobs.get(jid)

# ---------- utilidades ----------
def clean_meta(path):
    root, ext = os.path.splitext(path)
    ext = ext.lower()
    out = root + '.__clean__' + ext
    try:
        if ext in IMG_CLEAN:
            limpa_midia.limpar_imagem(path, out)
        elif ext in VID_CLEAN:
            limpa_midia.limpar_video(path, out)
        else:
            return False
        os.replace(out, path)
        return True
    except Exception:
        try:
            os.path.exists(out) and os.remove(out)
        except Exception:
            pass
        return False

def collect(d):
    out = []
    for root, _, files in os.walk(d):
        for f in sorted(files):
            if not f.startswith('.') and not f.endswith('.__clean__'):
                out.append(os.path.join(root, f))
    return out

def normalize_link(raw):
    raw = raw.strip().strip('""\'')
    m = re.search(r'https?://[^\s<>"\'""]+', raw)
    if m:
        return m.group(0).rstrip('.,;)')
    raw = re.sub(r'^[@#]', '', raw)
    return 'https://' + raw if not raw.startswith('http') else raw

def detect_source(link):
    if 'instagram.com' in link: return 'Instagram'
    if 'tiktok.com'    in link: return 'TikTok'
    if 'pinterest'     in link: return 'Pinterest'
    if 'youtube.com'   in link or 'youtu.be' in link: return 'YouTube'
    if 'twitter.com'   in link or 'x.com'    in link: return 'X/Twitter'
    return 'Web'

# ---------- download ----------
def _ytdlp_run(extra_args, link, out_dir, out_tmpl, emit, user=None):
    cmd = (['yt-dlp', '-P', out_dir, '-o', out_tmpl, '--newline',
            '--progress-template', 'download:DLP|%(progress._percent_str)s|%(progress.eta)s']
           + cookie_args(user) + extra_args + [link])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    last = ''
    count = 0
    for line in p.stdout:
        line = line.strip()
        if line.startswith('DLP|'):
            parts = line.split('|')
            try:
                pct = float(parts[1].replace('%', '').strip())
                emit({'type': 'progress', 'percent': int(20 + pct * 0.75)})
            except Exception:
                pass
        elif line and not line.startswith('['):
            count += 1
            emit({'type': 'progress', 'percent': min(90, 20 + count * 10)})
        if line:
            last = line
    p.wait()
    emit({'type': 'progress', 'percent': 95})
    return p.returncode, last

def _gallery_run(link, out_dir, emit, user=None):
    emit({'type': 'status', 'text': 'baixando imagem...'})
    cmd = ['gallery-dl'] + cookie_args(user) + ['-d', out_dir, '--filename', '{num}.{extension}', link]
    p = subprocess.run(cmd, capture_output=True, text=True)
    emit({'type': 'progress', 'percent': 95})
    return p.returncode, p.stderr

def _curl_run(link, out_dir, gif=False, emit=None):
    if emit: emit({'type': 'status', 'text': 'baixando...'})
    tail = link.split('/')[-1]
    m = re.search(r'\.([a-z0-9]{2,4})(\?|#|$)', tail, re.I)
    ext = 'gif' if gif else (m.group(1).lower() if m else 'jpg')
    out = os.path.join(out_dir, f'file.{ext}')
    if emit: emit({'type': 'progress', 'percent': 40})
    p = subprocess.run(['curl', '-sL', '-o', out, link])
    if emit: emit({'type': 'progress', 'percent': 95})
    return p.returncode, ''

def _ensure_quicktime(path, emit, do_convert=True):
    """Garante H.264 (compatibilidade com editores/desktop). No CELULAR
    (do_convert=False) NAO converte: iPhone/Android postam HEVC nativo e a
    conversao era justamente o passo lento que travava a tela no telefone.
    Nunca bloqueia pra sempre: usa preset rapido e timeout; se estourar, fica
    com o arquivo original (melhor um HEVC pronto do que travar)."""
    try:
        if not do_convert:
            return
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name', '-of', 'default=nk=1:nw=1', path],
            capture_output=True, text=True)
        vcodec = r.stdout.strip()
        if not vcodec or vcodec == 'h264':
            return
        tmp = path + '.qt.mp4'
        # Semaforo: 1 conversao por vez (bounda a memoria). O batimento do /download
        # mantem a conexao viva enquanto espera a vez. -threads 2 segura a RAM do ffmpeg.
        with _CONVERT_SEM:
            emit({'type': 'status', 'text': f'convertendo {vcodec}→H.264...'})
            try:
                subprocess.run(['ffmpeg', '-y', '-i', path,
                                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                                '-threads', '2', '-c:a', 'aac', '-movflags', '+faststart', tmp],
                               capture_output=True, timeout=CONVERT_TIMEOUT)
            except subprocess.TimeoutExpired:
                try: os.remove(tmp)
                except Exception: pass
                return
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            try: os.remove(tmp)
            except Exception: pass
    except Exception:
        pass

def _tikwm_video(link, out_dir, emit=None):
    """Fallback p/ TikTok: as vezes o yt-dlp so acha a faixa de audio (m4a) por
    causa da protecao anti-robo do TikTok. Aqui pega o MP4 sem marca d'agua pela
    API publica do tikwm. Devolve True se salvou um video."""
    try:
        if emit: emit({'type': 'status', 'text': 'pegando o vídeo (via tikwm)...'})
        api = 'https://tikwm.com/api/?hd=1&url=' + urllib.parse.quote(link, safe='')
        req = urllib.request.Request(api, headers={'User-Agent': SHOP_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read(4 * 1024 * 1024))
        d = (data or {}).get('data') or {}
        play = d.get('hdplay') or d.get('play') or d.get('wmplay')
        if not play:
            return False
        if play.startswith('/'):
            play = 'https://tikwm.com' + play
        if not _shop_url_ok(play):   # nao segue URL para host interno (SSRF)
            return False
        out = os.path.join(out_dir, '1.mp4')
        vreq = urllib.request.Request(play, headers={'User-Agent': SHOP_UA,
                                                     'Referer': 'https://tikwm.com/'})
        with urllib.request.urlopen(vreq, timeout=90) as vr, open(out, 'wb') as f:
            shutil.copyfileobj(vr, f)
        return os.path.getsize(out) > 0
    except Exception:
        return False

# ============================================================================
# SHOPIFY: cola link de produto/colecao -> baixa TODAS as fotos (e videos) por
# produto e entrega um ZIP (pasta por produto dentro). Funciona em qualquer loja.
# ============================================================================
SHOP_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')


class _ShopSafeRedirect(urllib.request.HTTPRedirectHandler):
    """Segue redirect SO se o destino for host publico. Deixa lojas Shopify que
    redirecionam para o dominio canonico funcionarem, mas bloqueia redirect para
    host interno (169.254.x, 127.0.0.1) — protege contra SSRF."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _shop_url_ok(newurl):
            raise ValueError('redirect para host nao permitido')
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


_SHOP_OPENER = urllib.request.build_opener(_ShopSafeRedirect)


def _shop_json(url, timeout=12):
    if not _shop_url_ok(url):   # bloqueia deteccao apontada para host interno (SSRF)
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': SHOP_UA, 'Accept': 'application/json'})
        with _SHOP_OPENER.open(req, timeout=timeout) as r:
            if getattr(r, 'status', 200) != 200:
                return None
            data = r.read(64 * 1024 * 1024)   # colecao de 250 produtos pode ser grande; teto generoso
        return json.loads(data)
    except Exception:
        return None


def _shop_abs(u):
    u = (u or '').strip()
    return ('https:' + u) if u.startswith('//') else u


def shop_sanitize(name):
    name = (name or '').strip()
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip().strip('.')
    return name[:80] or 'produto'


def _shop_videos(handle, host):
    vids = []
    js = _shop_json(host + '/products/' + handle + '.js')
    for m in (js or {}).get('media', []) or []:
        if m.get('media_type') == 'video':
            best, bh = None, -1
            for s in (m.get('sources') or []):
                tag = (str(s.get('mime_type', '')) + ' ' + str(s.get('format', ''))).lower()
                if 'mp4' in tag:
                    h = s.get('height') or 0
                    if h > bh:
                        bh, best = h, s.get('url')
            if not best and (m.get('sources')):
                best = m['sources'][-1].get('url')
            if best:
                vids.append(_shop_abs(best))
    return vids


def _shop_product(prod):
    title = prod.get('title') or prod.get('handle') or 'produto'
    handle = prod.get('handle') or ''
    images = []
    for im in prod.get('images', []) or []:
        s = im.get('src') if isinstance(im, dict) else im
        if s:
            images.append(_shop_abs(s))
    return {'title': title, 'handle': handle, 'images': images, 'videos': []}


def _shop_fill_videos(produtos, host):
    def fill(p):
        if p.get('handle'):
            try:
                p['videos'] = _shop_videos(p['handle'], host)
            except Exception:
                pass
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(fill, produtos))
    except Exception:
        for p in produtos:
            fill(p)


def shopify_expand(link, want_video=False):
    """Link Shopify (produto ou colecao) -> [{title,handle,images,videos}] ou None."""
    try:
        p = urllib.parse.urlparse(link)
        if not p.netloc:
            return None
        host = (p.scheme or 'https') + '://' + p.netloc
        path = p.path or ''
        m = re.search(r'/products/([^/?#]+)', path)
        if m:
            data = _shop_json(host + '/products/' + m.group(1) + '.json')
            if isinstance(data, dict) and data.get('product'):
                prods = [_shop_product(data['product'])]
                if want_video:
                    _shop_fill_videos(prods, host)
                return prods
            return None
        m = re.search(r'/collections/([^/?#]+)', path)
        if m:
            col = m.group(1)
            produtos, page = [], 1
            while page <= 40 and len(produtos) < 500:   # teto de seguranca de produtos por lote
                data = _shop_json(host + '/collections/' + col + '/products.json?limit=250&page=' + str(page))
                items = (data or {}).get('products') if isinstance(data, dict) else None
                if not items:
                    break
                for prod in items:
                    produtos.append(_shop_product(prod))
                if len(items) < 250:
                    break
                page += 1
            produtos = produtos[:500]
            # Buscar video exige 1 requisicao por produto; so vale em colecoes
            # pequenas, senao trava colecoes gigantes dentro de um unico request.
            if produtos and want_video and len(produtos) <= 30:
                _shop_fill_videos(produtos, host)
            return produtos or None
    except Exception:
        return None
    return None


def shop_pick_ext(url):
    tail = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]
    m = re.search(r'\.([a-zA-Z0-9]{2,5})$', tail)
    ext = (m.group(1).lower() if m else 'jpg')
    return 'jpg' if ext == 'jpeg' else ext


def _shop_url_ok(url):
    """So permite http/https apontando para host publico. Barra file://, IP interno,
    loopback e o endpoint de metadados da nuvem (protege contra SSRF no /produto)."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:   # so IP publico (bloqueia privado/loopback/link-local/reservado/CGNAT 100.64)
                return False
        return True
    except Exception:
        return False


def shop_download(url, dest_path, referer=None, timeout=60, max_bytes=300 * 1024 * 1024):
    if not _shop_url_ok(url):
        raise ValueError('url nao permitida')
    headers = {'User-Agent': SHOP_UA,
               'Accept': 'image/avif,image/webp,image/*,video/*,*/*;q=0.8'}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    total = 0
    try:
        with _SHOP_OPENER.open(req, timeout=timeout) as r, open(dest_path, 'wb') as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError('arquivo muito grande')
                f.write(chunk)
        if total < 100:
            raise ValueError('conteudo vazio')
        return total
    except Exception:
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)   # nunca deixa arquivo parcial/corrompido no ZIP/pasta
        except OSError:
            pass
        raise


def _shop_download_all(produtos, emit, referer, limpar=True):
    """Baixa todos os produtos numa pasta temp (uma subpasta por produto).
    Retorna (work_dir, quantos_ok, total_prod)."""
    work = tempfile.mkdtemp(prefix='shop_')
    tasks, used = [], set()
    for prod in produtos:
        name = shop_sanitize(prod['title'])
        base, k = name, 2
        while name in used:
            name = base + ' (' + str(k) + ')'; k += 1
        used.add(name)
        folder = os.path.join(work, name)
        os.makedirs(folder, exist_ok=True)
        n = 1
        for u in prod['images']:
            tasks.append((folder, n, u, False)); n += 1
        for u in prod['videos']:
            tasks.append((folder, n, u, True)); n += 1
    total = len(tasks)
    state = {'n': 0, 'ok': 0, 'bytes': 0}
    cap = 3 * 1024 * 1024 * 1024   # teto total por lote (anti-abuso / disco da nuvem)
    lock = threading.Lock()

    def work_one(t):
        folder, num, url, is_vid = t
        with lock:
            if state['bytes'] > cap:   # ja atingiu o teto total do lote: para de baixar
                state['n'] += 1
                return
        ext = 'mp4' if is_vid else shop_pick_ext(url)
        out = os.path.join(folder, str(num) + '.' + ext)
        ok, sz = False, 0
        try:
            sz = shop_download(url, out, referer=referer)
            if limpar:
                clean_meta(out)
            ok = True
        except Exception:
            pass
        with lock:
            state['n'] += 1
            state['bytes'] += sz
            if ok:
                state['ok'] += 1
            pct = int(state['n'] / max(total, 1) * 100)
        if emit:
            emit({'type': 'progress', 'percent': pct})

    if total:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(work_one, tasks))
    return work, state['ok'], len(produtos)


def _shop_make_zip(work, zip_path):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as z:
        for root, _, files in os.walk(work):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, work)
                z.write(full, rel)


def handle_shopify_cloud(link, produtos, emit, job_dir, job_id, limpar=True):
    """Baixa os produtos e entrega um ZIP (pasta por produto) via o job atual."""
    total_prod = len(produtos)
    emit({'type': 'start', 'link': link, 'source': 'Shopify'})
    emit({'type': 'kind', 'kind': 'Shopify · ' + str(total_prod) + ' produto(s)'})
    work, okc, _ = _shop_download_all(produtos, emit, referer=link, limpar=limpar)
    try:
        if not okc:
            emit({'type': 'done', 'ok': False, 'msg': 'nao consegui baixar as fotos deste link.'})
            return
        p = urllib.parse.urlparse(link)
        m = re.search(r'/collections/([^/?#]+)', p.path or '')
        if total_prod == 1:
            zipbase = shop_sanitize(produtos[0]['title'])
        else:
            zipbase = shop_sanitize(m.group(1) if m else 'produtos')
        # nome do ZIP so em ASCII: o endpoint /file usa header latin-1 e travaria com emoji/acento
        zipbase = re.sub(r'[^A-Za-z0-9 ._-]', '_', zipbase).strip() or 'produtos'
        # download longo pode passar do TTL do job: renova e garante a pasta antes de gravar
        with _jobs_lock:
            j = _jobs.get(job_id)
            if j:
                j['expires'] = time.time() + JOB_TTL
            else:
                _jobs[job_id] = {'dir': job_dir, 'files': [], 'expires': time.time() + JOB_TTL}
        os.makedirs(job_dir, exist_ok=True)
        emit({'type': 'status', 'text': 'compactando em ZIP...'})
        zip_path = os.path.join(job_dir, zipbase + '.zip')
        k = 2
        while os.path.exists(zip_path):   # nao sobrescrever ZIP de outro link do mesmo lote
            zip_path = os.path.join(job_dir, zipbase + ' (' + str(k) + ').zip'); k += 1
        _shop_make_zip(work, zip_path)
        _job_add_file(job_id, zip_path)
        emit({'type': 'done', 'ok': True, 'saved': [os.path.basename(zip_path)],
              'job_id': job_id})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def handle_one(link, emit, mode='av', limpar=False, job_id=None, user=None, do_convert=True):
    link = normalize_link(link)
    if not _shop_url_ok(link):   # bloqueia SSRF: link para IP interno/loopback/metadados
        emit({'type': 'start', 'link': link, 'source': detect_source(link)})
        emit({'type': 'done', 'ok': False, 'msg': 'Link nao permitido (endereco interno ou invalido).'})
        return
    src  = detect_source(link)
    clean_link = link.split('?')[0] if src == 'Instagram' else link
    emit({'type': 'start', 'link': link, 'source': src})
    tmp = tempfile.mkdtemp()
    try:
        err = ''
        if GIF_RE.search(link):
            emit({'type': 'kind', 'kind': 'GIF'})
            _curl_run(clean_link, tmp, gif=True, emit=emit)
        elif IMG_RE.search(link):
            emit({'type': 'kind', 'kind': 'Imagem'})
            _curl_run(clean_link, tmp, emit=emit)
        elif src == 'Instagram' and mode != 'audio_only':
            # Carrossel do Instagram (fotos + videos juntos): o gallery-dl pega
            # TODOS os itens do post. O yt-dlp sozinho so traz o video e ignora as
            # fotos, por isso aqui ele e so o plano B (ex.: reel de video puro).
            emit({'type': 'kind', 'kind': 'Instagram (carrossel)'})
            rc, err = _gallery_run(clean_link, tmp, emit, user)
            if not collect(tmp):
                emit({'type': 'kind', 'kind': 'Vídeo + áudio (MP4)'})
                rc, err = _ytdlp_run(VIDEO_AV, clean_link, tmp, '%(autonumber)d.%(ext)s', emit, user)
            for f in os.listdir(tmp):
                if f.endswith('.mp4'):
                    _ensure_quicktime(os.path.join(tmp, f), emit, do_convert)
        else:
            emit({'type': 'kind', 'kind': 'Vídeo + áudio (MP4)'})
            if mode == 'audio_only':
                emit({'type': 'kind', 'kind': 'Só áudio (MP3)'})
                rc, err = _ytdlp_run(['-x', '--audio-format', 'mp3', '--audio-quality', '0'],
                                     clean_link, tmp, '%(autonumber)d.%(ext)s', emit, user)
            else:
                rc, err = _ytdlp_run(VIDEO_AV, clean_link, tmp, '%(autonumber)d.%(ext)s', emit, user)
                # TikTok as vezes so traz a faixa de audio pelo yt-dlp (anti-robo):
                # cai pro tikwm p/ pegar o MP4 de verdade.
                if src == 'TikTok' and not any(f.lower().endswith(('.mp4', '.mov')) for f in os.listdir(tmp)):
                    if _tikwm_video(clean_link, tmp, emit):
                        for f in os.listdir(tmp):   # entrega so o video, sem o audio solto
                            if f.lower().endswith(('.m4a', '.mp3')):
                                try: os.remove(os.path.join(tmp, f))
                                except Exception: pass
                for f in os.listdir(tmp):
                    if f.endswith('.mp4'):
                        _ensure_quicktime(os.path.join(tmp, f), emit, do_convert)
                if rc != 0 and not collect(tmp):
                    emit({'type': 'kind', 'kind': 'Imagem'})
                    _gallery_run(clean_link, tmp, emit, user)

        files = collect(tmp)
        if limpar and files:
            emit({'type': 'status', 'text': 'limpando metadados...'})
            for f in files:
                clean_meta(f)

        if not files:
            short = (err or 'falhou').splitlines()
            emit({'type': 'done', 'ok': False, 'msg': (short[-1] if short else 'falhou')[:240]})
            return

        saved_names = []
        for f in files:
            name = os.path.basename(f)
            if job_id:
                dest_dir = _job_get(job_id)['dir'] if _job_get(job_id) else tmp
                dest = os.path.join(dest_dir, name)
                shutil.move(f, dest)
                _job_add_file(job_id, dest)
            saved_names.append(name)

        emit({'type': 'done', 'ok': True, 'saved': saved_names, 'job_id': job_id})
    except Exception as e:
        emit({'type': 'done', 'ok': False, 'msg': str(e)[:240]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- handler HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Expose-Headers', 'Content-Disposition')

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Content-Length', '0'); self.end_headers()

    def _err(self, code, msg):
        b = json.dumps({'ok': False, 'msg': msg}).encode('utf-8')
        self.send_response(code); self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def _grab(self):
        """Baixa 1 video (TikTok/IG/etc) e devolve o MP4 direto, sem marca d'agua e
        SEM conversao (leve). Feito pro Atalho da Apple salvar no rolo com 1 toque:
        GET /grab?url=<link>  ->  bytes do video (Content-Type video/mp4)."""
        qs   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw  = (qs.get('url') or [''])[0]
        link = normalize_link(raw) if raw else ''
        if not link or not _shop_url_ok(link) or detect_source(link) == 'Web':
            self._err(400, 'link invalido'); return
        tmp = tempfile.mkdtemp()
        try:
            _ytdlp_run(VIDEO_AV, link, tmp, '%(id)s.%(ext)s', lambda o: None)
            vids = [f for f in collect(tmp)
                    if f.lower().rsplit('.', 1)[-1] in ('mp4', 'mov', 'webm', 'mkv')]
            if not vids and detect_source(link) == 'TikTok':   # yt-dlp so trouxe audio -> tikwm
                _tikwm_video(link, tmp)
                vids = [f for f in collect(tmp)
                        if f.lower().rsplit('.', 1)[-1] in ('mp4', 'mov', 'webm', 'mkv')]
            if not vids:
                self._err(502, 'nao consegui baixar esse link'); return
            path = vids[0]
            size = os.path.getsize(path)
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Disposition', 'inline; filename="video.mp4"')
            self.send_header('Content-Length', str(size)); self.end_headers()
            with open(path, 'rb') as f:
                while True:
                    c = f.read(1 << 20)
                    if not c: break
                    self.wfile.write(c)
        except Exception as e:
            try: self._err(500, str(e)[:150])
            except Exception: pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def do_GET(self):
        if self.path.startswith('/ping'):
            b = b'{"ok":true,"cloud":true}'
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return

        if self.path.startswith('/grab'):
            self._grab(); return

        if self.path.startswith('/bookmarklet'):
            # pagina que instala o "botao magico" (bookmarklet)
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bookmarklet.html')
            try:
                with open(path, 'rb') as f:
                    b = f.read()
            except Exception:
                self.send_error(404); return
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return

        if self.path.startswith('/cookie-status'):
            qs    = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cpath = _cookie_path((qs.get('u') or [''])[0])   # status dos cookies deste usuario
            has_file = os.path.exists(cpath)
            count = 0
            has_session = False
            if has_file:
                try:
                    with open(cpath, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    count = sum(1 for l in content.splitlines() if l.strip() and not l.startswith('#'))
                    has_session = 'sessionid' in content
                except Exception:
                    pass
            b = json.dumps({'ok': True, 'has_file': has_file,
                            'has_session': has_session, 'count': count}).encode('utf-8')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return

        # /file/{job_id}/{filename}
        m = re.match(r'^/file/([^/]+)/(.+)$', self.path)
        if m:
            jid, fname = m.group(1), urllib.parse.unquote(m.group(2))
            job = _job_get(jid)
            if not job:
                self._err(404, 'Job expirado ou não encontrado.'); return
            path = os.path.join(job['dir'], os.path.basename(fname))
            if not os.path.exists(path):
                self._err(404, 'Arquivo não encontrado.'); return
            size = os.path.getsize(path)
            safe = fname.replace('"', '')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', _cd(safe))
            self.send_header('Content-Length', str(size)); self.end_headers()
            with open(path, 'rb') as f:
                while True:
                    c = f.read(1 << 20)
                    if not c: break
                    self.wfile.write(c)
            return

        self.send_error(404)

    def _recv(self):
        qs   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = urllib.parse.unquote((qs.get('name') or ['arquivo'])[0])
        base = os.path.basename(name).replace('"', '').replace('\n', '').strip() or 'arquivo'
        ln   = int(self.headers.get('Content-Length', 0))
        if ln > MAX_UPLOAD:
            return None, None
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, base)
        rem = ln
        with open(src, 'wb') as f:
            while rem > 0:
                c = self.rfile.read(min(1 << 20, rem))
                if not c: break
                f.write(c); rem -= len(c)
        return tmp, src

    def do_POST(self):
        if self.path.startswith('/cookie-save'):
            qs   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            dest = _cookie_path((qs.get('u') or [''])[0])   # cookies isolados por usuario
            ln = int(self.headers.get('Content-Length', 0))
            if ln > 10 * 1024 * 1024:
                self._err(413, 'Arquivo muito grande.'); return
            data = self.rfile.read(ln)
            text = data.decode('utf-8', errors='replace')
            if 'sessionid' not in text:
                b = json.dumps({'ok': False, 'msg': 'Cookie sessionid não encontrado — verifique se você está logado no Instagram ao exportar.'}).encode('utf-8')
            else:
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                except Exception:
                    pass
                with open(dest, 'wb') as f:
                    f.write(data)
                count = sum(1 for l in text.splitlines() if l.strip() and not l.startswith('#'))
                b = json.dumps({'ok': True, 'count': count}).encode('utf-8')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if self.path.startswith('/meta'):
            self._meta(); return
        if self.path.startswith('/clean'):
            self._clean(); return
        if self.path.startswith('/produto'):
            self._produto(); return
        if self.path == '/download':
            self._download(); return
        self.send_error(404)

    def _download(self):
        ln    = int(self.headers.get('Content-Length', 0))
        data  = json.loads(self.rfile.read(ln) or b'{}')
        links = [l.strip() for l in data.get('links', []) if l.strip()]
        mode  = data.get('mode', 'av')
        if mode not in ('av', 'video_only', 'audio_only', 'separate_zip'):
            mode = 'av'
        limpar = bool(data.get('clean'))
        user   = data.get('user')   # chave por usuario -> cookies isolados
        # convert=False (celular): NAO reencoda HEVC->H.264. iPhone/Android postam
        # HEVC nativo, e a conversao era o passo lento que travava no telefone.
        do_convert = bool(data.get('convert', True))

        self.send_response(200); self._cors()
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close'); self.end_headers()

        lock = threading.Lock()

        def emit(obj, idx=None):
            if idx is not None:
                obj = dict(obj); obj['index'] = idx
            try:
                with lock:
                    self.wfile.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))
                    self.wfile.flush()
            except Exception:
                pass

        job_id = str(uuid.uuid4())
        d = tempfile.mkdtemp(prefix='job_')
        with _jobs_lock:
            _jobs[job_id] = {'dir': d, 'files': [], 'expires': time.time() + JOB_TTL}

        emit({'type': 'init', 'total': len(links)})

        # Batimento: manda um ping a cada 4s pra manter a conexao viva durante
        # passos silenciosos (conversao, limpar metadados, gallery-dl). Sem isso o
        # iOS/operadora derruba a conexao no silencio e a tela trava pra sempre.
        _hb_stop = threading.Event()
        def _heartbeat():
            while not _hb_stop.wait(4):
                emit({'type': 'ping'})
        threading.Thread(target=_heartbeat, daemon=True).start()

        for i, link in enumerate(links):
            emit_i = lambda o, i=i: emit(o, i)
            try:
                # Loja Shopify (produto/colecao): baixa por produto e entrega um ZIP.
                # So desvia se realmente encontrou foto/video; senao segue o fluxo normal.
                prods = shopify_expand(normalize_link(link), want_video=True)
                has_media = prods and any(p['images'] or p['videos'] for p in prods)
                if has_media:
                    handle_shopify_cloud(normalize_link(link), prods, emit_i, d, job_id, limpar=True)
                else:
                    handle_one(link, emit_i, mode=mode, limpar=limpar, job_id=job_id, user=user, do_convert=do_convert)
            except Exception as ex:   # um link com erro nao derruba o lote inteiro
                try:
                    emit_i({'type': 'done', 'ok': False, 'msg': str(ex)[:200]})
                except Exception:
                    pass
        _hb_stop.set()   # desliga o batimento
        emit({'type': 'all_done'})

    def _produto(self):
        """Botao magico (bookmarklet): recebe {title, images, videos, page_url},
        baixa tudo, limpa metadados e devolve um ZIP direto para download."""
        try:
            ln = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            ln = 0
        if ln <= 0 or ln > 100 * 1024 * 1024:   # comporta varias fotos enviadas em base64
            self._err(413, 'payload invalido'); return
        raw = self.rfile.read(ln)
        payload = None
        try:
            txt = raw.decode('utf-8', 'replace')
            if txt.lstrip().startswith('{'):
                payload = json.loads(txt)
            else:
                qs = urllib.parse.parse_qs(txt)
                if 'payload' in qs:
                    payload = json.loads(qs['payload'][0])
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            self._err(400, 'payload invalido'); return
        title = shop_sanitize(payload.get('title') or 'produto')
        referer = payload.get('page_url') or None
        images = [u for u in (payload.get('images') or []) if u]
        videos = [u for u in (payload.get('videos') or []) if u]
        blobs = payload.get('blobs') or []   # fotos ja baixadas pelo navegador (B5)
        if len(images) + len(videos) + len(blobs) > 300:   # teto por pedido (anti-abuso)
            self._err(413, 'itens demais nesta pagina.'); return
        limpar = bool(payload.get('clean', True))
        prod = {'title': title, 'handle': '', 'images': images, 'videos': videos}
        work, okc, _ = _shop_download_all([prod], None, referer=referer, limpar=limpar)
        # anexa as fotos enviadas em bytes (quando o servidor nao consegue baixar pela URL)
        if blobs:
            folder = os.path.join(work, shop_sanitize(title))
            os.makedirs(folder, exist_ok=True)
            n = 1   # continua do maior numero ja usado (nao sobrescreve fotos com falha no meio)
            for _f in os.listdir(folder):
                _m = re.match(r'(\d+)', _f)
                if _m:
                    n = max(n, int(_m.group(1)) + 1)
            for bl in blobs:
                try:
                    raw = base64.b64decode(bl.get('b64') or '')
                    if len(raw) < 512:
                        continue
                    nm = bl.get('name') or ''
                    ext = shop_pick_ext(nm) if '.' in nm else 'jpg'
                    out = os.path.join(folder, str(n) + '.' + ext)
                    with open(out, 'wb') as f:
                        f.write(raw)
                    if limpar:
                        clean_meta(out)
                    okc += 1; n += 1
                except Exception:
                    pass
        if not okc:
            shutil.rmtree(work, ignore_errors=True)
            self._err(502, 'nao consegui baixar as imagens desta pagina.'); return
        fd, zip_path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        try:
            _shop_make_zip(work, zip_path)
            size = os.path.getsize(zip_path)
            # nome do arquivo: ASCII seguro no filename + versao UTF-8 (acento/emoji) no filename*
            ascii_name = (re.sub(r'[^A-Za-z0-9 ._-]', '_', title).strip() or 'produto') + '.zip'
            utf8_name = urllib.parse.quote(title + '.zip')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition',
                             'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + utf8_name)
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with open(zip_path, 'rb') as f:   # envia em pedacos (nao carrega o ZIP todo na memoria)
                while True:
                    c = f.read(65536)
                    if not c:
                        break
                    self.wfile.write(c)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            try:
                os.remove(zip_path)
            except Exception:
                pass

    def _clean(self):
        tmp, src = self._recv()
        if tmp is None:
            self._err(413, 'Arquivo acima de 300 MB.'); return
        try:
            ext = os.path.splitext(src)[1].lower()
            if ext not in IMG_CLEAN and ext not in VID_CLEAN:
                self._err(415, 'Tipo não suportado (use jpg, png, mp4, mov).'); return
            if not clean_meta(src):
                self._err(500, 'Não consegui limpar este arquivo (pode estar corrompido ou a ferramenta falhou).'); return
            root, ext2 = os.path.splitext(os.path.basename(src))
            outname = root + '_limpo' + ext2
            size = os.path.getsize(src)
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', _cd(outname))
            self.send_header('Content-Length', str(size)); self.end_headers()
            with open(src, 'rb') as f:
                while True:
                    c = f.read(1 << 20)
                    if not c: break
                    self.wfile.write(c)
        except Exception as e:
            try: self._err(500, str(e)[:200])
            except Exception: pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _meta(self):
        tmp, src = self._recv()
        if tmp is None:
            self._err(413, 'Arquivo acima de 300 MB.'); return
        try:
            data = {}
            try:
                r = subprocess.run(['exiftool', '-json', '-G1', '-a', '-u', src],
                                   capture_output=True, text=True, timeout=120)
                arr = json.loads(r.stdout) if r.stdout.strip() else []
                data = arr[0] if arr else {}
            except Exception:
                data = {}
            skip = ('File', 'System', 'ExifTool', 'JFIF')
            sensitive = []
            for k, v in data.items():
                if k == 'SourceFile': continue
                grp   = k.split(':')[0] if ':' in k else ''
                short = k.split(':')[-1]
                if grp in skip: continue
                gu = grp.upper()
                is_id = gu.startswith('IPTC') or gu.startswith('XMP') or gu.startswith('PHOTOSHOP')
                if not (SENSITIVE_RE.search(short) or is_id): continue
                sval = str(v)
                if short == 'SpecialInstructions' and sval.startswith('FBMD'):
                    sensitive.append({'tag': 'Marca do Facebook/Instagram (FBMD)',
                                      'value': 'identificador de origem / rastreio'}); continue
                if len(sval) > 140: sval = sval[:140] + '...'
                sensitive.append({'tag': short, 'value': sval})
            alli = []
            for k, v in data.items():
                if k == 'SourceFile': continue
                grp   = k.split(':')[0] if ':' in k else ''
                short = k.split(':')[-1]
                if grp in ('File', 'System', 'ExifTool'): continue
                sval = str(v)
                if len(sval) > 200: sval = sval[:200] + '...'
                alli.append({'g': grp, 't': short, 'v': sval})
            b = json.dumps({'ok': True, 'sensitive': sensitive,
                            'all': alli, 'total': len(alli)}).encode('utf-8')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            try: self._err(500, str(e)[:200])
            except Exception: pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    print(f'Motor de nuvem rodando na porta {PORT}')
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
