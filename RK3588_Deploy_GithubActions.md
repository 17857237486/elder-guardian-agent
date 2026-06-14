# RK3588 GitHub Actions 部署指南

本文档用于每次将 Elder Guardian Agent 部署或更新到 RK3588 开发机。

部署方式：GitHub Actions 提前构建 ARM64 Docker 镜像，RK3588 只负责拉取 `latest` 镜像并启动容器，不在开发板上编译源码。

## 固定部署信息

- GitHub 仓库：`17857237486/elder-guardian-agent`
- 部署分支：`main`
- 镜像标签：`latest`
- RK3588 地址：`192.168.10.64`
- SSH 用户：`root`
- RK3588 部署目录：`/opt/elder-guardian-agent`
- 本地项目目录：`D:\3_Works\2026.6\elderly_Project_4`
- Compose 文件：`docker-compose.images.yml`

## 每次部署流程

### 1. 提交并推送代码

在 PowerShell 中执行：

```powershell
cd D:\3_Works\2026.6\elderly_Project_4
git status
git add .
git commit -m "说明本次修改内容"
git push origin main
```

如果没有需要提交的修改，可以跳过 `git add` 和 `git commit`。

### 2. 等待 GitHub Actions 构建完成

打开：

<https://github.com/17857237486/elder-guardian-agent/actions>

进入 `Docker Images` 工作流，确认本次 `main` 提交对应的运行结果为绿色 `success`。

该工作流会构建以下 ARM64 镜像：

- `guardian-core`
- `edge-mcp-server`
- `guardian-orchestrator`
- `web-dashboard`
- `elder-hmi`
- `background-mqtt`
- `wechat-adapter`
- `vision-service`
- `voice-hmi-service`

不要在 Actions 仍处于运行中或失败时部署，否则 RK3588 可能拉取到上一版本的 `latest` 镜像。

### 3. 执行 RK3588 部署脚本

在 PowerShell 中执行：

```powershell
wsl bash -lc "cd /mnt/d/3_Works/2026.6/elderly_Project_4 && REMOTE_HOST=192.168.10.64 REMOTE_USER=root ./scripts/deploy_rk3588.sh"
```

部署脚本会自动：

1. 将 Compose、配置和部署脚本同步到 RK3588。
2. 保留远端 `.env`、SQLite 数据库和 Mosquitto 数据。
3. 使用 `docker-compose.images.yml`。
4. 拉取 `ghcr.io/17857237486/elder-guardian-agent/*:latest`。
5. 执行 `docker compose up -d --remove-orphans`。
6. 输出服务状态和访问地址。

正常部署过程中不应出现 Docker 镜像构建步骤。

## 部署后检查

### 查看容器状态

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml ps"
```

正常状态：

- Mosquitto 和所有 HTTP 服务显示 `healthy`。
- `voice-hmi-service` 显示 `Up`。
- 不应有服务显示 `Restarting`、`Exited` 或 `unhealthy`。

### 检查后端接口

```powershell
curl.exe http://192.168.10.64:8000/health
curl.exe http://192.168.10.64:8010/health
curl.exe http://192.168.10.64:8020/health
curl.exe http://192.168.10.64:8090/api/health
curl.exe http://192.168.10.64:8101/health
curl.exe http://192.168.10.64:8102/health
```

所有请求都应返回 HTTP 200。Guardian Core、Edge MCP 和 Background MQTT 的健康信息中，MQTT 应显示为已连接。

### 检查前端

- Web Dashboard：<http://192.168.10.64:5173>
- Elder HMI：<http://192.168.10.64:5174>
- Background MQTT 面板：<http://192.168.10.64:8090>

### 查看日志

查看全部服务最近日志：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml logs --tail=100"
```

持续查看指定服务：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml logs -f background-mqtt"
```

把 `background-mqtt` 替换成其他 Compose 服务名即可查看相应日志。

## 只在 RK3588 上重新启动

如果镜像和代码都没有变化，只需要重启现有容器：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml restart"
```

如果需要重新创建容器：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml up -d --force-recreate"
```

## 停止和恢复服务

停止并删除容器，但保留持久化数据：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml down"
```

重新启动：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml up -d"
```

不要使用 `down -v`，否则可能删除 Docker 卷中的持久化数据。

## 常见问题

### GHCR 拉取超时

如果出现 `TLS handshake timeout`，先重新执行部署命令。Docker 会复用已经下载的镜像层。

如果仍然无法拉取，可以在 RK3588 上通过已验证的加速入口拉取，再标记回标准镜像名：

```bash
docker pull ghcr.1ms.run/17857237486/elder-guardian-agent/guardian-core:latest
docker tag ghcr.1ms.run/17857237486/elder-guardian-agent/guardian-core:latest \
  ghcr.io/17857237486/elder-guardian-agent/guardian-core:latest
```

其他镜像使用相同方法处理，然后重新运行部署脚本。

### 服务一直 Restarting

先查看对应服务日志：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml logs --tail=100 服务名"
```

然后确认 Mosquitto 是否健康：

```powershell
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml ps mosquitto"
```

### 端口被占用

检查端口占用：

```powershell
ssh root@192.168.10.64 "ss -ltnp | grep -E ':(1883|5173|5174|8000|8010|8020|8090|8101|8102) '"
```

本项目部署后，旧版原生 Nginx、Mosquitto 和 Guardian systemd 服务应保持禁用，避免与 Docker 容器争用端口。

### 查看当前使用的镜像

```powershell
ssh root@192.168.10.64 "docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep elder-guardian-agent"
```

镜像标签应为 `latest`。

## RK3588 一次性宿主机条件

当前 `192.168.10.64` 已完成以下配置，日常部署不需要重复操作：

- `/opt/elder-guardian-agent/.env` 已配置 GHCR 镜像前缀和 `IMAGE_TAG=latest`。
- 已配置该 RK3588 内核所需的 Docker/runc cgroup 兼容运行时。
- 原生 Mosquitto、Nginx 和 Guardian systemd 服务已禁用。
- Docker Compose 已接管项目使用的全部端口。

如果开发板重装系统、重新刷机或 Docker 配置被覆盖，需要重新完成这些宿主机初始化工作。

## 最简部署清单

每次正式更新只需要按以下顺序操作：

1. `git push origin main`
2. 等待 GitHub Actions `Docker Images` 成功
3. 执行 `scripts/deploy_rk3588.sh`
4. 检查 `docker compose ps`
5. 检查 6 个健康接口和 2 个前端页面

