FROM python:3.11-slim

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY packages/guardian-shared /app/packages/guardian-shared
COPY apps/guardian-orchestrator /app/apps/guardian-orchestrator
RUN pip install --no-cache-dir -e /app/packages/guardian-shared -e /app/apps/guardian-orchestrator
WORKDIR /app/apps/guardian-orchestrator
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]
