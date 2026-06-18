FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy

COPY packages/guardian-shared /app/packages/guardian-shared
COPY apps/guardian-core /app/apps/guardian-core
COPY configs /app/configs

WORKDIR /app/apps/guardian-core
RUN pip install --no-cache-dir -e ../../packages/guardian-shared -e .

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
