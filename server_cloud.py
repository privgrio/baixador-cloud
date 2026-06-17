#!/usr/bin/env python3
# Motor de NUVEM do Baixador — download + limpar + ver metadados
# Roda no Render via Docker. HTTP simples (Render termina HTTPS na frente).
import os, re, json, shutil, tempfile, subprocess, urllib.parse, threading, time, uuid, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import limpa_midia

PORT = int(os.environ.get('PORT', '10000'))
MAX_UPLOAD = 300 * 1024 * 1024   # 300 MB para /clean e /meta
JOB_TTL    = 600                  # 10 min para baixar os arquivos do job

# Cookies do Instagram (login). Guarda no disco persistente do Render (/var/data)
# se existir, para sobreviver a restart/deploy; senao usa pasta temporaria.
COOKIE_DIR = '/var/data' if os.path.isdir('/var/data') else tempfile.gettempdir()
COOKIES    = os.path.join(COOKIE_DIR, 'cookies.txt')

def cookie_args():
    """Usa o arquivo de cookies se existir (necessario p/ foto/carrossel do Instagram)."""
    return ['--cookies', COOKIES] if os.path.exists(COOKIES) else []

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
def _ytdlp_run(extra_args, link, out_dir, out_tmpl, emit):
    cmd = (['yt-dlp', '-P', out_dir, '-o', out_tmpl, '--newline',
            '--progress-template', 'download:DLP|%(progress._percent_str)s|%(progress.eta)s']
           + cookie_args() + extra_args + [link])
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

def _gallery_run(link, out_dir, emit):
    emit({'type': 'status', 'text': 'baixando imagem...'})
    cmd = ['gallery-dl'] + cookie_args() + ['-d', out_dir, '--filename', '{num}.{extension}', link]
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

def _ensure_quicktime(path, emit):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name', '-of', 'default=nk=1:nw=1', path],
            capture_output=True, text=True)
        vcodec = r.stdout.strip()
        if vcodec == 'h264':
            return
        emit({'type': 'status', 'text': f'convertendo {vcodec}→H.264...'})
        tmp = path + '.qt.mp4'
        subprocess.run(['ffmpeg', '-y', '-i', path,
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-movflags', '+faststart', tmp],
                       capture_output=True)
        os.replace(tmp, path)
    except Exception:
        pass

def handle_one(link, emit, mode='av', limpar=False, job_id=None):
    link = normalize_link(link)
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
            rc, err = _gallery_run(clean_link, tmp, emit)
            if not collect(tmp):
                emit({'type': 'kind', 'kind': 'Vídeo + áudio (MP4)'})
                rc, err = _ytdlp_run(VIDEO_AV, clean_link, tmp, '%(autonumber)d.%(ext)s', emit)
            for f in os.listdir(tmp):
                if f.endswith('.mp4'):
                    _ensure_quicktime(os.path.join(tmp, f), emit)
        else:
            emit({'type': 'kind', 'kind': 'Vídeo + áudio (MP4)'})
            if mode == 'audio_only':
                emit({'type': 'kind', 'kind': 'Só áudio (MP3)'})
                rc, err = _ytdlp_run(['-x', '--audio-format', 'mp3', '--audio-quality', '0'],
                                     clean_link, tmp, '%(autonumber)d.%(ext)s', emit)
            else:
                rc, err = _ytdlp_run(VIDEO_AV, clean_link, tmp, '%(autonumber)d.%(ext)s', emit)
                if rc == 0:
                    for f in os.listdir(tmp):
                        if f.endswith('.mp4'):
                            _ensure_quicktime(os.path.join(tmp, f), emit)
                if rc != 0 and not collect(tmp):
                    emit({'type': 'kind', 'kind': 'Imagem'})
                    _gallery_run(clean_link, tmp, emit)

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

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Content-Length', '0'); self.end_headers()

    def _err(self, code, msg):
        b = json.dumps({'ok': False, 'msg': msg}).encode('utf-8')
        self.send_response(code); self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith('/ping'):
            b = b'{"ok":true,"cloud":true}'
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return

        if self.path.startswith('/cookie-status'):
            has_file = os.path.exists(COOKIES)
            count = 0
            has_session = False
            if has_file:
                try:
                    with open(COOKIES, 'r', encoding='utf-8', errors='replace') as f:
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
            self.send_header('Content-Disposition', f'attachment; filename="{safe}"')
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
            ln = int(self.headers.get('Content-Length', 0))
            if ln > 10 * 1024 * 1024:
                self._err(413, 'Arquivo muito grande.'); return
            data = self.rfile.read(ln)
            text = data.decode('utf-8', errors='replace')
            if 'sessionid' not in text:
                b = json.dumps({'ok': False, 'msg': 'Cookie sessionid não encontrado — verifique se você está logado no Instagram ao exportar.'}).encode('utf-8')
            else:
                try:
                    os.makedirs(os.path.dirname(COOKIES), exist_ok=True)
                except Exception:
                    pass
                with open(COOKIES, 'wb') as f:
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
        for i, link in enumerate(links):
            handle_one(link, lambda o, i=i: emit(o, i), mode=mode, limpar=limpar, job_id=job_id)
        emit({'type': 'all_done'})

    def _clean(self):
        tmp, src = self._recv()
        if tmp is None:
            self._err(413, 'Arquivo acima de 300 MB.'); return
        try:
            if not clean_meta(src):
                self._err(415, 'Tipo não suportado.'); return
            root, ext = os.path.splitext(os.path.basename(src))
            outname = root + '_limpo' + ext
            size = os.path.getsize(src)
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="{outname}"')
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
                                   capture_output=True, text=True)
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
