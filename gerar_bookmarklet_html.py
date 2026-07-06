#!/usr/bin/env python3
# Gera bookmarklet.html a partir do bookmarklet.min.js (codigo do botao magico).
# Rode sempre que mudar o bookmarklet.js/min.js.
import os

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, 'bookmarklet.min.js'), 'r', encoding='utf-8') as f:
    code = f.read().strip()

# escapa para caber numa <textarea> sem ambiguidade
esc = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

HTML = '''<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Botao Magico do Baixador</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;background:#0f0c1e;color:#f0eef8;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.6;padding:24px}
  .wrap{max-width:620px;margin:0 auto}
  .eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#a99fce}
  h1{font-size:26px;margin:6px 0 4px}
  .sub{color:#c9c2e6;margin-bottom:26px}
  .btnbox{background:linear-gradient(135deg,#241d45,#1a1530);border:1px dashed #55488f;border-radius:16px;padding:26px;text-align:center;margin:20px 0}
  .magic{display:inline-block;background:#8b5cf6;color:#fff;text-decoration:none;font-weight:700;font-size:17px;padding:14px 26px;border-radius:12px;cursor:grab;box-shadow:0 8px 24px rgba(139,92,246,.4)}
  .magic:active{cursor:grabbing}
  .hint{font-size:13px;color:#a99fce;margin-top:14px}
  ol{padding-left:20px}
  li{margin-bottom:12px}
  .card{background:#1c1830;border:1px solid #2a2547;border-radius:14px;padding:18px 20px;margin:18px 0}
  .k{background:#2a2547;border-radius:6px;padding:2px 8px;font-family:ui-monospace,monospace;font-size:13px;white-space:nowrap}
  b{color:#fff}
  .note{font-size:13px;color:#a99fce}
  .ok{color:#86efac}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">Baixador &middot; Mac e Chrome</div>
  <h1>Botao Magico</h1>
  <div class="sub">Baixa <b>todas as fotos de um produto</b> em alta, de qualquer loja (Crate &amp; Barrel, CB2, Pottery Barn e outras), inclusive as que bloqueiam robo. Como quem abre a pagina e voce, nenhum site bloqueia.</div>

  <div class="btnbox">
    <a class="magic" id="bm" href="#" draggable="true">&#129668; Baixar fotos do produto</a>
    <div class="hint">Arraste este botao roxo para a sua barra de favoritos &#8593;</div>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:10px">Como instalar (so 1 vez)</div>
    <ol>
      <li>Mostre a barra de favoritos do Chrome, se estiver escondida: <span class="k">Cmd + Shift + B</span> (mesma tecla no teclado frances).</li>
      <li><b>Arraste</b> o botao roxo &#129668; ali de cima e solte na barra de favoritos.</li>
    </ol>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:10px">Como usar</div>
    <ol>
      <li>Abra a <b>pagina do produto</b> que voce quer (ex: um espelho no Crate &amp; Barrel).</li>
      <li>Clique no botao <b>&#129668; Baixar fotos do produto</b> na barra de favoritos.</li>
      <li>Aparece um painel com as fotos encontradas. Confira, tire as que nao quiser (clicando nelas) e ajuste o nome se precisar.</li>
      <li>Clique em <b>Baixar em ZIP</b>. Pronto: baixa um ZIP com as fotos em alta e <span class="ok">metadados ja limpos</span>.</li>
    </ol>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:8px">Bom saber</div>
    <p class="note">A primeira vez do dia pode levar ate ~1 minuto (a nuvem estava dormindo). Depois fica rapido.</p>
    <p class="note">Se um site tiver protecao extra, o download pode abrir numa aba nova em vez de baixar direto. E normal, o ZIP vem do mesmo jeito.</p>
    <p class="note">Para lojas <b>Shopify</b> (como o seu McGee &amp; Co) nao precisa do botao: e so colar o link do produto ou da colecao direto no Baixador.</p>
  </div>
</div>

<textarea id="src" style="display:none">%CODE%</textarea>
<script>
(function(){
  try{
    var code = document.getElementById('src').value;
    document.getElementById('bm').href = 'javascript:' + code;
  }catch(e){}
  document.getElementById('bm').addEventListener('click', function(ev){
    ev.preventDefault();
    alert('Nao clique aqui: ARRASTE este botao para a sua barra de favoritos. Depois use ele com a pagina de um produto aberta.');
  });
})();
</script>
</body>
</html>
'''

out = HTML.replace('%CODE%', esc)
with open(os.path.join(HERE, 'bookmarklet.html'), 'w', encoding='utf-8') as f:
    f.write(out)
print('bookmarklet.html gerado,', len(out), 'bytes')
