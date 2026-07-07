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
import shutil
import subprocess
import sys
from pathlib import Path

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
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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

    # Passada extra do exiftool para atomos teimosos SO em containers que ele sabe
    # ESCREVER (mp4/mov/m4v). Em mkv/webm/avi o exiftool nao escreve e levantaria
    # erro, jogando fora o video ja limpo pelo ffmpeg; nesses casos o ffmpeg
    # (-map_metadata -1) ja removeu os metadados.
    if dst.suffix.lower() in (".mp4", ".mov", ".m4v"):
        _checar_dependencia("exiftool")
        _run(["exiftool", "-all=", "-overwrite_original", str(dst)])


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
    args = ap.parse_args(argv)

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
