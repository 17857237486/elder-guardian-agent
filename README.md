# RK3588 居家老人健康守护与环境协同 Agent

v2 三级 AI、多关键帧接口和 RK3588 本地视觉模型配置见 [V2_Three_Level_AI_Architecture.md](V2_Three_Level_AI_Architecture.md)。

这是一个面向 RK3588 Ubuntu 22 Desktop 的 monorepo。v2 架构把系统拆成三层：`edge-mcp-server` 负责传感器/执行器/MQTT/SQLite/MCP 桥接，`guardian-orchestrator` 负责规则触发和多轮小模型 workflow，前端负责老人 HMI 和家属 dashboard 展示。默认 `LLM_MOCK=true`，无需真实模型即可跑通核心链路骨架。

后续使用 Agent 修 bug、加功能或重构前，请先阅读 `AGENTS.md`，它定义了本项目的分层职责、安全红线和统一开发风格。

## 架构

```text
传感器/视觉/设备模拟器
        |
        v
 Mosquitto MQTT Broker
        |
        v
 edge-mcp-server
  ├─ MQTT Bridge: 接入传感器、视觉、设备 ack/state
  ├─ MCP Tools: 读取上下文、请求设备动作、发起告警、记录 workflow
  ├─ Device Policy: 工具内部策略门控和审计
  └─ SQLite: raw observations、events、workflows、tool calls、executions
        |
        v
 guardian-orchestrator
  ├─ Rule Gate: P0/P1/P2/P3/P4 确定性安全分级
  ├─ Step LLM: 每个步骤 fresh conversation
  ├─ Workflow Runner: context -> fusion -> decision -> action request
  └─ Guardrails: P0/P1 不降级，燃气场景动作白名单
        |
        ├─ elder-hmi: RK3588 本地屏幕确认
        ├─ web-dashboard: 家属/开发实时看板
        └─ wechat-adapter: mock 家属协同接口
```

## 目录

```text
configs/                  风险规则、设备策略、topic、老人画像示例
data/                     SQLite、日志、视觉快照目录
scripts/                  传感器、视觉、设备模拟和开发启动脚本
packages/guardian-shared  Python 共享枚举、schema、MQTT topic
packages/frontend-shared  前端共享类型与 API 地址工具
apps/edge-mcp-server      v2 MCP/HTTP/MQTT/SQLite 桥接服务
apps/guardian-orchestrator v2 规则触发和多轮小模型编排服务
apps/guardian-core        旧 MVP FastAPI 主后端，保留作对照
apps/vision-service       mock 视觉服务
apps/voice-hmi-service    mock ASR/TTS HMI 服务
apps/wechat-adapter       mock 微信适配器
apps/web-dashboard        Vue 家属 dashboard
apps/elder-hmi            Vue 老人本地全屏 HMI
deploy/systemd            RK3588 systemd 示例
```

## 本地启动

```bash
conda env create -f environment.yml
conda activate elder-guardian-agent

docker compose up mosquitto

cd apps/guardian-core
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

前端：

```bash
pnpm install
pnpm --filter web-dashboard dev
pnpm --filter elder-hmi dev
```

打开：

- Dashboard: `http://localhost:5173`
- Elder HMI: `http://localhost:5174`
- API: `http://localhost:8000`

## MQTT Topic

```text
elder/{elder_id}/sensor/vital
elder/{elder_id}/sensor/env
elder/{elder_id}/vision/event
elder/{elder_id}/hmi/prompt
elder/{elder_id}/hmi/response
elder/{elder_id}/hmi/status
elder/{elder_id}/alert/event
elder/{elder_id}/agent/decision
elder/{elder_id}/system/status
home/{room}/{device}/set
home/{room}/{device}/state
home/{room}/{device}/ack
```

## 夜间异常活动规则（v2）

`night_abnormal_activity` 是唯一保留的夜间组合风险事件。系统按北京时间判断夜间：

```text
22:00 至次日 06:00
+ 卧室持续无人满 5 分钟
-> night_abnormal_activity（P2）
```

人体存在传感器可向 Edge API `POST /api/v2/observations` 发送状态：

```json
{
  "elder_id": "elder_001",
  "kind": "device_state",
  "source": "presence_sensor",
  "payload": {
    "room": "bedroom",
    "device": "presence_sensor",
    "present": false,
    "state": "absent"
  }
}
```

检测到老人返回卧室时发送 `present: true` 和 `state: "present"`。首次无人上报会启动独立计时器，期间重复上报不会重置计时；恢复有人会取消计时。灯光关闭、门窗开启、卫生间状态、心率数据以及直接上报 `event_type=night_abnormal_activity` 均不会单独触发此规则。

## 风险等级

- `P0`: 紧急危险，规则引擎直接告警，不等待老人确认或 LLM，例如燃气异常、血氧 `< 88`。
- `P1`: 高风险，本地询问并同步通知家属，例如疑似跌倒、血氧明显下降、夜间异常。
- `P2`: 中风险，先询问老人，超时后通知家属，例如长时间静止。
- `P3`: 低风险，自动控制设备或本地提醒，例如 CO2 偏高、温度异常。
- `P4`: 正常状态，仅记录和更新 dashboard。

关键约束：P0 不允许被 LLM 降级；P1 不允许被降到 P3/P4；燃气异常禁止风扇和空调，只允许开窗、关燃气阀、本地报警、通知家属。

## Agent 流程

```text
MQTT/API 事件
  -> 保存原始样本
  -> 规则引擎分级
  -> ContextBuilder 组装老人画像、最近数据、设备状态、历史事件
  -> LLMClient 或 mock AgentDecision
  -> OutputParser 校验 JSON
  -> Guardrails 强制安全底线
  -> ActionPlanner 生成 ask_elder / auto_control / notify_family / emergency_alert
  -> DevicePolicy 校验设备动作
  -> ActionExecutor 执行 MQTT/HMI/WebSocket/微信 mock/DB 记录
```

## 模拟验收

建议先启动设备模拟器，让设备 ack/state 能回到后端：

```bash
python scripts/simulate_device.py
```

CO2 偏高自动开窗：

```bash
python scripts/simulate_sensor.py --event co2_high
```

预期：生成 `P3` 风险事件，发布 `home/living_room/window/set` 开窗命令，dashboard 收到事件，SQLite 写入风险事件和设备动作。

长时间静止后询问老人：

```bash
python scripts/simulate_vision.py --event long_static
```

预期：生成 `P2` 风险事件，elder-hmi 收到本地询问。点击“我没事”后事件 `resolved`；点击“需要帮助”或“联系家属”后 mock 微信通知家属；超过 `HMI_RESPONSE_TIMEOUT_SEC` 无响应后升级为 `P1` 并通知家属。

燃气泄漏直接 P0 告警：

```bash
python scripts/simulate_sensor.py --event gas_leak
```

预期：直接生成 `P0`，不等待 LLM 或老人确认；发布开窗、关闭燃气阀、本地报警；不会发布风扇或空调控制。

## LLM 切换

默认 `.env.example` 中：

```bash
LLM_MOCK=true
```

此模式不会请求真实模型。要使用 OpenAI-compatible API，创建 `.env` 并设置：

```bash
LLM_MOCK=false
LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_API_KEY=your-key
LLM_MODEL=your-model
```

LLM 只输出建议动作 JSON，不允许直接发 MQTT 控制指令。所有设备控制必须经过 Action Planner 和 Device Policy。

## RK3588 部署建议

- `mosquitto` 和 `guardian-core` 可以用 Docker Compose 跑在 RK3588 上。
- `vision-service`、`voice-hmi-service` 建议用 systemd，便于后续接真实摄像头、麦克风、扬声器。
- `elder-hmi` 可用 Chromium kiosk 全屏打开 `http://127.0.0.1:5174`。
- systemd 示例在 `deploy/systemd/`，部署时把仓库放到 `/opt/elder-guardian-agent` 或按实际路径修改 `WorkingDirectory` 与 conda 路径。

纯容器部署到 RK3588 开发机：

```bash
REMOTE_HOST=192.168.10.64 REMOTE_USER=root ./scripts/deploy_rk3588.sh
```

部署完成后，开发机上会保留一键更新脚本：

```bash
cd /opt/elder-guardian-agent
./update_latest.sh
```

`update_latest.sh` 会优先使用系统代理环境变量，并把代理传给 `docker compose pull/build`。如果目录是 Git 仓库，它会先 `git pull --ff-only`，否则直接基于当前上传的源码重建并重启容器。

更推荐的生产方式是让 GitHub Actions 构建 ARM64 镜像，RK3588 只拉镜像：

1. 把仓库推到 GitHub。
2. 在 GitHub Actions 中运行 `Docker Images` 工作流。
3. 如果 GHCR package 是私有的，在 RK3588 上先执行 `docker login ghcr.io`。
4. 在 `/opt/elder-guardian-agent/.env` 写入镜像前缀，例如：

```bash
IMAGE_PREFIX=ghcr.io/your-org/elder-guardian-agent
IMAGE_TAG=latest
PUBLIC_GUARDIAN_API_BASE=http://192.168.10.64:8000
PUBLIC_EDGE_API_BASE=http://192.168.10.64:8010
```

之后在 RK3588 上一键更新：

```bash
cd /opt/elder-guardian-agent
./update_latest.sh
```

当 `.env` 中存在 `IMAGE_PREFIX` 时，脚本会自动使用 `docker-compose.images.yml`，只执行 `docker compose pull && docker compose up -d`，不在 RK3588 本机编译镜像。

## 完整 Docker Compose 服务栈

三套 Compose 文件都包含同一组 10 个服务，默认无需 profile 即可全部启动：

| 服务 | 端口 | 用途 |
| --- | ---: | --- |
| Mosquitto | `1883` | MQTT broker |
| guardian-core | `8000` | 旧 MVP API，供微信适配器和场景面板使用 |
| edge-mcp-server | `8010` | v2 Edge MCP/API，供两个前端使用 |
| guardian-orchestrator | `8020` | v2 规则与 LLM workflow 编排 |
| Background MQTT | `8090` | MQTT 数据记录、场景生成和设备面板 |
| vision-service | `8101` | mock 视觉事件 API |
| wechat-adapter | `8102` | mock 微信适配 API |
| web-dashboard | `5173` | 家属/开发者 dashboard |
| elder-hmi | `5174` | 老人本地 HMI |
| voice-hmi-service | 无 | MQTT ASR/TTS 监听服务 |

本地完整启动：

```bash
docker compose up -d --build
docker compose ps
```

RK3588 从源码构建：

```bash
cp .env.example .env
# 将 PUBLIC_EDGE_API_BASE 改为浏览器可访问的 RK3588 地址，例如 http://192.168.10.64:8010
docker compose -f docker-compose.rk3588.yml up -d --build
```

使用 GHCR 预构建镜像：

```bash
docker compose -f docker-compose.images.yml pull
docker compose -f docker-compose.images.yml up -d
```

HTTP 服务和 Mosquitto 都配置了健康检查。`guardian-orchestrator`、前端、微信适配器及场景面板会等待其上游健康后再启动。共享 SQLite 文件位于 `data/guardian.db`，v1 与 v2 使用不同表名。

如同一台机器上已有其他实例，可在 `.env` 中设置 `MQTT_PUBLIC_PORT`、`CORE_PUBLIC_PORT`、`EDGE_PUBLIC_PORT`、`ORCHESTRATOR_PUBLIC_PORT`、`BACKGROUND_PUBLIC_PORT`、`VISION_PUBLIC_PORT`、`WECHAT_PUBLIC_PORT`、`DASHBOARD_PUBLIC_PORT` 和 `HMI_PUBLIC_PORT` 覆盖宿主机端口；容器间通信端口不受影响。

## 后续扩展

- 接入真实摄像头和跌倒/姿态模型。
- 接入真实 ASR/TTS，替换 `voice-hmi-service/app/asr.py` 与 `tts.py`。
- 接入微信公众号或企业微信，替换 `wechat-adapter` mock。
- 在 RK3588 上接入 RKLLM 或本地 OpenAI-compatible 模型服务。
- 增加设备 ack 超时重试、事件复盘报表、家属多端权限。
