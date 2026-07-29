#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Exportador de Sites (motor de reserva, na nuvem).

Mesma ideia do motor do Mac (~/ExportadorSites/server.py): o wget baixa uma
copia offline do site (paginas + imagens + CSS/JS) e devolve um zip.

Aqui e so a RESERVA: a aba Exportador da Esteira tenta o Mac primeiro e so cai
pra ca quando o Mac esta desligado (celular, por exemplo). Por isso os limites
sao apertados, o disco do Render e de 1 GB dividido com o Baixador:

  - 1 exportacao por vez
  - teto de 400 MB por site
  - 12 minutos de limite
  - a copia crua e apagada logo depois do zip
  - faxina de tudo que passa de 6 horas
  - teto diario, pra banda de saida nao virar conta

Este arquivo e um modulo: quem serve as rotas e o server_cloud.py.
"""

import os
import re
import json
import time
import shutil
import threading
import subprocess
import urllib.parse
import urllib.request

MOTOR_VERSION = 2

EXPORT_DIR = '/var/data/exports' if os.path.isdir('/var/data') else '/tmp/exports'
WGET = shutil.which('wget') or '/usr/bin/wget'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')

MAX_MB = int(os.environ.get('EXPORT_MAX_MB', '400'))
MAX_JOBS = 1
LIMITE_MIN = int(os.environ.get('EXPORT_LIMITE_MIN', '12'))
MAX_POR_DIA = int(os.environ.get('EXPORT_MAX_DIA', '25'))
FOLGA_DISCO_MB = 600          # nao comeca se sobrar menos que isso no disco
GUARDA_HORAS = 6              # faxina do que passa disso

# Quem pode MANDAR exportar. O navegador preenche o Origin sozinho e a pagina
# nao consegue mentir nele, entao isso ja tira o motor da mao de quem so achou
# o endereco. Se um dia o Gabriel criar a variavel EXPORT_TOKEN no painel do
# Render, o motor passa a exigir tambem o token (trava mais forte).
ORIGENS_OK = (
    'https://app.aclickmarketing.com',
    'https://esteira-shopify.vercel.app',
    'http://localhost',
    'https://localhost',
    'http://127.0.0.1',
    'https://127.0.0.1',
)
TOKEN = os.environ.get('EXPORT_TOKEN', '').strip()

os.makedirs(EXPORT_DIR, exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()
HISTORICO = []                # carimbos das exportacoes do dia (teto diario)


def slug(texto):
    texto = re.sub(r'[^a-zA-Z0-9._-]+', '-', texto).strip('-')
    return texto[:60] or 'site'


def normaliza_url(url):
    url = (url or '').strip()
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    return url


def hosts_da_home(url):
    """De quais CDNs vem os ARQUIVOS da home (imagens/css/js/fontes). So olha
    assets, nunca link de navegacao, pra o wget nao sair andando pra fora."""
    hosts = set()

    def add(m):
        if not m:
            return
        m = m.strip().strip('\'"')
        if m.startswith('//'):
            m = 'https:' + m
        if m.startswith('http'):
            h = urllib.parse.urlparse(m).netloc.lower()
            if h:
                hosts.add(h)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read(1500000).decode('utf-8', 'ignore')
        for m in re.findall(r'\bsrc\s*=\s*["\']([^"\']+)["\']', html):
            add(m)
        for tag in re.findall(r'<link\b[^>]*>', html, re.I):
            mm = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
            if mm:
                add(mm.group(1))
        for ss in re.findall(r'\bsrcset\s*=\s*["\']([^"\']+)["\']', html):
            for parte in ss.split(','):
                add(parte.strip().split(' ')[0])
        for m in re.findall(r'url\(([^)]+)\)', html):
            add(m)
    except Exception:
        pass
    return hosts


def conta_paginas_sitemap(url):
    base = '{u.scheme}://{u.netloc}'.format(u=urllib.parse.urlparse(url))

    def baixa(u):
        try:
            req = urllib.request.Request(u, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read(4000000)
            if data[:2] == b'\x1f\x8b':
                return ''
            return data.decode('utf-8', 'ignore')
        except Exception:
            return ''
    raiz = baixa(base + '/sitemap.xml')
    if not raiz:
        return None
    locs = [l.replace('&amp;', '&') for l in re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', raiz)]
    if '<sitemapindex' in raiz:
        total, achou = 0, False
        for sub in locs[:25]:
            n = len(re.findall(r'<loc>', baixa(sub)))
            if n:
                total += n
                achou = True
        return total if achou else None
    return len(locs) if locs else None


def conta_arquivos(diretorio):
    paginas = imagens = outros = 0
    bytes_total = 0
    recentes = []
    for dp, _, arqs in os.walk(diretorio):
        for a in arqs:
            if a == '_export.log':
                continue
            caminho = os.path.join(dp, a)
            try:
                st = os.stat(caminho)
                mt, tam = st.st_mtime, st.st_size
            except OSError:
                mt, tam = 0, 0
            bytes_total += tam
            low = a.lower()
            if low.endswith('.html') or low.endswith('.htm'):
                paginas += 1
            elif re.search(r'\.(jpg|jpeg|png|webp|gif|svg|avif|ico)', low):
                imagens += 1
            else:
                outros += 1
            recentes.append((mt, a))
    recentes.sort(key=lambda x: x[0])
    return paginas, imagens, outros, [a for _, a in recentes[-16:]], bytes_total


def roda_exportacao(job):
    url = job['url']
    diretorio = job['dir']
    host = urllib.parse.urlparse(url).netloc

    job['phase'] = 'preparando'
    dominios = set(hosts_da_home(url))
    dominios.add(host)
    dominios.add(host[4:] if host.startswith('www.') else 'www.' + host)
    for c in ('cdn.shopify.com', 'cdn.shopifycdn.net', 'fonts.shopifycdn.com',
              'fonts.gstatic.com', 'fonts.googleapis.com', 'cdn.jsdelivr.net'):
        dominios.add(c)
    dominios = ','.join(sorted(d for d in dominios if d))

    try:
        job['target_pages'] = conta_paginas_sitemap(url)
    except Exception:
        job['target_pages'] = None

    job['phase'] = 'baixando'
    cmd = [
        WGET, '--mirror', '--convert-links', '--adjust-extension',
        '--page-requisites', '--no-parent', '--restrict-file-names=windows',
        '-e', 'robots=off', '--span-hosts', '--domains=' + dominios,
        '--reject-regex',
        r'(/cart|/checkout|/account|/challenge|/apps/|/services/|sort_by=|/admin|logout|add-to-cart)',
        '--wait=0.2', '--random-wait', '--tries=3', '--timeout=25',
        '--no-check-certificate', '-Q', '%dm' % MAX_MB, '-U', UA,
        '--directory-prefix=' + diretorio, url,
    ]
    with open(os.path.join(diretorio, '_export.log'), 'w') as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        limite = time.time() + LIMITE_MIN * 60
        while proc.poll() is None:
            if job.get('cancel'):
                proc.terminate()
                break
            if time.time() > limite:
                proc.terminate()
                job['aviso'] = 'Parou no limite de %d min (site muito grande).' % LIMITE_MIN
                break
            time.sleep(1)
        proc.wait()

    if job.get('cancel'):
        job['phase'] = 'cancelado'
        job['stats'] = conta_arquivos(diretorio)
        shutil.rmtree(diretorio, ignore_errors=True)
        job['fim'] = time.time()
        return

    job['phase'] = 'compactando'
    job['stats'] = conta_arquivos(diretorio)
    try:
        zip_base = os.path.join(EXPORT_DIR, job['folder'])
        shutil.make_archive(zip_base, 'zip', root_dir=diretorio)
        job['zip'] = zip_base + '.zip'
        job['zip_bytes'] = os.path.getsize(job['zip'])
    except Exception as e:
        job['erro'] = 'Falha ao compactar: %s' % e

    p, i = job['stats'][0], job['stats'][1]
    if p == 0 and i == 0:
        job['phase'] = 'erro'
        job['erro'] = job.get('erro') or 'Nao consegui baixar nada desse link. Confere o endereco.'
    else:
        job['phase'] = 'concluido'
    job['fim'] = time.time()

    # Disco de 1 GB dividido com o Baixador: depois do zip a copia crua so atrapalha.
    shutil.rmtree(diretorio, ignore_errors=True)


def percentual(job):
    fase = job['phase']
    if fase == 'preparando':
        return 3
    if fase == 'compactando':
        return 96
    if fase in ('concluido', 'erro', 'cancelado'):
        return 100
    p, i, o, _, _ = conta_arquivos(job['dir'])
    alvo = job.get('target_pages')
    if alvo and alvo > 0:
        return int(5 + 88 * min(1.0, p / alvo))
    total = p + i + o
    return int(5 + 85 * (1 - 1.0 / (1 + total / 60.0)))


def status_job(job):
    if job.get('stats'):
        p, i, o, ultimos, bytes_total = job['stats']
    else:
        p, i, o, ultimos, bytes_total = conta_arquivos(job['dir'])
    return {
        'id': job['id'], 'url': job['url'], 'phase': job['phase'],
        'pages': p, 'assets': i + o, 'images': i,
        'bytes': job.get('zip_bytes') or bytes_total,
        'target_pages': job.get('target_pages'),
        'percent': percentual(job), 'feed': ultimos,
        'zip': bool(job.get('zip')), 'home': False, 'cloud': True,
        'erro': job.get('erro'), 'aviso': job.get('aviso'),
    }


def _ativos():
    return [j for j in JOBS.values()
            if j.get('phase') in ('preparando', 'baixando', 'compactando')]


def _disco_livre_mb():
    try:
        return shutil.disk_usage(EXPORT_DIR).free / (1024 * 1024)
    except Exception:
        return 9999


def _origem_ok(handler):
    if TOKEN:
        enviado = (handler.headers.get('X-Token') or '').strip()
        if not enviado:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
            enviado = (qs.get('tk') or [''])[0].strip()
        if enviado != TOKEN:
            return False
    origem = (handler.headers.get('Origin') or handler.headers.get('Referer') or '').strip()
    if not origem:
        return False
    return any(origem.startswith(o) for o in ORIGENS_OK)


def cria_job(url, handler):
    if not _origem_ok(handler):
        return {'erro': 'Sem permissao pra usar este motor.'}, 403
    url = normaliza_url(url)
    if not re.match(r'^https?://[^/\s.]+\.[^/\s]+', url):
        return {'erro': 'Link invalido. Ex: minhaloja.com'}, 400
    if _ativos():
        return {'erro': 'O motor da nuvem faz uma de cada vez. Espera a atual terminar.'}, 429
    agora = time.time()
    HISTORICO[:] = [t for t in HISTORICO if agora - t < 86400]
    if len(HISTORICO) >= MAX_POR_DIA:
        return {'erro': 'Limite de %d exportacoes por dia na nuvem. Usa o Mac.' % MAX_POR_DIA}, 429
    if _disco_livre_mb() < FOLGA_DISCO_MB:
        faxina(forcar=True)
        if _disco_livre_mb() < FOLGA_DISCO_MB:
            return {'erro': 'Disco da nuvem cheio. Tenta de novo em alguns minutos.'}, 507

    host = urllib.parse.urlparse(url).netloc
    folder = '%s_%s' % (slug(host), time.strftime('%Y%m%d-%H%M%S'))
    diretorio = os.path.join(EXPORT_DIR, folder)
    os.makedirs(diretorio, exist_ok=True)
    jid = '%s-%d' % (slug(host), int(time.time() * 1000) % 1000000)
    job = {'id': jid, 'url': url, 'dir': diretorio, 'folder': folder,
           'phase': 'preparando', 'inicio': agora}
    with JOBS_LOCK:
        JOBS[jid] = job
    HISTORICO.append(agora)
    threading.Thread(target=roda_exportacao, args=(job,), daemon=True).start()
    return {'id': jid}, 200


def faxina(forcar=False):
    limite = time.time() - (0 if forcar else GUARDA_HORAS * 3600)
    ativos = set(j['dir'] for j in _ativos())
    for nome in os.listdir(EXPORT_DIR):
        caminho = os.path.join(EXPORT_DIR, nome)
        if caminho in ativos:
            continue
        try:
            if os.path.getmtime(caminho) > limite:
                continue
            if os.path.isdir(caminho):
                shutil.rmtree(caminho, ignore_errors=True)
            else:
                os.remove(caminho)
        except OSError:
            pass


def faxina_loop():
    while True:
        time.sleep(900)
        try:
            faxina()
        except Exception:
            pass


# ---------------------------------------------------------------- rotas HTTP

def _json(handler, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    handler.send_response(code)
    handler._cors()
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(b)))
    handler.end_headers()
    try:
        handler.wfile.write(b)
    except (BrokenPipeError, ConnectionResetError):
        pass


def handle_get(handler):
    """Devolve True se a rota era do Exportador (e ja respondeu)."""
    parsed = urllib.parse.urlparse(handler.path)
    if not parsed.path.startswith('/export'):
        return False
    path = parsed.path[len('/export'):] or '/'
    qs = urllib.parse.parse_qs(parsed.query)
    job = JOBS.get((qs.get('id') or [''])[0])

    if path in ('/', '/ping'):
        _json(handler, 200, {'ok': True, 'exportador': True, 'cloud': True,
                             'version': MOTOR_VERSION, 'max_mb': MAX_MB})
        return True

    if path == '/status':
        if not job:
            _json(handler, 404, {'erro': 'job nao encontrado'})
        else:
            _json(handler, 200, status_job(job))
        return True

    if path == '/cancelar':
        if job:
            job['cancel'] = True
        _json(handler, 200, {'ok': True})
        return True

    if path == '/download':
        if not job or not job.get('zip') or not os.path.isfile(job['zip']):
            _json(handler, 404, {'erro': 'Zip ainda nao pronto'})
            return True
        zp = job['zip']
        handler.send_response(200)
        handler._cors()
        handler.send_header('Content-Type', 'application/zip')
        handler.send_header('Content-Disposition',
                            'attachment; filename="%s"' % os.path.basename(zp))
        handler.send_header('Content-Length', str(os.path.getsize(zp)))
        handler.end_headers()
        try:
            with open(zp, 'rb') as f:
                while True:
                    pedaco = f.read(1 << 18)
                    if not pedaco:
                        break
                    handler.wfile.write(pedaco)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    _json(handler, 404, {'erro': 'nao encontrado'})
    return True


def handle_post(handler):
    parsed = urllib.parse.urlparse(handler.path)
    if not parsed.path.startswith('/export'):
        return False
    path = parsed.path[len('/export'):] or '/'
    if path != '/start':
        _json(handler, 404, {'erro': 'nao encontrado'})
        return True
    try:
        tam = int(handler.headers.get('Content-Length', 0) or 0)
        corpo = handler.rfile.read(tam).decode('utf-8', 'ignore') if tam else ''
        dados = json.loads(corpo) if corpo else {}
    except Exception:
        dados = {}
    resposta, code = cria_job(dados.get('url', ''), handler)
    _json(handler, code, resposta)
    return True


def start_background():
    threading.Thread(target=faxina_loop, daemon=True).start()
