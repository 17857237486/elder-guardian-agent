# Behavior Baseline Context Guide

本文说明 v2 的长期行为片段、个人基线摘要和 AI review candidate 如何使用与验证。

## 1. 数据链路

```text
传感器 / 模拟器
  -> MQTT
  -> Mosquitto
  -> edge-mcp-server
  -> v2_raw_observations
  -> Behavior Segment Worker
  -> v2_behavior_segments
  -> Baseline Worker
  -> v2_personal_baselines
  -> ai_review_candidates
  -> guardian-orchestrator
  -> local_context_fusion
  -> local_multiframe_analysis
```

长期 worker 是低优先级后台任务。它平时持续整理 observation，不等风险发生才运行。

起夜本身不再是正式风险事件。起夜异常先进入 `ai_review_candidates`，只有本地 AI 输出 `P2/P1/P0` 后，才升级为正式风险事件并进入 HMI、家属告警和策略执行链路。

## 2. MQTT 输入示例

### Presence

```bash
mosquitto_pub -h 192.168.10.64 -p 1883 \
  -t elder/elder_001/sensor/env \
  -m '{
    "schema": "home_environment_snapshot_v1",
    "elder_id": "elder_001",
    "rooms": {
      "bedroom": {"temperature": 24.2, "humidity": 50, "co2_ppm": 820, "gas_ppm": 0, "smoke_ppm": 0, "presence": false},
      "bathroom": {"temperature": 25.0, "humidity": 62, "co2_ppm": 780, "gas_ppm": 0, "smoke_ppm": 0, "presence": true},
      "living_room": {"temperature": 24.8, "humidity": 49, "co2_ppm": 900, "gas_ppm": 0, "smoke_ppm": 0, "presence": false},
      "kitchen": {"temperature": 26.0, "humidity": 54, "co2_ppm": 880, "gas_ppm": 0, "smoke_ppm": 0, "presence": false}
    }
  }'
```

`edge-mcp-server` 会拆成 4 条 `environment` observation 和 4 条 `device_state` presence observation。

### Vital

```bash
mosquitto_pub -h 192.168.10.64 -p 1883 \
  -t elder/elder_001/sensor/vital \
  -m '{"elder_id":"elder_001","heart_rate":96,"spo2":95}'
```

Vital worker 会按 5 分钟窗口生成：

- `heart_rate_window`
- `spo2_window`

## 3. 查看行为片段

```bash
curl "http://192.168.10.64:8010/api/v2/behavior-segments?elder_id=elder_001&limit=20"
```

常见 `segment_type`：

- `room_stay`
- `night_sleep`
- `night_wake`
- `bathroom_stay`
- `heart_rate_window`
- `spo2_window`

## 4. 查看个人基线摘要

```bash
curl "http://192.168.10.64:8010/api/v2/personal-baselines?elder_id=elder_001"
```

常见 `baseline_type`：

- `night_routine`
- `bathroom_routine`
- `heart_rate_daily`
- `spo2_daily`

数据不足时 `quality=insufficient_data`，系统会使用默认 p90/p10。

## 5. 查看 AI Review Candidate

```bash
curl "http://192.168.10.64:8010/api/v2/ai-review-candidates?elder_id=elder_001&limit=20"
```

Candidate 状态：

- `pending`
- `reviewing`
- `dismissed`
- `promoted`
- `failed`

## 6. 手动设置个人基线

```bash
curl -X POST http://192.168.10.64:8010/api/v2/personal-baselines \
  -H "Content-Type: application/json" \
  -d '{
    "elder_id": "elder_001",
    "baseline_type": "night_routine",
    "scope": "default",
    "timezone": "Asia/Shanghai",
    "lookback_days": 14,
    "sample_count": 14,
    "quality": "stable",
    "metrics": {
      "usual_sleep_start": "22:35",
      "usual_sleep_end": "06:18",
      "night_wake_count_p90": 2,
      "night_wake_duration_p90_sec": 480,
      "returned_to_bedroom_rate": 0.94
    }
  }'
```

```bash
curl -X POST http://192.168.10.64:8010/api/v2/personal-baselines \
  -H "Content-Type: application/json" \
  -d '{
    "elder_id": "elder_001",
    "baseline_type": "bathroom_routine",
    "scope": "default",
    "timezone": "Asia/Shanghai",
    "lookback_days": 14,
    "sample_count": 20,
    "quality": "stable",
    "metrics": {
      "bathroom_stay_avg_sec": 160,
      "bathroom_stay_p90_sec": 360,
      "night_bathroom_visits_avg": 0.8
    }
  }'
```

## 7. 手动插入行为片段

```bash
curl -X POST http://192.168.10.64:8010/api/v2/behavior-segments \
  -H "Content-Type: application/json" \
  -d '{
    "segment_id": "seg_test_night_wake",
    "elder_id": "elder_001",
    "segment_type": "night_wake",
    "start_at": "2026-06-18T23:10:00+08:00",
    "end_at": null,
    "duration_seconds": 720,
    "room": "bedroom",
    "source_kinds": ["device_state"],
    "features": {
      "rooms": ["bedroom", "bathroom", "living_room"],
      "returned_to_bedroom": false,
      "bathroom_stay_seconds": 180,
      "night_key": "2026-06-18"
    },
    "status": "open"
  }'
```

## 8. 手动制造 Candidate

```bash
curl -X POST http://192.168.10.64:8010/api/v2/ai-review-candidates \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_id": "cand_test_night_wake",
    "elder_id": "elder_001",
    "candidate_type": "night_behavior_anomaly",
    "priority": "low",
    "reason": "起夜持续时间超过个人90分位",
    "source_segment_ids": ["seg_test_night_wake"],
    "features": {
      "duration_seconds": 720,
      "baseline_p90_seconds": 480
    }
  }'
```

如果 `ORCHESTRATOR_URL` 已配置，Edge 会自动把 candidate 转发给 Orchestrator：

```text
POST /api/v2/orchestrator/candidates
```

## 9. Candidate 处理规则

本地 AI 输出：

- `P4/P3`：candidate 标记为 `dismissed`，只记录。
- `P2/P1/P0`：candidate 标记为 `promoted`，创建正式风险事件，进入 HMI/告警/策略链路。
- 模型失败或超时：candidate 标记为 `failed`，不升级。

## 10. RK3588 部署后验证

健康检查：

```bash
curl http://192.168.10.64:8010/health
curl http://192.168.10.64:8020/health
curl http://192.168.10.64:5173
```

验证 API 写入：

```bash
curl "http://192.168.10.64:8010/api/v2/dashboard/state?elder_id=elder_001"
```

验证 candidate workflow：

```bash
curl "http://192.168.10.64:8010/api/v2/workflow-steps?elder_id=elder_001&limit=20"
```

应看到：

- `candidate_received`
- `local_context_fusion`
- `local_multiframe_analysis`
- `candidate_decision`

如果 AI 判断低风险，应看到 candidate `status=dismissed`。

如果 AI 判断 `P2/P1/P0`，应看到 candidate `status=promoted`，并带有 `promoted_event_id`。
