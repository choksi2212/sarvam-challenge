# BhashaBuzzer — production image (build AFTER the feature merges)
# App: FastAPI (backend.main:app) serving the /ws game relay + static frontend/
# Deploy: Render Free (web service) or any Docker host. WS connections are long-lived — no serverless.
FROM python:3.14-slim

WORKDIR /app

# Deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (backend/ + frontend/ exist after the feature branches merge)
COPY backend/ backend/
COPY frontend/ frontend/

# Non-root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "16777216"]
