# Candidate Local Model Context Guide

本文说明 `ai_review_candidates` 进入 RK3588 本地模型时实际使用的精简输入、处理流程和验证方法。

## 1. 数据链路

```text
v2_raw_observations
  -> Behavior Segment Worker
  -> behavior_segments
  -> Baseline Worker
  -> personal_baselines
  -> ai_review_candidates
  -> guardian-orchestrator
  -> local_context_fusion
  -> local_multiframe_analysis
```

Candidate 不是正式风险事件。它只表示“这段行为可能值得 AI 看一眼”。本地模型输出 `P4/P3` 时只记录并 dismiss；输出 `P2/P1/P0` 时才升级为正式风险事件。

## 2. 本地模型输入字段

Candidate 本地模型只接收摘要，不接收原始历史数据。

实际输入位于 workflow 的 `local_context_fusion.output.candidate_local_input`，字段包括：

```json
{
  "candidate_type": "night_behavior_anomaly",
  "reason": "起夜持续时间超过个人90分位",
  "duration_seconds": 720,
  "baseline_p90_seconds": 480,
  "returned_to_bedroom": false,
  "bathroom_stay_seconds": 180,
  "room_sequence": ["bedroom", "bathroom", "living_room"],
  "current_room": "living_room",
  "latest_environment": {
    "room": "living_room",
    "temperature": 25.0,
    "humidity": 52,
    "co2_ppm": 850,
    "presence": true
  },
  "latest_vital": {
    "heart_rate": 92,
    "spo2": 95
  },
  "baseline": {
    "night_routine": {
      "night_wake_duration_p90_sec": 480,
      "night_wake_count_p90": 2
    }
  }
}
```

不会输入：

- 原始 observation 列表
- devices 快照
- 完整 behavior segment
- 完整 personal baseline
- 最近 20 组环境或生命体征
- 历史 `local_result`
- `dedupe_key`
- 嵌套 `segment` 全量对象

## 3. 起夜异常示例

手动创建起夜 candidate：

```bash
curl -X POST http://192.168.10.64:8010/api/v2/ai-review-candidates \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_id": "cand_fast_night_1",
    "elder_id": "elder_001",
    "candidate_type": "night_behavior_anomaly",
    "priority": "low",
    "reason": "起夜持续时间超过个人90分位",
    "source_segment_ids": ["seg_test_night_wake"],
    "features": {
      "duration_seconds": 720,
      "baseline_p90_seconds": 480,
      "returned_to_bedroom": false,
      "bathroom_stay_seconds": 180
    }
  }'
```

本地模型只判断：这次起夜持续时间、是否回卧室、卫生间停留和个人 p90 相比，是否需要升级为正式风险。

## 4. 生命体征基线异常示例

```bash
curl -X POST http://192.168.10.64:8010/api/v2/ai-review-candidates \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_id": "cand_fast_hr_1",
    "elder_id": "elder_001",
    "candidate_type": "vital_baseline_anomaly",
    "priority": "low",
    "reason": "心率窗口高于个人90分位",
    "source_segment_ids": ["seg_hr_window_1"],
    "features": {
      "metric": "heart_rate",
      "latest_value": 104,
      "baseline_p90": 96
    }
  }'
```

本地模型只判断：当前生命体征是否只是轻微偏离个人基线，还是需要升级为正式关注事件。

## 5. 本地模型输出

本地模型必须输出顶层 JSON：

```json
{
  "event_semantics": "起夜时间偏长",
  "risk_level": "P3",
  "confidence": 0.72,
  "supporting_evidence": ["持续时间超过个人90分位"],
  "family_summary": "起夜时间偏长，已记录观察"
}
```

结果处理：

- `P4/P3`：candidate 标记为 `dismissed`
- `P2/P1/P0`：candidate 标记为 `promoted`，创建正式风险事件
- 模型失败、超时、非法 JSON、复合风险等级或设备控制字段：candidate 标记为 `failed`

## 6. 查看 workflow

查看 candidate：

```bash
curl "http://192.168.10.64:8010/api/v2/ai-review-candidates?elder_id=elder_001&limit=20"
```

查看 workflow steps：

```bash
curl "http://192.168.10.64:8010/api/v2/workflow-steps?elder_id=elder_001&limit=80"
```

重点检查：

- `local_context_fusion.output.candidate_local_input`
- `local_multiframe_analysis.output.latency_ms`
- `local_multiframe_analysis.output.fallback`
- `candidate_decision.output.status`

## 7. 30 秒验收

热模型状态下，连续创建 3 个新的 candidate，均满足：

```text
local_multiframe_analysis.output.fallback = false
candidate status = dismissed 或 promoted
local_multiframe_analysis.output.latency_ms < 30000
```

如果仍超过 30 秒，本轮不通过调整超时、模型参数、并发或服务配置规避；只记录失败原因，后续再评估模型规格或更小的本地文本模型。
