FROM python:3.11-slim

ARG LOCAL_WHISPER=0
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Moscow

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-color-emoji tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt requirements-local-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$LOCAL_WHISPER" = "1" ]; then pip install --no-cache-dir -r requirements-local-whisper.txt; fi \
 && playwright install --with-deps chromium

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
