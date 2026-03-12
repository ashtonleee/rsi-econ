FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml /app/
COPY shared /app/shared
COPY trusted /app/trusted

RUN pip install --no-cache-dir .

EXPOSE 8000
