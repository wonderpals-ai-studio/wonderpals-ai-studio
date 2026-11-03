FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libespeak-ng1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Piper voice models and their matching JSON configuration files.
RUN mkdir -p /voices && \
    python -m piper.download_voices --data-dir /voices \
      en_US-lessac-medium en_US-amy-medium en_US-ryan-high

COPY app.py .
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
