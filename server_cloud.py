#!/usr/bin/env python3
# Motor de NUVEM do Baixador — SO limpar / ver metadados (sem download).
# Roda no Render. O Render termina o HTTPS na frente; aqui servimos HTTP simples.
import os, re, json, shutil, tempfile, subprocess, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import limpa_midia

PORT = int(os.environ.get('PORT', '10000'))
MAX = 300 * 1024 * 1024  # 300 MB (suficiente p/ fotos e videos curtos no plano free)

IMG_CLEAN = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.heic', '.tiff', '.tif'}
VID_CLEAN = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'}
SENSITIVE_RE = re.compile(
    r'GPS|Make|Model|Date|Owner|Author|Artist|Creator|Software|Serial|Lens|'
    r'HostComputer|Copyright|Comment|Location|City|Country|State|Title|Keyword|'
    r'Subject|Description|Device|UniqueID|Rights|By-line|Credit|Instruction|'
    r'FBMD|Caption|Headline|Source|Contact|Email|Phone|Identifier|PersonI', re.I)


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
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
        return False


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.send_header('Content-Length', '0'); self.end_headers()

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
        else:
            self.send_error(404)

    def _recv(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = urllib.parse.unquote((qs.get('name') or ['arquivo'])[0])
        base = os.path.basename(name).replace('"', '').replace('\n', '').strip() or 'arquivo'
        ln = int(self.headers.get('Content-Length', 0))
        if ln > MAX:
            return None, None
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, base)
        rem = ln
        with open(src, 'wb') as f:
            while rem > 0:
                c = self.rfile.read(min(1 << 20, rem))
                if not c:
                    break
                f.write(c); rem -= len(c)
        return tmp, src

    def do_POST(self):
        if self.path.startswith('/meta'):
            self._meta(); return
        if self.path.startswith('/clean'):
            self._clean(); return
        self.send_error(404)

    def _clean(self):
        tmp, src = self._recv()
        if tmp is None:
            self._err(413, 'Arquivo acima de 300 MB.'); return
        try:
            if not clean_meta(src):
                self._err(415, 'Tipo nao suportado (use jpg, png, mp4 ou mov).'); return
            root, ext = os.path.splitext(os.path.basename(src))
            outname = root + '_limpo' + ext
            size = os.path.getsize(src)
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', 'attachment; filename="' + outname + '"')
            self.send_header('Content-Length', str(size)); self.end_headers()
            with open(src, 'rb') as f:
                while True:
                    c = f.read(1 << 20)
                    if not c:
                        break
                    self.wfile.write(c)
        except Exception as e:
            try:
                self._err(500, str(e)[:200])
            except Exception:
                pass
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
                if k == 'SourceFile':
                    continue
                grp = k.split(':')[0] if ':' in k else ''
                short = k.split(':')[-1]
                if grp in skip:
                    continue
                gu = grp.upper()
                is_id = gu.startswith('IPTC') or gu.startswith('XMP') or gu.startswith('PHOTOSHOP')
                if not (SENSITIVE_RE.search(short) or is_id):
                    continue
                sval = str(v)
                if short == 'SpecialInstructions' and sval.startswith('FBMD'):
                    sensitive.append({'tag': 'Marca do Facebook/Instagram (FBMD)',
                                      'value': 'identificador de origem / rastreio'})
                    continue
                if len(sval) > 140:
                    sval = sval[:140] + '...'
                sensitive.append({'tag': short, 'value': sval})
            alli = []
            for k, v in data.items():
                if k == 'SourceFile':
                    continue
                grp = k.split(':')[0] if ':' in k else ''
                short = k.split(':')[-1]
                if grp in ('File', 'System', 'ExifTool'):
                    continue
                sval = str(v)
                if len(sval) > 200:
                    sval = sval[:200] + '...'
                alli.append({'g': grp, 't': short, 'v': sval})
            b = json.dumps({'ok': True, 'sensitive': sensitive, 'all': alli, 'total': len(alli)}).encode('utf-8')
            self.send_response(200); self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        except Exception as e:
            try:
                self._err(500, str(e)[:200])
            except Exception:
                pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    print('Motor de nuvem rodando na porta', PORT)
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
