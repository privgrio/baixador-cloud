FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libimage-exiftool-perl curl \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp gallery-dl "requests[socks]" pysocks
WORKDIR /app
COPY server_cloud.py limpa_midia.py /app/
CMD ["python", "server_cloud.py"]
