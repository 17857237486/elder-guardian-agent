FROM node:24-alpine AS build

ARG APP_DIR
ARG VITE_GUARDIAN_API_BASE=http://localhost:8000
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy

WORKDIR /app
COPY package.json pnpm-workspace.yaml ./
COPY pnpm-lock.yaml ./
COPY packages/frontend-shared ./packages/frontend-shared
COPY ${APP_DIR} ./${APP_DIR}
RUN corepack enable
RUN pnpm install --frozen-lockfile
ENV VITE_GUARDIAN_API_BASE=${VITE_GUARDIAN_API_BASE}
RUN pnpm --filter "$(basename ${APP_DIR})" build

FROM nginx:1.27-alpine
ARG APP_DIR
COPY deploy/nginx/spa.conf /etc/nginx/conf.d/default.conf
COPY deploy/nginx/99-runtime-config.sh /docker-entrypoint.d/99-runtime-config.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/99-runtime-config.sh \
    && chmod +x /docker-entrypoint.d/99-runtime-config.sh
COPY --from=build /app/${APP_DIR}/dist /usr/share/nginx/html
EXPOSE 80
