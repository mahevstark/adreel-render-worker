FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    fonts-noto \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ .

# Railway injects $PORT at runtime — do NOT hardcode or override it.
# Using exec form with shell to expand $PORT at runtime.
CMD ["sh", "-c", "echo 'Starting on PORT='$PORT && exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
