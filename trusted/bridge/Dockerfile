FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi uvicorn

COPY trusted/bridge/wallet_api.py /app/wallet_api.py

EXPOSE 8081

CMD ["uvicorn", "wallet_api:app", "--host", "0.0.0.0", "--port", "8081"]
