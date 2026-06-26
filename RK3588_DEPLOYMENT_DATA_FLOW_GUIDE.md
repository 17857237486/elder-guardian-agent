# RK3588 部署、数据链路与真实数据接入说明

本文用于把本项目交给其他人部署、演示和接入真实数据。项目名称为“基于 RK3588 边缘计算的居家老人健康守护与环境协同 Agent 系统”。

## 1. 如何部署到 RK3588

### 1.1 部署前准备

RK3588 设备建议准备：

- Ubuntu 22.04 或兼容 Linux 系统。
- Docker 与 Docker Compose。
- 能访问 GitHub Container Registry，即 `ghcr.io`。
- 与开发电脑在同一局域网，例如 RK3588 地址为 `192.168.10.64`。
- 如使用云端复核，需要 RK3588 能访问云端大模型 API。
- 如使用真实摄像头，需要摄像头提供 HTTP snapshot 地址。

开发电脑需要：

- 能 SSH 到 RK3588，例如：

```bash
ssh root@192.168.10.64
```

- 本仓库代码在本地，例如：

```text
D:\3_Works\2026.6\elderly_Project_4
```

### 1.2 推荐部署方式：GitHub Actions 构建镜像，RK3588 拉取镜像

推荐流程：

1. 本地修改代码并测试。
2. 提交并推送到 GitHub `main`。
3. 等待 GitHub Actions 的 `Docker Images` workflow 构建 ARM64 `latest` 镜像。
4. 在 RK3588 上拉取 `latest` 镜像并重启服务。

RK3588 上的 `.env` 至少需要包含：

```bash
IMAGE_PREFIX=ghcr.io/17857237486/elder-guardian-agent
IMAGE_TAG=latest

PUBLIC_EDGE_API_BASE=http://192.168.10.64:8010
PUBLIC_GUARDIAN_API_BASE=http://192.168.10.64:8000

LLM_BASE_URL=http://172.30.0.1:8001/v1
LLM_API_KEY=local-rk3588
LLM_MODEL=internvl3.5-4b-rk3588
CORE_LLM_MOCK=true
LLM_MOCK=false

CLOUD_LLM_ENABLED=false
CLOUD_LLM_BASE_URL=
CLOUD_LLM_API_KEY=
CLOUD_LLM_MODEL=

AUTO_PERSONAL_BASELINE_ENABLED=false
AUTO_CANDIDATE_ENABLED=true
BACKGROUND_MAX_RECORDS=3100
```

如果 GHCR 镜像是私有的，先在 RK3588 登录：

```bash
docker login ghcr.io
```

### 1.3 一键部署命令

在开发电脑执行：

```powershell
wsl bash -lc "cd /mnt/d/3_Works/2026.6/elderly_Project_4 && REMOTE_HOST=192.168.10.64 REMOTE_USER=root ./scripts/deploy_rk3588.sh"
```

这个脚本会：

- 把当前仓库同步到 RK3588 的 `/opt/elder-guardian-agent`。
- 保留 RK3588 上已有 `.env`。
- 调用 RK3588 上的更新脚本。
- 如果 `.env` 中设置了 `IMAGE_PREFIX`，就使用 `docker-compose.images.yml` 拉取镜像并重启服务。

### 1.4 最小更新某些服务

如果只想更新前端或单个服务，可在 RK3588 上执行：

```bash
cd /opt/elder-guardian-agent
docker compose -f docker-compose.images.yml pull web-dashboard elder-hmi background-mqtt
docker compose -f docker-compose.images.yml up -d --no-deps web-dashboard elder-hmi background-mqtt
```

常用服务名：

```text
mosquitto
edge-mcp-server
guardian-orchestrator
background-mqtt
vision-service
web-dashboard
elder-hmi
guardian-core
wechat-adapter
voice-hmi-service
```

### 1.5 部署后检查

在开发电脑或 RK3588 上检查容器：

```bash
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml ps"
```

检查主要健康接口：

```bash
curl http://192.168.10.64:8010/health
curl http://192.168.10.64:8020/health
curl http://192.168.10.64:8090/api/health
curl http://192.168.10.64:8101/health
```

主要网页：

```text
MQTT 传感器数据记录: http://192.168.10.64:8090
Dashboard:              http://192.168.10.64:5173
老人 HMI:               http://192.168.10.64:5174
Edge API:               http://192.168.10.64:8010
Orchestrator API:       http://192.168.10.64:8020
Vision Service:         http://192.168.10.64:8101
```

## 2. 项目数据链路

### 2.1 总体链路

```text
传感器 / 手表 / 摄像头 / 8090 模拟页面
  ↓
MQTT / HTTP
  ↓
Mosquitto
  ↓
edge-mcp-server
  ↓
v2_raw_observations 原始记录
  ↓
规则判断 / 行为片段 / 个人基线 / Candidate
  ↓
guardian-orchestrator
  ↓
workflow
  ↓
本地 RK3588 模型 / 云端复核 / 确定性规则跳过
  ↓
设备策略
  ↓
HMI 老人反馈 / 家属告警 / Dashboard 展示
```

### 2.2 生命体征数据链路

输入 topic：

```text
elder/{elder_id}/sensor/vital
```

典型数据：

```json
{
  "elder_id": "elder_001",
  "heart_rate": 78,
  "spo2": 96,
  "systolic": 128,
  "diastolic": 80,
  "body_temperature": 36.6,
  "observed_at": "2026-06-26T10:00:00+08:00"
}
```

处理逻辑：

- 心率 `<45` 或 `>130`：硬规则 `heart_rate_abnormal P1`。
- 血氧 `<92`：硬规则 `spo2_low P1`。
- 血氧 `<88`：硬规则 `spo2_low P0`。
- 硬规则生命体征事件不再调用 RK3588 本地模型。
- 轻度心率、轻度血氧异常通过个人基线和 24 组摘要生成 Candidate，再由本地模型复核。

### 2.3 整屋环境与红外 presence 数据链路

输入 topic：

```text
elder/{elder_id}/sensor/env
```

推荐整屋快照格式：

```json
{
  "schema": "home_environment_snapshot_v1",
  "elder_id": "elder_001",
  "rooms": {
    "bedroom": {
      "temperature": 24.0,
      "humidity": 50.0,
      "co2_ppm": 820,
      "gas_ppm": 0,
      "smoke_ppm": 0,
      "presence": false
    },
    "bathroom": {
      "temperature": 24.5,
      "humidity": 58.0,
      "co2_ppm": 780,
      "gas_ppm": 0,
      "smoke_ppm": 0,
      "presence": false
    },
    "living_room": {
      "temperature": 24.8,
      "humidity": 49.5,
      "co2_ppm": 900,
      "gas_ppm": 0,
      "smoke_ppm": 0,
      "presence": true
    },
    "kitchen": {
      "temperature": 26.0,
      "humidity": 54.0,
      "co2_ppm": 880,
      "gas_ppm": 0,
      "smoke_ppm": 0,
      "presence": false
    }
  }
}
```

处理逻辑：

- `presence=true` 的房间代表老人当前所在房间。
- P3 温度、湿度、CO2 环境事件只对老人所在房间有意义。
- 燃气异常是全屋安全风险，不受 presence 限制。
- 红外 presence 会生成行为片段，例如 `room_stay`、`bathroom_stay`。
- 卫生间停留过长通过 `bathroom_stay` 行为片段和 `bathroom_routine` 个人基线生成 Candidate。

### 2.4 视觉图片链路

当前视觉链路：

```text
真实摄像头拍照 / 8090 导入五张图片
  ↓
vision-service pending captures
  ↓
触发 suspected_fall 或 long_static
  ↓
生成 frame_set_id 和 manifest
  ↓
本地模型分析中间三张图片
  ↓
云端复核五张原图 + 最近生命体征/环境摘要
  ↓
final_advisory
  ↓
Dashboard / HMI / 家属告警
```

关键点：

- 本地 RK3588 模型只处理五张中的第 2、3、4 张。
- 云端复核处理五张原图。
- 云端复核会结合最近生命体征和环境摘要，给出更完整的事件语义。
- `long_static` 允许在严格条件下降级：生命体征正常、视觉无异常、表情正常、可解释为休息状态时，可降为 P4 且不通知。
- `suspected_fall` 不允许随意降级到低风险。

### 2.5 行为片段、个人基线和 Candidate

后台行为链路：

```text
v2_raw_observations
  ↓
Behavior Segment Worker
  ↓
behavior_segments
  ↓
personal_baselines
  ↓
ai_review_candidates
  ↓
guardian-orchestrator 本地模型复核
```

当前主要 Candidate：

- `vital_baseline_anomaly`：轻度心率或血氧相对个人基线异常。
- `bathroom_stay_anomaly`：卫生间停留时间超过个人参考上限。

Candidate 不等同于正式风险事件：

- 本地模型输出 `P4/P3`：candidate dismissed，不升级风险。
- 本地模型输出 `P2/P1/P0`：candidate promoted，创建正式风险事件。

## 3. 各个网页的作用

### 3.1 MQTT 传感器数据记录页面

地址：

```text
http://192.168.10.64:8090
```

作用：

- 发送生命体征、环境、presence 模拟数据。
- 触发 P0-P4 风险事件时间轴。
- 设置手动个人基线。
- 自动生成心率、血氧、卫生间停留基线。
- 验证卫生间停留时间，展示老人从其他房间进入卫生间、停留、离开的环境记录。
- 导入五张图片到 Vision Service。
- 观察 MQTT 回流记录。
- 查看设备状态和设备动作日志。

适合演示人员使用。

### 3.2 Dashboard

地址：

```text
http://192.168.10.64:5173
```

作用：

- 展示当前演示事件链路。
- 展示 P0-P4 风险事件状态。
- 展示本地模型事件语义和云端复核事件语义。
- 展示老人 HMI 反馈。
- 展示家属告警。
- 展示整屋环境状态。
- 控制房间设备开关。
- 展示策略与设备执行记录。
- 展示每日健康摘要和近 30 天身体健康趋势。

适合家属、老师、评委或开发调试人员观看。

### 3.3 老人 HMI

地址：

```text
http://192.168.10.64:5174
```

作用：

- 面向老人显示大字提示。
- 风险事件发生时询问老人状态。
- 提供三个反馈按钮：
  - 我没事
  - 需要帮助
  - 联系家属
- 老人点击后，反馈会写入系统并显示在 Dashboard。

适合放在 RK3588 屏幕或老人端触摸屏上。

### 3.4 Edge API

地址：

```text
http://192.168.10.64:8010
```

作用：

- 传感器和设备数据入口。
- Dashboard state 数据来源。
- 行为片段、个人基线、Candidate、事件、workflow、HMI、告警等数据查询。

常用检查：

```bash
curl http://192.168.10.64:8010/health
curl http://192.168.10.64:8010/api/v2/dashboard/state
```

### 3.5 Orchestrator API

地址：

```text
http://192.168.10.64:8020
```

作用：

- 规则触发。
- workflow 编排。
- 调用本地 RK3588 模型。
- 调用云端复核。
- 生成最终建议。

常用检查：

```bash
curl http://192.168.10.64:8020/health
```

### 3.6 Vision Service

地址：

```text
http://192.168.10.64:8101
```

作用：

- 真实摄像头拍照。
- 导入五张图片。
- 管理 pending captures。
- 触发视觉事件时生成五张关键帧 manifest。
- 生成本地三图 contact sheet。

常用检查：

```bash
curl http://192.168.10.64:8101/health
```

## 4. 哪些内容需要真实数据输入

项目可以用 8090 页面完整模拟演示，但如果要接近真实应用，以下内容建议使用真实数据。

### 4.1 必须或强烈建议接入真实数据的内容

#### 生命体征

来源：

- 手表。
- 血氧仪。
- 心率带。
- 其他可通过 MQTT/HTTP 上报的设备。

建议真实输入：

- 心率。
- 血氧。
- 血压。
- 体温。

用途：

- P1/P0 硬规则判断。
- 个人基线统计。
- Candidate 复核。
- 云端复核时辅助解释视觉事件。
- 每日健康摘要和 30 日趋势。

#### 整屋环境数据

来源：

- 温湿度传感器。
- CO2 传感器。
- 燃气传感器。
- 烟雾传感器。

建议真实输入：

- 四个房间的温度、湿度、CO2、燃气、烟雾。
- 房间包括卧室、卫生间、客厅、厨房。

用途：

- P3 环境事件。
- P0 燃气事件。
- Dashboard 整屋环境展示。
- 云端复核视觉事件时辅助判断环境风险。

#### 红外 presence

来源：

- 四个房间的 PIR 红外传感器。
- 毫米波存在传感器也可以，但需要转换成房间 presence。

建议真实输入：

```text
bedroom.presence
bathroom.presence
living_room.presence
kitchen.presence
```

用途：

- 判断老人当前所在房间。
- P3 环境事件只对老人所在房间触发。
- 生成 `room_stay` 行为片段。
- 生成 `bathroom_stay` 行为片段。
- 判断卫生间停留过长 Candidate。

#### 视觉图片

来源：

- 真实摄像头 HTTP snapshot。
- 或演示时从本地选择五张图片导入。

用途：

- 疑似跌倒。
- 长时间静止。
- 姿态异常。
- 表情或疼痛状态辅助识别。

本地模型只处理中间三张，云端复核五张原图。

### 4.2 可以继续模拟的数据

#### 设备执行

演示阶段可以先用模拟设备：

- 空调。
- 风扇。
- 窗户。
- 灯光。
- 燃气阀。
- 本地报警器。
- 卫生间取暖器。

真实接入时，仍应走 Edge MCP 的设备策略，不要让前端或 LLM 直接控制设备。

#### 家属告警

当前可以用 mock 家属告警或微信适配器。

真实接入时可以接：

- 微信服务号。
- 企业微信。
- 短信。
- 电话通知。

#### 云端复核

演示阶段可以关闭：

```bash
CLOUD_LLM_ENABLED=false
```

需要真实云端复核时再配置：

```bash
CLOUD_LLM_ENABLED=true
CLOUD_LLM_BASE_URL=...
CLOUD_LLM_API_KEY=...
CLOUD_LLM_MODEL=...
```

## 5. 推荐演示流程

### 5.1 P4 正常数据

在 8090 选择：

```text
P4 正常状态
```

预期：

- Dashboard 显示正常数据已记录。
- 不创建风险 workflow。
- HMI 不弹出询问。

### 5.2 P3 环境事件

在 8090 选择：

```text
P3 湿度异常
P3 室温过高
P3 室温过低
P3 CO2 偏高
```

预期：

- Dashboard 显示 P3 事件。
- 本地 AI 和云端复核跳过。
- HMI 显示中文提示。
- 设备策略按规则执行或记录。

### 5.3 P1 心率或低血氧

在 8090 选择：

```text
P1 心率异常
P1 低血氧
```

预期：

- 硬规则直接触发。
- 不调用 RK3588 本地模型。
- Dashboard 显示确定性生命体征规则。
- HMI 询问老人。
- 家属告警显示中文具体数值。

### 5.4 P0 燃气或严重低血氧

在 8090 选择：

```text
P0 燃气异常
P0 严重低血氧
```

预期：

- 立即紧急处置。
- 家属告警。
- HMI 可反馈，但反馈不会撤销紧急动作。
- 冷却窗口内不重复创建大量 workflow。

### 5.5 卫生间停留过长

在 8090：

1. 设置卫生间停留基线。
2. 在“验证卫生间停留时间”中输入超过参考上限的秒数。
3. 点击验证。

预期：

- 环境数据记录显示老人从客厅进入卫生间，再离开卫生间。
- 如果超过参考上限，生成 `bathroom_stay_anomaly` Candidate。
- Candidate 经本地模型复核后，可能 dismissed 或 promoted。

### 5.6 视觉事件

在 8090：

1. 导入五张图片到视觉服务。
2. 选择 `P1 疑似跌倒` 或 `P2 长时间静止`。
3. 发送风险事件时间轴。

预期：

- 本地模型使用第 2、3、4 张。
- 云端复核使用五张原图，并结合最近生命体征和环境摘要。
- Dashboard 显示本地模型语义、云端复核语义和 workflow。

## 6. 常见问题

### 6.1 P3 事件为什么不调用本地模型？

P3 温度、湿度、CO2 是确定性环境规则，系统可以直接判断并处置。为了降低 RK3588 本地模型负载，P3 默认跳过本地模型和云端复核。

### 6.2 P1 心率和低血氧为什么不调用本地模型？

心率和血氧属于明确生命体征硬规则。达到阈值后应立即处置，不应等待模型推理。因此 P1 心率、P1 低血氧、P0 严重低血氧直接走规则链路。

### 6.3 Candidate 为什么不是一出现就算风险？

Candidate 是“值得复核的候选事件”，不是正式风险。只有本地模型判断需要升级为 P2/P1/P0 时，才创建正式风险事件。

### 6.4 为什么 v1 guardian-core 默认不调用 LLM？

v1 `guardian-core` 保留兼容 API 和旧链路，但默认 `CORE_LLM_MOCK=true`，避免它占用 RK3588 本地模型。当前智能处理以 v2 `guardian-orchestrator` 为主。

### 6.5 真实设备应该怎么接入？

优先走 MQTT：

- 生命体征发到 `elder/{elder_id}/sensor/vital`。
- 整屋环境和 presence 发到 `elder/{elder_id}/sensor/env`。
- 设备状态发到 `home/{room}/{device}/state`。
- 设备 ACK 发到 `home/{room}/{device}/ack`。

不要让设备直接绕过 Edge MCP 控制策略。

