FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn>=0.30,<1.0"

COPY tests/fixtures/webfixture/app.py /app/app.py

EXPOSE 80
