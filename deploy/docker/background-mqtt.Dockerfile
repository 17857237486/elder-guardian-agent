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
COPY Background_MQTT /app/Background_MQTT
RUN pip install --no-cache-dir \
    -e /app/packages/guardian-shared \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "paho-mqtt>=2.0" \
    "httpx>=0.27" \
    "pydantic>=2.7,<3"

CMD ["python", "-m", "uvicorn", "Background_MQTT.backend:app", "--host", "0.0.0.0", "--port", "8090"]
