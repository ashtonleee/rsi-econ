FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright requests httpx beautifulsoup4 lxml \
    && playwright install chromium

RUN mkdir -p /workspace/agent /opt/supervisor /var/lib/rsi/backups /opt/seed-fallback

WORKDIR /workspace/agent

# Cache-bust arg: pass --build-arg CACHE_BUST=$(date +%s) to ensure
# COPY picks up the latest seed files instead of serving stale layers.
ARG CACHE_BUST=1

# Copy seed as fallback for local dev / tests (when GIT_REMOTE_URL is not set).
# Production containers clone from GitHub at runtime via entrypoint.sh.
COPY seed/ /opt/seed-fallback/

COPY entrypoint.sh /opt/entrypoint.sh

RUN chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]
