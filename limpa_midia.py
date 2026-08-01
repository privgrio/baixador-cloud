#!/usr/bin/env python3
"""
limpa_midia.py — Limpeza de metadados para IMAGENS e VÍDEOS.

Mesmo tratamento para os dois tipos: remove metadados (EXIF / IPTC / XMP /
GPS / título / comentário / etc.) preservando a qualidade ao máximo.

Por padrão NÃO recomprime (lossless):
  - JPEG ......... exiftool -all=                  (não reencoda; pixels intactos)
  - PNG .......... exiftool -all=                  (PNG é lossless por natureza)
  - MP4/MOV/... .. ffmpeg -map_metadata -1 -c copy (não reencoda o vídeo)
                   + exiftool por cima p/ átomos teimosos

Recompressão é OPCIONAL (recompress=True), sempre em UMA passagem e em
faixa segura: JPEG quality=95 / subsampling 4:4:4 ; vídeo CRF 18 (H.264).
PNG continua lossless mesmo recomprimido (só reotimiza tamanho).

O hash criptográfico (SHA-256) muda sozinho porque os bytes mudam — não há
etapa separada para isso. Esta ferramenta NÃO toca em hash perceptual; ela
apenas remove metadados e (se pedido) recomprime.

Dependências externas:
  exiftool : apt install libimage-exiftool-perl   |  brew install exiftool
  ffmpeg   : apt install ffmpeg                    |  brew install ffmpeg
  Pillow   : pip install Pillow   (só necessário para recompress=True)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

# Windows: esconder a janela preta de console do exiftool/ffmpeg.
if os.name == 'nt':
    _NO_WINDOW = 0x08000000
    _SI = subprocess.STARTUPINFO()
    _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _SI.wShowWindow = 0  # SW_HIDE
else:
    _NO_WINDOW = 0
    _SI = None

# ---------------------------------------------------------------------------
# Tipos suportados
# ---------------------------------------------------------------------------
EXT_IMAGEM = {".jpg", ".jpeg", ".png"}
EXT_VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def _checar_dependencia(nome: str) -> None:
    if shutil.which(nome) is None:
        raise RuntimeError(
            f"'{nome}' não encontrado no PATH. Instale-o antes de usar.\n"
            f"  exiftool: apt install libimage-exiftool-perl | brew install exiftool\n"
            f"  ffmpeg:   apt install ffmpeg | brew install ffmpeg"
        )


def _run(cmd: list[str], timeout: int = 600) -> None:
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = _NO_WINDOW
        kw['startupinfo'] = _SI
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Tempo esgotado ({timeout}s): {' '.join(cmd)}")
    if r.returncode != 0:
        raise RuntimeError(f"Falhou: {' '.join(cmd)}\n{r.stderr.strip()}")


def sha256(caminho: str | Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def detectar_tipo(caminho: str | Path) -> str:
    """Retorna 'imagem', 'video' ou levanta erro. Usa extensão + magic bytes."""
    p = Path(caminho)
    ext = p.suffix.lower()
    if ext in EXT_IMAGEM:
        return "imagem"
    if ext in EXT_VIDEO:
        return "video"

    # fallback por assinatura de bytes
    with open(p, "rb") as f:
        head = f.read(16)
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return "imagem"
    if head[4:8] == b"ftyp":  # MP4 / MOV
        return "video"
    raise ValueError(f"Tipo de arquivo não suportado: {p.name}")


# ---------------------------------------------------------------------------
# IMAGEM
# ---------------------------------------------------------------------------
def limpar_imagem(
    src: str | Path,
    dst: str | Path,
    *,
    recompress: bool = False,
    quality: int = 95,
    remover_icc: bool = False,
) -> None:
    """
    recompress=False (padrão): lossless. Copia o arquivo e remove metadados
        com exiftool, sem reencodar. Pixels ficam idênticos ao original.
    recompress=True: reencoda (1 passagem). JPEG em quality/subsampling 4:4:4;
        PNG continua lossless (só reotimiza). Use só se precisar mesmo.

    remover_icc=False (padrão): preserva o perfil de cor (evita deslocar cor
        em imagens de gamut largo). True remove o ICC também.
    """
    _checar_dependencia("exiftool")
    src, dst = Path(src), Path(dst)

    if not recompress:
        # ---- caminho lossless: copia bytes e tira só os metadados ----
        shutil.copy2(src, dst)
        if remover_icc:
            _run(["exiftool", "-all=", "-overwrite_original", str(dst)])
        else:
            # remove tudo, mas devolve o ICC do próprio arquivo
            _run(["exiftool", "-all=", "-tagsfromfile", "@",
                  "-icc_profile", "-overwrite_original", str(dst)])
        return

    # ---- caminho com recompressão ----
    from PIL import Image  # importado só aqui (dependência opcional)

    with Image.open(src) as img:
        icc = None if remover_icc else img.info.get("icc_profile")
        fmt = (img.format or "").upper()
        params = {}
        if icc:
            params["icc_profile"] = icc

        if fmt in ("JPEG", "MPO") or dst.suffix.lower() in (".jpg", ".jpeg"):
            img.convert("RGB").save(
                dst, "JPEG", quality=quality, subsampling=0, **params  # 4:4:4
            )
        else:  # PNG e afins — lossless; quality não se aplica
            img.save(dst, "PNG", optimize=True, **params)

    # garante que nenhum metadado sobreviveu ao re-encode
    if remover_icc:
        _run(["exiftool", "-all=", "-overwrite_original", str(dst)])
    else:
        _run(["exiftool", "-all=", "-tagsfromfile", "@",
              "-icc_profile", "-overwrite_original", str(dst)])


# ---------------------------------------------------------------------------
# VÍDEO
# ---------------------------------------------------------------------------
def limpar_video(
    src: str | Path,
    dst: str | Path,
    *,
    recompress: bool = False,
    crf: int = 18,
    preset: str = "slow",
    vcodec: str = "libx264",
) -> None:
    """
    recompress=False (padrão): lossless. ffmpeg -map_metadata -1 -c copy
        (não reencoda nenhum fluxo) + exiftool para limpar átomos restantes.
    recompress=True: reencoda o vídeo (1 passagem) em CRF (menor = melhor;
        18 ~ visualmente transparente em H.264). Áudio é copiado, não tocado.
        Atenção: reencodar repetidamente acumula perda — evite ciclos.
    """
    _checar_dependencia("ffmpeg")
    src, dst = Path(src), Path(dst)

    if not recompress:
        _run(["ffmpeg", "-y", "-i", str(src),
              "-map_metadata", "-1", "-c", "copy", str(dst)])
    else:
        _run(["ffmpeg", "-y", "-i", str(src),
              "-map_metadata", "-1",
              "-c:v", vcodec, "-crf", str(crf), "-preset", preset,
              "-c:a", "copy", str(dst)])

    # Passada extra do exiftool para atomos teimosos SO em containers que o exiftool
    # sabe ESCREVER (mp4/mov/m4v). Para mkv/webm/avi o exiftool nao escreve e
    # levantaria erro, jogando fora o video ja limpo pelo ffmpeg (-map_metadata -1),
    # entao nesses casos confiamos so no ffmpeg, que ja removeu os metadados.
    if dst.suffix.lower() in (".mp4", ".mov", ".m4v"):
        _checar_dependencia("exiftool")
        _run(["exiftool", "-all=", "-overwrite_original", str(dst)])


# ---------------------------------------------------------------------------
# UNIFICAR (deixar o arquivo "único")
# ---------------------------------------------------------------------------
# O objetivo aqui é DIFERENTE do de limpar metadados. Limpar tira os dados de
# dentro sem tocar na imagem. Unificar reescreve o arquivo inteiro: recomprime,
# muda a velocidade em fração de por cento, corta uma casquinha das bordas e
# devolve ao tamanho original, mexe no brilho/saturação em grau imperceptível e
# grava com parâmetros de codificação sorteados. Cada passagem gera um arquivo
# com bytes, duração, quantidade de quadros e assinatura de compressão novos.
#
# O QUE ISSO RESOLVE: comparação de ARQUIVO (hash, impressão digital, "esse
# vídeo é cópia daquele", detecção de duplicata/reenvio).
# O QUE ISSO NÃO RESOLVE: revisão VISUAL (o robô que olha os pixels, lê o texto
# na tela e reconhece o produto continua vendo exatamente a mesma coisa).
#
# Níveis: 'leve' (nada visível), 'medio' (padrão), 'forte' (mais agressivo).
# Cada chamada com semente diferente produz um resultado diferente do anterior,
# então dá para gerar N versões distintas do mesmo vídeo.

NIVEIS = ("leve", "medio", "forte")


def _run_out(cmd: list[str], timeout: int = 120) -> str:
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = _NO_WINDOW
        kw['startupinfo'] = _SI
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    return r.stdout or ""


def sondar(src: str | Path) -> dict:
    """Lê largura/altura/fps/áudio do arquivo com ffprobe."""
    _checar_dependencia("ffprobe")
    out = _run_out(["ffprobe", "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-show_format", str(src)])
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    info = {"largura": 0, "altura": 0, "fps": 30.0, "tem_audio": False,
            "taxa_audio": 44100, "duracao": 0.0, "quadros": 0, "bitrate": 0}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["largura"]:
            info["largura"] = int(s.get("width") or 0)
            info["altura"] = int(s.get("height") or 0)
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1"
            try:
                num, den = rate.split("/")
                if float(den) > 0:
                    info["fps"] = float(num) / float(den)
            except Exception:
                pass
            try:
                info["quadros"] = int(s.get("nb_frames") or 0)
            except Exception:
                pass
        elif s.get("codec_type") == "audio" and not info["tem_audio"]:
            info["tem_audio"] = True
            try:
                info["taxa_audio"] = int(s.get("sample_rate") or 44100)
            except Exception:
                pass
    fmt = data.get("format") or {}
    try:
        info["duracao"] = float(fmt.get("duration") or 0)
    except Exception:
        pass
    try:
        info["bitrate"] = int(fmt.get("bit_rate") or 0)
    except Exception:
        pass
    if not info["bitrate"] and info["duracao"] > 0:
        try:
            info["bitrate"] = int(Path(src).stat().st_size * 8 / info["duracao"])
        except Exception:
            pass
    if not info["fps"] or info["fps"] <= 0 or info["fps"] > 240:
        info["fps"] = 30.0
    return info


def _receita(nivel: str, rnd: random.Random) -> dict:
    """Sorteia os números de UMA versão. Semente diferente = versão diferente.

    Os níveis foram calibrados MEDINDO a distância de hash perceptual (pHash,
    o método que os comparadores de imagem usam) entre o original e a saída,
    num criativo real 1080x1920. O resultado da medição:

        recomprimir, mexer em cor, cortar até 8% ....... distância 0 a 2  (idêntico p/ o robô)
        cortar 15% .................................... distância 10
        cortar 25% .................................... distância 22
        zoom de 12% deslocado para um canto ........... distância 26
        espelhar na horizontal ........................ distância 32  (o mais forte)

    Ou seja: 'leve' e 'medio' criam um ARQUIVO novo (vencem hash e detecção de
    duplicata) mas continuam sendo a MESMA IMAGEM para um comparador visual.
    Só o 'forte' (zoom deslocado) e o espelhamento realmente mudam o que o
    comparador de imagem enxerga.
    """
    nivel = nivel if nivel in NIVEIS else "medio"
    if nivel == "leve":
        r = dict(
            velocidade=rnd.uniform(0.994, 1.006),   # ±0,6% — ninguém percebe
            corte=0.0,                              # sem corte de borda
            desloca=0.0,
            brilho=rnd.uniform(-0.008, 0.008),
            contraste=rnd.uniform(0.995, 1.005),
            saturacao=rnd.uniform(0.99, 1.01),
            matiz=rnd.uniform(-0.4, 0.4),
            ruido=0,
            corta_inicio=0.0,
            fps_fator=rnd.uniform(0.997, 1.003),
        )
    elif nivel == "forte":
        # Corte grande e DESLOCADO para um canto: é o que de fato tira a peça do
        # radar de comparação de imagem. Dá para perceber como um enquadramento
        # diferente, então o texto que estiver colado na borda pode sair.
        r = dict(
            velocidade=rnd.uniform(0.96, 1.04),
            corte=rnd.uniform(0.12, 0.18),          # 12% a 18% das bordas
            desloca=rnd.uniform(0.45, 0.9) * rnd.choice([-1, 1]),
            brilho=rnd.uniform(-0.025, 0.025),
            contraste=rnd.uniform(0.97, 1.03),
            saturacao=rnd.uniform(0.95, 1.06),
            matiz=rnd.uniform(-2.5, 2.5),
            ruido=rnd.randint(2, 5),
            corta_inicio=rnd.uniform(0.06, 0.16),
            fps_fator=rnd.uniform(0.98, 1.02),
        )
    else:  # medio
        r = dict(
            velocidade=rnd.uniform(0.98, 1.02),
            corte=rnd.uniform(0.02, 0.045),         # 2% a 4,5%
            desloca=rnd.uniform(-0.5, 0.5),
            brilho=rnd.uniform(-0.015, 0.015),
            contraste=rnd.uniform(0.985, 1.015),
            saturacao=rnd.uniform(0.97, 1.03),
            matiz=rnd.uniform(-1.2, 1.2),
            ruido=rnd.randint(1, 3),
            corta_inicio=rnd.uniform(0.0, 0.09),
            fps_fator=rnd.uniform(0.99, 1.01),
        )
    r["crf"] = rnd.randint(20, 24)
    r["gop"] = rnd.randint(48, 250)
    r["audio_kbps"] = rnd.choice([128, 144, 160, 176, 192])
    r["nivel"] = nivel
    return r


def unificar_video(
    src: str | Path,
    dst: str | Path,
    *,
    nivel: str = "medio",
    semente: int | None = None,
    preset: str = "veryfast",
    tirar_repetidos: bool = False,
    espelhar: bool = False,
    threads: int = 0,
    max_pixels: int = 0,
) -> dict:
    """
    Reescreve o vídeo inteiro para que ele seja um arquivo novo.

    threads: 0 = livre (máquina do usuário). Um número pequeno segura o consumo de
        MEMÓRIA, não só de processador: o x264 guarda uma cópia do quadro por
        thread e, num container de 512 MB que enxerga os 16 núcleos do servidor
        físico, o padrão do ffmpeg abre ~24 threads e estoura a memória (foi
        exatamente isso que derrubou o motor da nuvem na primeira publicação).
        Com threads pequeno também ligamos fatiamento por faixa (sliced-threads),
        que divide o MESMO quadro entre as threads em vez de manter vários.
    max_pixels: 0 = mantém o tamanho original. Se vier acima disso, reduz na
        proporção (protege a memória em máquina pequena com vídeo 4K).

    tirar_repetidos: joga fora quadros idênticos ao anterior (mpdecimate). Bom em
        gravação de tela / animação; em vídeo filmado pode deixar o movimento
        engasgado. Quando o vídeo TEM áudio, o descarte roda sem reescrever os
        tempos (senão a imagem adianta e sai do sincronismo com a voz).
    espelhar: inverte na horizontal. Muito eficaz, mas ESPELHA TEXTO e logo —
        desligado por padrão, serve só para vídeo sem escrita na tela.
    """
    _checar_dependencia("ffmpeg")
    src, dst = Path(src), Path(dst)
    if semente is None:
        semente = int.from_bytes(os.urandom(4), "big")
    rnd = random.Random(semente)
    r = _receita(nivel, rnd)
    info = sondar(src)

    L, A = info["largura"], info["altura"]
    if L <= 0 or A <= 0:
        raise RuntimeError("Não consegui ler a imagem deste vídeo (arquivo corrompido?).")
    reduzido = False
    if max_pixels and L * A > max_pixels:
        fator = (max_pixels / float(L * A)) ** 0.5
        L, A = int(L * fator), int(A * fator)
        reduzido = True
    L -= L % 2
    A -= A % 2

    v = []
    # 1) quadros repetidos fora (opcional)
    if tirar_repetidos:
        if info["tem_audio"]:
            v.append("mpdecimate")                       # sem reescrever tempos: áudio continua no lugar
        else:
            v.append("mpdecimate,setpts=N/FRAME_RATE/TB")
    # 2) corta uma faixa das bordas, deslocada para um lado, e devolve ao tamanho
    #    original. É a única etapa que muda de verdade o que um comparador de
    #    IMAGEM enxerga (as outras só trocam os bytes do arquivo).
    if r["corte"] > 0.001:
        manter = 1 - r["corte"]
        dx, dy = r["desloca"], r["desloca"] * 0.6
        v.append(
            f"crop=trunc(iw*{manter:.4f}/2)*2:trunc(ih*{manter:.4f}/2)*2:"
            f"(iw-ow)/2*(1+{dx:.3f}):(ih-oh)/2*(1+{dy:.3f})"
        )
        v.append(f"scale={L}:{A}:flags=lanczos")
    else:
        v.append(f"scale={L}:{A}")
    if espelhar:
        v.append("hflip")
    # 3) cor em grau imperceptível (muda o histograma inteiro)
    v.append(f"eq=brightness={r['brilho']:.4f}:contrast={r['contraste']:.4f}:"
             f"saturation={r['saturacao']:.4f}")
    if abs(r["matiz"]) > 0.05:
        v.append(f"hue=h={r['matiz']:.3f}")
    # 4) grão levíssimo (quebra comparação pixel a pixel)
    if r["ruido"] > 0:
        v.append(f"noise=alls={r['ruido']}:allf=t+u")
    # 5) velocidade (muda duração e o tempo de cada quadro)
    vel = r["velocidade"]
    if abs(vel - 1.0) > 0.0005:
        v.append(f"setpts=PTS/{vel:.6f}")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-map", "0:v:0"]
    if info["tem_audio"]:
        cmd += ["-map", "0:a:0"]
    if r["corta_inicio"] > 0.005:
        # Come alguns quadros do começo. Vai DEPOIS do -i de propósito: no lado da
        # saída o corte é exato nos dois fluxos (do lado da entrada ele pula para o
        # keyframe mais próximo e a voz sairia até 33 ms fora do lábio).
        cmd += ["-ss", f"{r['corta_inicio']:.3f}"]
    cmd += ["-vf", ",".join(v)]
    if info["tem_audio"] and abs(vel - 1.0) > 0.0005:
        cmd += ["-af", f"atempo={vel:.6f}"]
    cmd += ["-c:v", "libx264", "-crf", str(r["crf"]), "-preset", preset,
            "-pix_fmt", "yuv420p", "-g", str(r["gop"])]
    if threads and threads > 0:
        cmd += ["-threads", str(threads), "-x264-params", "sliced-threads=1"]
    # TETO DE TAMANHO. Recomprimir para H.264 um vídeo que veio em HEVC (celular,
    # TikTok, Instagram) infla o arquivo várias vezes, porque o H.264 precisa de
    # mais bits para a mesma imagem. Sem teto, um vídeo grande vira um monstro na
    # hora de subir. O teto é 3x a taxa do original (nunca abaixo de 3 Mbps, nunca
    # acima de 16 Mbps): o CRF continua mandando na qualidade e o teto só corta o
    # exagero nas cenas de muito movimento.
    if info["bitrate"] > 0:
        teto = int(min(max(info["bitrate"] * 3, 3_000_000), 16_000_000))
        cmd += ["-maxrate", str(teto), "-bufsize", str(teto * 2)]
    # Com mpdecimate ligado NÃO forçamos taxa fixa: forçar reporia os quadros
    # jogados fora (e, com áudio, seria justamente o que desalinha).
    if not tirar_repetidos:
        cmd += ["-r", f"{info['fps'] * r['fps_fator']:.4f}"]
    if info["tem_audio"]:
        cmd += ["-c:a", "aac", "-b:a", f"{r['audio_kbps']}k"]
    else:
        cmd += ["-an"]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", str(dst)]
    _run(cmd, timeout=1800)

    if dst.suffix.lower() in (".mp4", ".mov", ".m4v") and shutil.which("exiftool"):
        try:
            _run(["exiftool", "-all=", "-overwrite_original", str(dst)])
        except Exception:
            pass   # o ffmpeg já tirou os metadados; exiftool aqui é só reforço

    depois = sondar(dst)
    return {
        "tipo": "video",
        "nivel": r["nivel"],
        "semente": semente,
        "velocidade": round(vel, 4),
        "corte_bordas_pct": round(r["corte"] * 100, 2),
        "quadros_repetidos_removidos": bool(tirar_repetidos),
        "espelhado": bool(espelhar),
        "reduzido": reduzido,
        "largura_saida": L,
        "altura_saida": A,
        "duracao_antes": round(info["duracao"], 3),
        "duracao_depois": round(depois["duracao"], 3),
        "fps_antes": round(info["fps"], 3),
        "fps_depois": round(depois["fps"], 3),
    }


def unificar_imagem(
    src: str | Path,
    dst: str | Path,
    *,
    nivel: str = "medio",
    semente: int | None = None,
    espelhar: bool = False,
) -> dict:
    """Mesma ideia da versão de vídeo, para JPEG/PNG: recomprime com corte de
    borda, ajuste de cor imperceptível e metadados zerados."""
    _checar_dependencia("ffmpeg")
    src, dst = Path(src), Path(dst)
    if semente is None:
        semente = int.from_bytes(os.urandom(4), "big")
    rnd = random.Random(semente)
    r = _receita(nivel, rnd)
    info = sondar(src)
    L, A = info["largura"], info["altura"]
    if L <= 0 or A <= 0:
        raise RuntimeError("Não consegui ler as dimensões desta imagem.")

    v = []
    if r["corte"] > 0.001:
        manter = 1 - r["corte"]
        dx, dy = r["desloca"], r["desloca"] * 0.6
        v.append(f"crop=trunc(iw*{manter:.4f}):trunc(ih*{manter:.4f}):"
                 f"(iw-ow)/2*(1+{dx:.3f}):(ih-oh)/2*(1+{dy:.3f})")
        v.append(f"scale={L}:{A}:flags=lanczos")
    if espelhar:
        v.append("hflip")
    v.append(f"eq=brightness={r['brilho']:.4f}:contrast={r['contraste']:.4f}:"
             f"saturation={r['saturacao']:.4f}")
    if abs(r["matiz"]) > 0.05:
        v.append(f"hue=h={r['matiz']:.3f}")
    if r["ruido"] > 0:
        v.append(f"noise=alls={max(1, r['ruido'] - 1)}:allf=t+u")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if v:
        cmd += ["-vf", ",".join(v)]
    if dst.suffix.lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", str(rnd.randint(2, 4))]
    else:
        cmd += ["-compression_level", str(rnd.randint(6, 9))]
    cmd += ["-map_metadata", "-1", str(dst)]
    _run(cmd, timeout=300)

    if shutil.which("exiftool"):
        try:
            _run(["exiftool", "-all=", "-overwrite_original", str(dst)])
        except Exception:
            pass
    return {
        "tipo": "imagem",
        "nivel": r["nivel"],
        "semente": semente,
        "corte_bordas_pct": round(r["corte"] * 100, 2),
        "espelhado": bool(espelhar),
    }


def unificar(src: str | Path, dst: str | Path | None = None, **kw) -> dict:
    """Detecta imagem/vídeo, unifica e devolve um relatório com os hashes."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    tipo = detectar_tipo(src)
    if dst is None:
        ext = src.suffix if src.suffix.lower() in (".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png") \
              else (".mp4" if tipo == "video" else ".jpg")
        dst = src.with_name(f"{src.stem}_v1{ext}")
    dst = Path(dst)
    sha_antes = sha256(src)
    if tipo == "imagem":
        rel = unificar_imagem(src, dst, nivel=kw.get("nivel", "medio"),
                              semente=kw.get("semente"), espelhar=kw.get("espelhar", False))
    else:
        rel = unificar_video(src, dst, nivel=kw.get("nivel", "medio"),
                             semente=kw.get("semente"), preset=kw.get("preset", "veryfast"),
                             tirar_repetidos=kw.get("tirar_repetidos", False),
                             espelhar=kw.get("espelhar", False),
                             threads=kw.get("threads", 0),
                             max_pixels=kw.get("max_pixels", 0))
    rel.update({
        "entrada": str(src),
        "saida": str(dst),
        "sha256_antes": sha_antes,
        "sha256_depois": sha256(dst),
        "bytes_antes": src.stat().st_size,
        "bytes_depois": dst.stat().st_size,
    })
    return rel


# ---------------------------------------------------------------------------
# Despachante (detecta o tipo e chama a função certa)
# ---------------------------------------------------------------------------
def limpar(src: str | Path, dst: str | Path | None = None, **kw) -> dict:
    """
    Detecta se é imagem ou vídeo e limpa. Se dst for None, gera '<nome>_limpo.ext'.
    Retorna um relatório com os hashes antes/depois e o tipo processado.
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    if dst is None:
        dst = src.with_name(f"{src.stem}_limpo{src.suffix}")
    dst = Path(dst)

    tipo = detectar_tipo(src)
    sha_antes = sha256(src)

    if tipo == "imagem":
        limpar_imagem(src, dst,
                      recompress=kw.get("recompress", False),
                      quality=kw.get("quality", 95),
                      remover_icc=kw.get("remover_icc", False))
    else:
        limpar_video(src, dst,
                     recompress=kw.get("recompress", False),
                     crf=kw.get("crf", 18),
                     preset=kw.get("preset", "slow"),
                     vcodec=kw.get("vcodec", "libx264"))

    return {
        "tipo": tipo,
        "entrada": str(src),
        "saida": str(dst),
        "sha256_antes": sha_antes,
        "sha256_depois": sha256(dst),
        "recomprimido": bool(kw.get("recompress", False)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli_unificar(args) -> int:
    entrada = Path(args.entrada)
    alvos: list[Path] = []
    if entrada.is_dir():
        for p in sorted(entrada.rglob("*")):
            if p.suffix.lower() in EXT_IMAGEM | EXT_VIDEO:
                alvos.append(p)
        if not alvos:
            print("Nenhuma imagem ou vídeo nessa pasta.", file=sys.stderr)
            return 2
    else:
        alvos.append(entrada)

    saida = Path(args.saida) if args.saida else None
    if saida and (entrada.is_dir() or args.versoes > 1 or len(alvos) > 1):
        saida.mkdir(parents=True, exist_ok=True)

    erros = 0
    for p in alvos:
        for n in range(1, max(1, args.versoes) + 1):
            try:
                ext = p.suffix if p.suffix.lower() in (".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png") \
                      else (".mp4" if detectar_tipo(p) == "video" else ".jpg")
                nome = f"{p.stem}_v{n}{ext}"
                if saida and saida.is_dir():
                    dst = saida / nome
                elif saida:
                    dst = saida
                else:
                    dst = p.with_name(nome)
                rel = unificar(p, dst, nivel=args.nivel,
                               tirar_repetidos=args.tirar_repetidos,
                               espelhar=args.espelhar)
                print(f"[ok] versão {n}: {rel['saida']}")
                print(f"     sha256 {rel['sha256_antes'][:12]}… -> {rel['sha256_depois'][:12]}…"
                      f"   ({rel['bytes_antes']//1024} KB -> {rel['bytes_depois']//1024} KB)")
                if rel["tipo"] == "video":
                    print(f"     velocidade {rel['velocidade']}x · borda -{rel['corte_bordas_pct']}% · "
                          f"{rel['duracao_antes']}s -> {rel['duracao_depois']}s")
            except Exception as e:
                erros += 1
                print(f"[erro] {p} (versão {n}): {e}", file=sys.stderr)
    return 1 if erros else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Remove metadados de imagens e vídeos (lossless por padrão)."
    )
    ap.add_argument("entrada", help="arquivo ou pasta")
    ap.add_argument("-o", "--saida", help="arquivo/pasta de saída (opcional)")
    ap.add_argument("-r", "--recursivo", action="store_true",
                    help="processa uma pasta inteira")
    ap.add_argument("--recompress", action="store_true",
                    help="recomprime em faixa segura (não é o padrão)")
    ap.add_argument("--quality", type=int, default=95,
                    help="qualidade JPEG quando --recompress (padrão 95)")
    ap.add_argument("--crf", type=int, default=18,
                    help="CRF do vídeo quando --recompress (padrão 18)")
    ap.add_argument("--remover-icc", action="store_true",
                    help="remove também o perfil de cor (imagens)")
    ap.add_argument("--unificar", action="store_true",
                    help="reescreve o arquivo inteiro para deixá-lo único (não é só limpar)")
    ap.add_argument("--nivel", choices=NIVEIS, default="medio",
                    help="força da unificação (padrão: medio)")
    ap.add_argument("--versoes", type=int, default=1,
                    help="quantas versões diferentes gerar de cada arquivo")
    ap.add_argument("--tirar-repetidos", action="store_true",
                    help="joga fora quadros idênticos (bom em gravação de tela)")
    ap.add_argument("--espelhar", action="store_true",
                    help="inverte na horizontal (CUIDADO: espelha texto e logo)")
    args = ap.parse_args(argv)

    if args.unificar:
        return _cli_unificar(args)

    entrada = Path(args.entrada)
    opts = dict(recompress=args.recompress, quality=args.quality,
                crf=args.crf, remover_icc=args.remover_icc)

    alvos: list[Path] = []
    if entrada.is_dir():
        if not args.recursivo:
            print("É uma pasta. Use -r para processar recursivamente.", file=sys.stderr)
            return 2
        for p in entrada.rglob("*"):
            if p.suffix.lower() in EXT_IMAGEM | EXT_VIDEO:
                alvos.append(p)
    else:
        alvos.append(entrada)

    saida_dir = Path(args.saida) if (args.saida and entrada.is_dir()) else None
    if saida_dir:
        saida_dir.mkdir(parents=True, exist_ok=True)

    erros = 0
    for p in alvos:
        try:
            dst = (saida_dir / f"{p.stem}_limpo{p.suffix}") if saida_dir else \
                  (Path(args.saida) if (args.saida and entrada.is_file()) else None)
            rel = limpar(p, dst, **opts)
            tag = " (recomprimido)" if rel["recomprimido"] else " (lossless)"
            print(f"[ok] {rel['tipo']}{tag}: {rel['entrada']} -> {rel['saida']}")
            print(f"     sha256 {rel['sha256_antes'][:12]}… -> {rel['sha256_depois'][:12]}…")
        except Exception as e:
            erros += 1
            print(f"[erro] {p}: {e}", file=sys.stderr)

    return 1 if erros else 0


if __name__ == "__main__":
    raise SystemExit(main())
