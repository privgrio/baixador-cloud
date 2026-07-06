/* ==========================================================================
   BOTAO MAGICO do Baixador (bookmarklet)
   Abra a pagina de um produto (Crate & Barrel, CB2, Pottery Barn, qualquer loja),
   clique neste botao. Ele acha todas as fotos do produto em alta, mostra um painel
   pra voce conferir, e baixa tudo num ZIP (com os metadados limpos).
   Fonte legivel. A versao instalavel (minificada) fica em bookmarklet.html.
   ========================================================================== */
(function () {
  var CLOUD = 'https://aclick-baixador-motor.onrender.com';
  if (document.getElementById('bxdmg-ov')) return; // ja aberto

  // escapa texto para caber com seguranca dentro de um atributo HTML
  function esc(s) { return ('' + s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }

  // ---------- nome do produto ----------
  function meta(sel) { var m = document.querySelector(sel); return m ? (m.content || '') : ''; }
  var title = meta('meta[property="og:title"]') || meta('meta[name="twitter:title"]');
  if (!title) { var h1 = document.querySelector('h1'); title = h1 ? h1.textContent : ''; }
  if (!title) title = document.title || 'produto';
  // tira so o sufixo do tipo " | Loja" / " - Loja" (exige espaco dos dois lados do separador,
  // pra nao cortar hifen interno como em "Non-Stick"). u2013/u2014 sao os tracos longos.
  title = title.replace(new RegExp('\\s+[|\\u2013\\u2014-]\\s+[^|\\u2013\\u2014-]*$'), '').trim().slice(0, 80) || 'produto';

  // ---------- coleta de fotos ----------
  var BAD = /(logo|sprite|\bicon|placeholder|swatch|payment|social|pixel|1x1|blank|spacer|badge|avatar|spinner|loading|thumbnail|-thumb|thumb-|instagram|facebook|pinterest|tiktok|youtube)/i;

  function normUrl(u) {
    if (!u) return '';
    u = ('' + u).trim();
    if (u.indexOf('//') === 0) u = 'https:' + u;
    return u;
  }
  function valid(u) {
    if (!/^https?:/i.test(u)) return false;
    if (/\.svg(\?|$)/i.test(u) || u.indexOf('data:') === 0) return false;
    if (BAD.test(u)) return false;
    return true;
  }
  function baseKey(u) {
    return u.split('?')[0].replace(/_\d{2,4}x\d{0,4}(?=\.\w+$)/, '');
  }
  // "sobe" a resolucao para os servidores de imagem conhecidos
  function upscale(u) {
    try {
      if (/[?&](sig|signature|hmac|expires|policy|token|x-amz-|_sig)=/i.test(u)) return u; // link assinado: nao mexer (senao da 403)
      if (/\/is\/image\//.test(u)) return u.split('?')[0] + '?wid=2000&qlt=90'; // Scene7 (Crate&Barrel, CB2)
      if (/cdn\.shopify|\/cdn\/shop\//.test(u)) {
        u = u.replace(/_\d{2,4}x\d{0,4}(?=\.\w+(\?|$))/, '');           // sufixo _NxM
        return u.replace(/([?&])(width|height)=\d+/g, '$1$2=2048');     // param novo ?width=
      }
      if (/[?&](wid|width|w|hei|height|h|sw|sh|size)=/i.test(u)) return u.replace(/([?&])(wid|width|w|hei|height|h|sw|sh|size)=\d+/gi, '$1$2=2000'); // preserva assinatura/tokens do link
    } catch (e) {}
    return u;
  }
  function biggestSrcset(ss) {
    // entende '800w' e '2x'. Separa candidatos por virgula SEGUIDA de espaco, pra nao
    // quebrar URLs que tem virgula no meio (ex: Cloudinary .../w_800,h_600/...).
    var best = '', bw = -1;
    ss.split(/,\s+/).forEach(function (part) {
      part = part.trim(); if (!part) return;
      var seg = part.split(/\s+/), url = seg[0], d = seg[1] || '';
      if (!url) return;
      var mw = d.match(/^([\d.]+)w$/), mx = d.match(/^([\d.]+)x$/);
      var score = mw ? parseFloat(mw[1]) : (mx ? parseFloat(mx[1]) * 1000 : 1);
      if (score > bw) { bw = score; best = url; }
    });
    return best;
  }
  // dedup + alta resolucao numa lista de URLs
  function refine(list) {
    var seen = {}, out = [];
    list.forEach(function (u) {
      u = normUrl(u);
      if (!valid(u)) return;
      var hi = upscale(u), k = baseKey(hi);
      if (!seen[k]) { seen[k] = 1; out.push(hi); }
    });
    return out;
  }
  // esta img esta dentro de uma galeria de produto? (evita nav/banner/rodape/recomendados)
  function inGallery(el) {
    for (var i = 0; i < 7 && el; i++, el = el.parentElement) {
      var c = el.className;
      c = (c && c.baseVal !== undefined) ? c.baseVal : ('' + (c || ''));
      var s = c + ' ' + (el.id || '');
      // galeria do PRODUTO (PDP). Evita "carousel/slider" genericos, que
      // costumam ser blocos de recomendacao ("voce tambem pode gostar").
      if (/(product|pdp|item)[-_]?(media|gallery|images?|photos?|main)|media[-_]?gallery|image[-_]?gallery|main[-_]?(image|photo|media)|photoswipe|fotorama/i.test(s)) return true;
    }
    return false;
  }

  // fonte 1: JSON-LD do produto (quando traz a galeria completa, e a melhor)
  var ld = [];
  function collectLD(o) {
    if (!o || typeof o !== 'object') return;
    if (o['@graph']) [].concat(o['@graph']).forEach(collectLD);
    var t = o['@type'];
    var isProd = t === 'Product' || (Array.isArray(t) && t.indexOf('Product') >= 0);
    if (isProd && o.image) {
      var im = o.image;
      if (typeof im === 'string') ld.push(im);
      else if (Array.isArray(im)) im.forEach(function (x) { ld.push(typeof x === 'string' ? x : (x && x.url)); });
      else if (im.url) ld.push(im.url);
    }
  }
  document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
    try { var j = JSON.parse(s.textContent); [].concat(j).forEach(collectLD); } catch (e) {}
  });

  // fonte 2: imagens do DOM, marcando as que estao numa galeria de produto
  var gal = [], big = [];
  function pushImg(u, img) {
    if (!u) return;
    (inGallery(img) ? gal : big).push(u);
  }
  Array.prototype.forEach.call(document.images, function (img) {
    var b = img.srcset ? biggestSrcset(img.srcset) : '';
    if (b && !/^(https?:)?\/\//i.test(b)) b = '';   // resultado estranho/relativo: usa o src resolvido
    var u = b || img.currentSrc || img.src;
    var w = img.naturalWidth || img.width || 0;
    if (u && (img.srcset || w >= 500)) pushImg(u, img);
  });
  document.querySelectorAll('picture source[srcset]').forEach(function (s) {
    var u = biggestSrcset(s.srcset); if (u) pushImg(u, s);
  });

  // separa "provaveis" (galeria + JSON-LD + og:image) das "extras" (demais grandes).
  // As provaveis ja vem marcadas; as extras aparecem desmarcadas caso falte alguma.
  var ldR = refine(ld), galR = refine(gal), bigR = refine(big);
  var og = normUrl(meta('meta[property="og:image"]'));
  var ogHi = (og && valid(og)) ? upscale(og) : '';
  var primary = refine((ogHi ? [ogHi] : []).concat(galR, ldR));
  var pk = {}; primary.forEach(function (u) { pk[baseKey(u)] = 1; });
  var extra = bigR.filter(function (u) { return !pk[baseKey(u)]; });
  var imgs = primary.concat(extra);
  if (!imgs.length) imgs = bigR;
  var nPrimary = primary.length;

  // videos (mp4 direto)
  var vids = [];
  document.querySelectorAll('video source[src], video[src]').forEach(function (v) {
    var s = v.src || v.getAttribute('src');
    if (s && /\.mp4/i.test(s)) vids.push(s.indexOf('//') === 0 ? 'https:' + s : s);
  });

  // ---------- painel na tela ----------
  var ov = document.createElement('div');
  ov.id = 'bxdmg-ov';
  ov.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(15,12,30,.72);display:flex;align-items:center;justify-content:center;padding:16px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif';
  var sel = {}; imgs.forEach(function (u, i) { sel[u] = nPrimary ? (i < nPrimary) : true; });

  function box() {
    var chosen = imgs.filter(function (u) { return sel[u]; });
    var grid = imgs.map(function (u) {
      var on = sel[u];
      return '<div data-u="' + encodeURIComponent(u) + '" style="position:relative;cursor:pointer;border-radius:8px;overflow:hidden;border:3px solid ' + (on ? '#8b5cf6' : 'transparent') + ';opacity:' + (on ? 1 : .35) + '">' +
        '<img src="' + esc(u) + '" style="width:100%;height:96px;object-fit:cover;display:block" onerror="this.parentNode.style.display=\'none\'">' +
        (on ? '' : '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:22px;color:#fff">✕</div>') +
        '</div>';
    }).join('');
    return '<div style="background:#1c1830;color:#f0eef8;border-radius:16px;max-width:640px;width:100%;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5)">' +
      '<div style="padding:18px 20px 8px">' +
      '<div style="font-size:12px;letter-spacing:.08em;color:#a99fce;text-transform:uppercase">Botao magico · Baixador</div>' +
      '<input id="bxdmg-title" value="' + esc(title) + '" style="width:100%;background:#0f0c1e;border:1px solid #35305a;color:#fff;border-radius:8px;padding:8px 10px;font-size:15px;margin-top:6px;box-sizing:border-box">' +
      '<div id="bxdmg-count" style="font-size:13px;color:#c9c2e6;margin-top:8px">' + chosen.length + ' de ' + imgs.length + ' selecionadas' + (vids.length ? (' · ' + vids.length + ' video(s)') : '') + ' · <a id="bxdmg-all" style="color:#c4b5fd;cursor:pointer;text-decoration:underline">' + (chosen.length < imgs.length ? 'marcar todas' : 'desmarcar todas') + '</a><br><span style="color:#8f88b0">Clique numa foto pra tirar/por. As prováveis já vêm marcadas.</span></div>' +
      '</div>' +
      '<div id="bxdmg-grid" style="padding:4px 20px 12px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px">' + (imgs.length ? grid : '<div style="color:#f9a8a8;padding:20px;grid-column:1/-1">Nao achei fotos de produto nesta pagina. Tente abrir a foto grande ou role a pagina antes de clicar.</div>') + '</div>' +
      '<div style="padding:12px 20px 18px;display:flex;gap:10px;border-top:1px solid #2a2547">' +
      '<button id="bxdmg-cancel" style="flex:0 0 auto;background:#2a2547;color:#cfc9e8;border:none;border-radius:10px;padding:12px 16px;font-size:14px;cursor:pointer">Cancelar</button>' +
      '<button id="bxdmg-go" style="flex:1;background:#8b5cf6;color:#fff;border:none;border-radius:10px;padding:12px 16px;font-size:15px;font-weight:600;cursor:pointer">Baixar ' + chosen.length + ' foto(s) em ZIP</button>' +
      '</div>' +
      '<div id="bxdmg-msg" style="padding:0 20px 16px;font-size:13px;color:#a99fce"></div>' +
      '</div>';
  }
  ov.innerHTML = box();
  document.body.appendChild(ov);

  function rerender() {
    var scroll = 0; var g = document.getElementById('bxdmg-grid'); if (g) scroll = g.scrollTop;
    var t = document.getElementById('bxdmg-title').value;
    ov.innerHTML = box();
    document.getElementById('bxdmg-title').value = t;
    bind();
    var g2 = document.getElementById('bxdmg-grid'); if (g2) g2.scrollTop = scroll;
  }
  function bind() {
    document.getElementById('bxdmg-cancel').onclick = function () { ov.remove(); };
    ov.onclick = function (e) { if (e.target === ov) ov.remove(); };
    document.querySelectorAll('#bxdmg-grid [data-u]').forEach(function (cell) {
      cell.onclick = function () { var u = decodeURIComponent(cell.getAttribute('data-u')); sel[u] = !sel[u]; rerender(); };
    });
    var allBtn = document.getElementById('bxdmg-all');
    if (allBtn) allBtn.onclick = function () {
      var chosen = imgs.filter(function (u) { return sel[u]; });
      var target = chosen.length < imgs.length;
      imgs.forEach(function (u) { sel[u] = target; });
      rerender();
    };
    document.getElementById('bxdmg-go').onclick = baixar;
  }
  bind();

  // ---------- baixar ----------
  function triggerDownload(blob, name) {
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); setTimeout(function () { a.remove(); }, 1500);
  }
  function baixar() {
    var chosen = imgs.filter(function (u) { return sel[u]; });
    if (!chosen.length) { alert('Selecione pelo menos uma foto.'); return; }
    var t = (document.getElementById('bxdmg-title').value || title).slice(0, 80);
    var msg = document.getElementById('bxdmg-msg');
    var go = document.getElementById('bxdmg-go');
    go.disabled = true; go.textContent = 'Preparando na nuvem...';
    msg.textContent = 'Baixando as fotos e montando o ZIP (a primeira vez pode levar ate ~1 min).';
    var body = JSON.stringify({ title: t, page_url: location.href, clean: true, images: chosen, videos: vids });

    function ok(blob) {
      triggerDownload(blob, t + '.zip');
      msg.textContent = '✅ Pronto! ZIP baixado.'; go.textContent = 'Baixado ✓';
      setTimeout(function () { ov.remove(); }, 1600);
    }
    // caminho 2: pagina bloqueou o envio (CSP) -> abre nova aba via formulario
    function formTab() {
      msg.textContent = 'Abrindo o download numa nova aba...';
      var f = document.createElement('form'); f.method = 'POST'; f.action = CLOUD + '/produto'; f.target = '_blank';
      var inp = document.createElement('input'); inp.type = 'hidden'; inp.name = 'payload'; inp.value = body;
      f.appendChild(inp); document.body.appendChild(f); f.submit();
      setTimeout(function () { f.remove(); }, 1500);
      go.textContent = 'Baixando em nova aba →'; setTimeout(function () { ov.remove(); }, 2500);
    }
    // caminho 3 (B5): servidor nao consegue baixar pela URL -> o navegador baixa e manda os bytes
    function viaBytes() {
      msg.textContent = 'O site nao deixa o servidor pegar as fotos; puxando pelo seu navegador...';
      Promise.all(chosen.map(function (u) {
        return fetch(u).then(function (r) { return r.blob(); }).then(function (b) {
          return new Promise(function (res) {
            var fr = new FileReader();
            fr.onload = function () { res({ name: (u.split('/').pop() || 'foto').split('?')[0], b64: ('' + fr.result).split(',')[1] }); };
            fr.onerror = function () { res(null); };
            fr.readAsDataURL(b);
          });
        }).catch(function () { return null; });
      })).then(function (bl) {
        bl = bl.filter(Boolean);
        if (!bl.length) { msg.textContent = 'Nao consegui baixar as fotos deste site.'; go.disabled = false; go.textContent = 'tentar de novo'; return; }
        return fetch(CLOUD + '/produto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: t, clean: true, blobs: bl, videos: vids }) })
          .then(function (r) { if (!r.ok) throw 0; return r.blob(); }).then(ok);
      }).catch(function () { msg.textContent = 'Nao consegui montar o ZIP.'; go.disabled = false; go.textContent = 'tentar de novo'; });
    }

    fetch(CLOUD + '/produto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body })
      .then(function (r) {
        if (r.status === 502) { viaBytes(); return null; }
        if (!r.ok) throw new Error('http ' + r.status);
        return r.blob();
      })
      .then(function (blob) { if (blob) ok(blob); })
      .catch(function () { formTab(); });
  }
})();
