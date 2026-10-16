# Container for Cloud Run. Build+deploy:  gcloud run deploy edge-home-assistant --source . --region europe-west4
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cloud.txt
COPY app.py ./
# Cloud Run injects $PORT (default 8080). Use the cloud backend by default in the container.
ENV LLM_BACKEND=gemini
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
