# Candidate 本地模型输入与处理说明

本文说明 `ai_review_candidates` 进入 RK3588 本地模型时的最小输入、串行队列、状态流转和验证方法。

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
  -> candidate 本地串行队列
  -> local_multiframe_analysis
```

Candidate 不是正式风险事件。它表示“这段行为可能值得 AI 复核”。本地模型输出 `P4/P3` 时只记录并 `dismissed`；输出 `P2/P1/P0` 时才 `promoted` 为正式风险事件。

## 2. 本地模型实际输入

workflow 只保存并发送一个短对象：

```json
{
  "candidate_local_input": {
    "t": "night_behavior_anomaly",
    "r": "起夜持续时间超过个人90分位",
    "dur": 720,
    "p90s": 480,
    "ret": false,
    "bath_s": 180,
    "rooms": ["bedroom", "bathroom", "living_room"],
    "room": "living_room",
    "temp": 25.0,
    "hum": 52,
    "hr": 92,
    "spo2": 95
  }
}
```

字段含义：

- `t`: candidate 类型，例如 `night_behavior_anomaly` 或 `vital_baseline_anomaly`
- `r`: 生成 candidate 的短原因
- `dur`: 当前片段持续秒数
- `p90s`: 个人基线 90 分位秒数
- `p90` / `p10`: 生命体征个人基线分位值
- `ret`: 是否已回到卧室
- `bath_s`: 卫生间停留秒数
- `rooms`: 本次行为涉及的房间序列
- `room`: 当前房间
- `temp` / `hum`: 最新温度、湿度
- `hr` / `spo2`: 最新心率、血氧

不会发送原始 observation 列表、设备快照、完整行为片段、完整个人基线、最近 20 组数据、历史 `local_result`、`dedupe_key` 或嵌套 segment 全量对象。

## 3. 本地模型输出

Candidate 专用 prompt 只要求 4 个字段：

```json
{
  "event_semantics": "起夜时间偏长",
  "risk_level": "P3",
  "confidence": 0.72,
  "family_summary": "起夜时间偏长，已记录观察"
}
```

Orchestrator 会把它归一化成现有 9 字段结构；缺少 `supporting_evidence` 时，会用 candidate reason 自动补一条短证据，保证 Dashboard 和 workflow 兼容。

## 4. 串行队列

Candidate 的本地模型调用经过 Orchestrator 内部的专用串行锁：

```text
candidate A context fusion -> 等锁 -> 调本地模型
candidate B context fusion -> 等锁 -> 调本地模型
candidate C context fusion -> 等锁 -> 调本地模型
```

锁只包住本地模型请求阶段，不阻塞 candidate 入库、workflow 创建和 context fusion。正式风险事件仍使用原来的 workflow 并发控制，不受 candidate 队列影响。

`local_multiframe_analysis.output` 会记录：

- `latency_ms`: 单次模型实际推理耗时
- `queue_wait_ms`: 排队等待耗时

35 秒目标看 `latency_ms`，不是 `queue_wait_ms + latency_ms`。

## 5. 手动创建 candidate

起夜异常：

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

生命体征基线异常：

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

## 6. 查看结果

```bash
curl "http://192.168.10.64:8010/api/v2/ai-review-candidates?elder_id=elder_001&limit=20"
curl "http://192.168.10.64:8010/api/v2/workflow-steps?elder_id=elder_001&limit=80"
```

重点检查：

- `local_context_fusion.output.candidate_local_input`
- `local_multiframe_analysis.output.fallback`
- `local_multiframe_analysis.output.latency_ms`
- `local_multiframe_analysis.output.queue_wait_ms`
- `candidate_decision.output.status`

## 7. RK3588 验收标准

热模型状态下连续创建 3 个新 candidate，预期：

```text
candidate status = dismissed 或 promoted
local_multiframe_analysis.output.fallback = false
不出现 503 Service Unavailable
local_multiframe_analysis.output.latency_ms <= 35000
```

如果 `queue_wait_ms` 大于 0，说明串行队列正在保护本地模型；这不计入单次推理耗时。
