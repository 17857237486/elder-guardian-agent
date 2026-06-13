#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/elder-guardian-agent}"
BRANCH="${BRANCH:-main}"
PUBLIC_HOST="${PUBLIC_HOST:-192.168.10.64}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.rk3588.yml}"

cd "$APP_DIR"

load_system_proxy() {
  set +u
  set -a
  if [ -f /etc/environment ]; then
    . /etc/environment 2>/dev/null || true
  fi
  for proxy_file in /etc/profile.d/*proxy*.sh /etc/profile.d/*Proxy*.sh; do
    if [ -f "$proxy_file" ]; then
      . "$proxy_file" 2>/dev/null || true
    fi
  done
  set +a

  if [ -z "${HTTP_PROXY:-}" ] && [ -n "${http_proxy:-}" ]; then export HTTP_PROXY="$http_proxy"; fi
  if [ -z "${HTTPS_PROXY:-}" ] && [ -n "${https_proxy:-}" ]; then export HTTPS_PROXY="$https_proxy"; fi

  if [ -z "${HTTP_PROXY:-}" ] && command -v gsettings >/dev/null 2>&1; then
    local desktop_user=""
    local desktop_home=""
    while IFS=: read -r user _ uid _ _ home _; do
      if [ "$uid" -ge 1000 ] && [ "$uid" -lt 60000 ] && [ -d "$home" ]; then
        desktop_user="$user"
        desktop_home="$home"
        break
      fi
    done < /etc/passwd

    if [ -n "$desktop_user" ]; then
      local desktop_uid
      desktop_uid="$(id -u "$desktop_user" 2>/dev/null || true)"
      local bus="unix:path=/run/user/${desktop_uid}/bus"
      local mode host port https_host https_port
      mode="$(sudo -u "$desktop_user" DBUS_SESSION_BUS_ADDRESS="$bus" gsettings get org.gnome.system.proxy mode 2>/dev/null | tr -d "'" || true)"
      if [ "$mode" = "manual" ]; then
        host="$(sudo -u "$desktop_user" DBUS_SESSION_BUS_ADDRESS="$bus" gsettings get org.gnome.system.proxy.http host 2>/dev/null | tr -d "'" || true)"
        port="$(sudo -u "$desktop_user" DBUS_SESSION_BUS_ADDRESS="$bus" gsettings get org.gnome.system.proxy.http port 2>/dev/null | tr -d "'" || true)"
        https_host="$(sudo -u "$desktop_user" DBUS_SESSION_BUS_ADDRESS="$bus" gsettings get org.gnome.system.proxy.https host 2>/dev/null | tr -d "'" || true)"
        https_port="$(sudo -u "$desktop_user" DBUS_SESSION_BUS_ADDRESS="$bus" gsettings get org.gnome.system.proxy.https port 2>/dev/null | tr -d "'" || true)"
        if [ -n "$host" ] && [ "${port:-0}" -gt 0 ]; then
          export HTTP_PROXY="http://${host}:${port}"
          export http_proxy="$HTTP_PROXY"
        fi
        if [ -n "$https_host" ] && [ "${https_port:-0}" -gt 0 ]; then
          export HTTPS_PROXY="http://${https_host}:${https_port}"
          export https_proxy="$HTTPS_PROXY"
        elif [ -n "${HTTP_PROXY:-}" ]; then
          export HTTPS_PROXY="$HTTP_PROXY"
          export https_proxy="$HTTP_PROXY"
        fi
      fi
      export HOME="$desktop_home"
    fi
  fi

  if [ -z "${http_proxy:-}" ] && [ -n "${HTTP_PROXY:-}" ]; then export http_proxy="$HTTP_PROXY"; fi
  if [ -z "${https_proxy:-}" ] && [ -n "${HTTPS_PROXY:-}" ]; then export https_proxy="$HTTPS_PROXY"; fi

  local default_no_proxy="localhost,127.0.0.1,::1,mosquitto,guardian-core,edge-mcp-server,guardian-orchestrator,background-mqtt,web-dashboard,elder-hmi,wechat-adapter,vision-service,voice-hmi-service,${PUBLIC_HOST}"
  if [ -z "${NO_PROXY:-}" ] && [ -z "${no_proxy:-}" ]; then
    export NO_PROXY="$default_no_proxy"
    export no_proxy="$default_no_proxy"
  elif [ -z "${NO_PROXY:-}" ]; then
    export NO_PROXY="$no_proxy"
  elif [ -z "${no_proxy:-}" ]; then
    export no_proxy="$NO_PROXY"
  fi
  set -u
}

configure_docker_daemon_proxy() {
  if [ -z "${HTTP_PROXY:-}" ] && [ -z "${HTTPS_PROXY:-}" ]; then
    echo "No system proxy detected for Docker downloads."
    return
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found; skip Docker daemon proxy setup."
    return
  fi

  local docker_proxy_dir="/etc/systemd/system/docker.service.d"
  local docker_proxy_file="${docker_proxy_dir}/http-proxy.conf"
  local tmp_file
  tmp_file="$(mktemp)"
  mkdir -p "$docker_proxy_dir"
  {
    echo "[Service]"
    printf 'Environment="HTTP_PROXY=%s"\n' "${HTTP_PROXY:-}"
    printf 'Environment="HTTPS_PROXY=%s"\n' "${HTTPS_PROXY:-${HTTP_PROXY:-}}"
    printf 'Environment="NO_PROXY=%s"\n' "${NO_PROXY:-${no_proxy:-}}"
  } > "$tmp_file"

  if [ ! -f "$docker_proxy_file" ] || ! cmp -s "$tmp_file" "$docker_proxy_file"; then
    cp "$tmp_file" "$docker_proxy_file"
    systemctl daemon-reload
    systemctl restart docker
    echo "Docker daemon proxy updated and Docker restarted."
  else
    echo "Docker daemon proxy already up to date."
  fi
  rm -f "$tmp_file"
}

ensure_env_file() {
  if [ ! -f .env ]; then
    cp .env.example .env
  fi

  if ! grep -q '^PUBLIC_GUARDIAN_API_BASE=' .env; then
    printf '\nPUBLIC_GUARDIAN_API_BASE=http://%s:8000\n' "$PUBLIC_HOST" >> .env
  fi
  if ! grep -q '^PUBLIC_EDGE_API_BASE=' .env; then
    printf 'PUBLIC_EDGE_API_BASE=http://%s:8010\n' "$PUBLIC_HOST" >> .env
  fi
  if [ -n "${IMAGE_PREFIX:-}" ] && ! grep -q '^IMAGE_PREFIX=' .env; then
    printf 'IMAGE_PREFIX=%s\n' "$IMAGE_PREFIX" >> .env
  fi
  if [ -n "${IMAGE_TAG:-}" ] && ! grep -q '^IMAGE_TAG=' .env; then
    printf 'IMAGE_TAG=%s\n' "$IMAGE_TAG" >> .env
  fi
}

update_source_if_git_repo() {
  if [ -d .git ]; then
    git fetch --all --prune
    git checkout "$BRANCH"
    git pull --ff-only
  else
    echo "No .git directory found; using the currently deployed source tree."
  fi
}

main() {
  load_system_proxy
  configure_docker_daemon_proxy
  ensure_env_file
  update_source_if_git_repo

  if grep -q '^IMAGE_PREFIX=' .env || [ -n "${IMAGE_PREFIX:-}" ]; then
    COMPOSE_FILE="${PREBUILT_COMPOSE_FILE:-docker-compose.images.yml}"
    docker compose -f "$COMPOSE_FILE" pull
    docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
    docker compose -f "$COMPOSE_FILE" ps
  else
    # Some RK3588 vendor kernels miss cgroup BPF features required by BuildKit.
    # The classic builder is slower but more compatible on Ubuntu 22 RK images.
    export DOCKER_BUILDKIT=0
    export COMPOSE_DOCKER_CLI_BUILD=0

    docker compose -f "$COMPOSE_FILE" pull --ignore-buildable || true
    docker compose -f "$COMPOSE_FILE" build --pull
    docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
    docker compose -f "$COMPOSE_FILE" ps
  fi

  echo
  echo "Elder Guardian Agent is available at:"
  echo "  Legacy Core:     http://${PUBLIC_HOST}:8000/health"
  echo "  Edge MCP/API:    http://${PUBLIC_HOST}:8010/health"
  echo "  Orchestrator:    http://${PUBLIC_HOST}:8020/health"
  echo "  MQTT Scenarios:  http://${PUBLIC_HOST}:8090"
  echo "  Vision Service:  http://${PUBLIC_HOST}:8101/health"
  echo "  WeChat Adapter:  http://${PUBLIC_HOST}:8102/health"
  echo "  Dashboard:       http://${PUBLIC_HOST}:5173"
  echo "  Elder HMI:       http://${PUBLIC_HOST}:5174"
  echo "  Mosquitto:       mqtt://${PUBLIC_HOST}:1883"
}

main "$@"
