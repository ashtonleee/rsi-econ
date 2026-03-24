FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi uvicorn

RUN git config --global user.email "bridge@rsi-econ" \
    && git config --global user.name "rsi-bridge"

# Cache-bust arg: pass --build-arg CACHE_BUST=$(date +%s) to ensure
# COPY picks up the latest code instead of serving stale layers.
ARG CACHE_BUST=1

COPY trusted/bridge/wallet_api.py /app/wallet_api.py
COPY trusted/bridge/notifier.py /app/notifier.py

EXPOSE 8081

CMD ["uvicorn", "wallet_api:app", "--host", "0.0.0.0", "--port", "8081"]
