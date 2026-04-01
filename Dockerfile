FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir celery[redis] redis anthropic requests python-dotenv

COPY orchestrator.py .
