# v2 三级 AI 与多关键帧视觉架构

## 处理链路

```text
传感器 / 摄像头
  -> Level 1 规则门控与 20 分钟状态窗口
  -> 视觉事件收集 T-4s、T-2s、T、T+2s、T+4s
  -> Level 2 RK3588 InternVL3.5-4B 本地多模态分析
  -> Edge MCP Device Policy 执行本地策略
  -> Level 3 云端 OpenAI-compatible 模型异步复核（仅 P0-P2）
  -> 最终建议与 Dashboard 展示
```

P0 规则命中后立即执行本地告警及安全动作，不等待关键帧、本地模型或云端模型。模型只允许保持或提高规则风险，所有设备控制仍由 Edge MCP Device Policy 审核执行。

## 视觉接口

### 上传预览帧

```http
POST /api/v2/vision/frames
Content-Type: multipart/form-data
```

字段：`image`、`elder_id`、`camera_id`、`room`、`captured_at`。单张上传最大 5 MB，保存前最长边缩放到 1280，JPEG 质量为 80。

### 报告视觉触发

```http
POST /api/v2/vision/triggers
Content-Type: application/json
```

示例：

```json
{
  "elder_id": "elder_001",
  "camera_id": "living-room-camera",
  "room": "living_room",
  "event_type": "suspected_fall",
  "confidence": 0.91,
  "triggered_at": "2026-06-14T12:00:00+08:00"
}
```

### 查询关键帧

```http
GET /api/v2/vision/events/{frame_set_id}/frames
GET /api/v2/vision/snapshots/{snapshot_id}
```

## 图片与模型输入

快照保存在：

```text
data/snapshots/YYYY/MM/DD/{elder_id}/{frame_set_id}/
```

目录包含五个时间槽原图、`contact_sheet.jpg` 和 `manifest.json`。缺失时间槽不会复制其他图片，联系表使用 `missing` 占位。默认保留 7 天，正在分析的目录不会被清理。

本地 `internvl3.5-4b-rk3588` 仅接收一张时序联系表。云端复核在至少有 3 张原图时，按时间顺序接收最多 5 张独立原图。MQTT 和 SQLite 只保存引用与元数据，不传 base64。

## 环境变量

```bash
LLM_MOCK=false
LLM_BASE_URL=http://172.30.0.1:8001/v1
LLM_API_KEY=local-rk3588
LLM_MODEL=internvl3.5-4b-rk3588
LLM_CHAIN_TIMEOUT_SEC=300

VISION_FRAME_WAIT_SEC=5
VISION_RETENTION_DAYS=7
VISION_BUFFER_SECONDS=12
VISION_MAX_IMAGE_BYTES=5242880
VISION_MAX_IMAGE_EDGE=1280
VISION_JPEG_QUALITY=80

CLOUD_LLM_ENABLED=false
CLOUD_LLM_BASE_URL=
CLOUD_LLM_API_KEY=
CLOUD_LLM_MODEL=
CLOUD_LLM_TIMEOUT_SEC=60
```

云端默认关闭。启用后仅 P0、P1、P2 进入异步复核，失败或超时保持本地结果且不自动重试。

## RK3588 镜像部署

GitHub Actions 构建并发布 ARM64 `latest` 镜像后，在开发机执行：

```bash
cd /opt/elder-guardian-agent
./update_latest.sh
```

`guardian-orchestrator` 只读挂载 `/app/data/snapshots`，`vision-service` 读写同一目录。本地模型 API 必须能从容器通过 `http://172.30.0.1:8001/v1` 访问。

每次执行 `update_latest.sh` 都会把远端 `.env` 的本地模型配置同步为 InternVL3.5-4B，并在使用 GHCR 时强制更新为 `IMAGE_TAG=latest`；已有云端 API Key 和云端开关不会被覆盖。
