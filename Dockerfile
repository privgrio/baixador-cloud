FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libimage-exiftool-perl curl \
 && rm -rf /var/lib/apt/lists/*
# Instagram/TikTok quebram o yt-dlp toda hora. Para pegar SEMPRE a versao mais
# nova no build, mudar a data abaixo fura o cache de camada do Render e forca
# reinstalar o yt-dlp/gallery-dl atualizados. Bump a data quando um site parar.
ARG YTDLP_BUST=20260711
RUN pip install --no-cache-dir --upgrade yt-dlp gallery-dl
WORKDIR /app
COPY server_cloud.py limpa_midia.py bookmarklet.html /app/
CMD ["python", "server_cloud.py"]
