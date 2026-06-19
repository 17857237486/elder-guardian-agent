# Background MQTT 场景数据生成

这个目录用于生成单个老人居家 MQTT 模拟数据，并按主项目 v2 的标准 MQTT 协议发送给 Mosquitto。数据进入 Mosquitto 后由 `edge-mcp-server` 入库，再由 `guardian-orchestrator` 完成规则判断、workflow、本地 AI/云端复核和 Dashboard 展示。

支持的场景：

- `morning_getup`：老人早上起床
- `midday_nap`：老人中午午休
- `dinner`：老人晚上吃饭
- `night_bathroom`：夜间起夜
- `tv_evening`：客厅看电视
- `cooking`：厨房做饭
- `after_meal_walk`：饭后散步
- `sleep_night`：夜间睡眠

脚本支持的插入事件：

- `normal`：正常状态
- `spo2_critical`：严重低血氧，低于 88% 触发 P0
- `spo2_low`：低血氧，88%-91% 触发 P1
- `heart_rate_abnormal`：心率异常，高于 130 bpm 触发 P1
- `suspected_fall`：疑似跌倒，发布视觉事件并触发 P1
- `long_static`：长时间静止，发布视觉事件并触发 P2
- `co2_high`：CO2 偏高，高于 1500 ppm 触发 P3
- `gas_leak`：燃气异常，高于 100 ppm 触发 P0
- `temperature_high`：室温过高，达到 30°C 及以上触发 P3
- `temperature_low`：室温过低，达到 16°C 及以下触发 P3
- `humidity_abnormal`：湿度低于 25% 或高于 75% 触发 P3

默认采样策略：每 5 秒生成 1 条样本，每个场景 2 分钟共 24 条。每条样本会拆成两条主系统标准 MQTT 消息：

```text
elder/{elder_id}/sensor/vital
elder/{elder_id}/sensor/env
```

因此，`generate_scenario_data.py` 可以替代 `scripts/simulate_sensor.py` 给 v2 MQTT 链路使用。

## 启动主系统验证

1. 启动本机 Mosquitto：

```powershell
docker compose up mosquitto
```

2. 启动 v2 主系统：

```powershell
docker compose up -d
```

3. 选择一个场景，并按真实时间发送：

```powershell
conda activate elder-guardian-agent
python Background_MQTT\generate_scenario_data.py --scene morning_getup --host localhost --port 1883 --duration-sec 120 --interval-sec 5 --realtime
```

可把 `morning_getup` 换成：

```text
midday_nap
dinner
```

这样每 5 秒发送一组数据，一共持续约 2 分钟；每组数据包含 1 条生命体征消息和 1 条环境消息。

4. 查看主系统状态：

```text
http://localhost:8010/api/v2/dashboard/state
```

如果前端 dashboard 已启动，也可以打开：

```text
http://localhost:5173
```

## 单独打开 MQTT 数据记录网页

如果你想在 `Dashboard:5173` 之外，再单开一个网页记录每一次 MQTT 数据，启动这个独立网页后端：

```powershell
conda activate elder-guardian-agent
uvicorn Background_MQTT.backend:app --reload --host 0.0.0.0 --port 8090
```

然后打开：

```text
http://localhost:8090
```

这个网页会直接订阅：

```text
elder/+/sensor/vital
elder/+/sensor/env
elder/+/vision/event
home/bedroom/presence_sensor/state
```

所以你运行下面的场景发送脚本后，网页会逐条记录生命体征和环境数据：

```powershell
python Background_MQTT\generate_scenario_data.py --scene morning_getup --host localhost --port 1883 --duration-sec 120 --interval-sec 5 --realtime
```

网页也支持手动录入数据：

- `生命体征录入` 会发布到 `elder/{elder_id}/sensor/vital`
- `环境数据录入` 会发布到 `elder/{elder_id}/sensor/env`
- 提交成功后，网页等待 MQTT 回流再显示记录，避免只在前端假显示成功
- 如果 v2 主系统已经启动，RK3588 主系统会按同一条链路完成 Edge 入库、规则分级、workflow、本地 AI/云端复核和 Dashboard 推送

手动录入页面会展示当前 v2 真实生效的阈值：

- 心率：55-110 bpm 正常；低于 55 或高于 110 触发 P2；低于 45 或高于 130 触发 P1
- 血氧：大于等于 92% 正常；低于 92% 触发 P1；低于 88% 触发 P0
- CO2：低于 1500 ppm 正常；大于等于 1500 ppm 触发 P3
- 燃气：低于 100 ppm 正常；大于等于 100 ppm 触发 P0
- 温度：16-30°C 正常；低于等于 16°C 或大于等于 30°C 触发 P3
- 血压、体温当前只记录展示；湿度低于 25% 或高于 75% 触发 P3 环境事件

网页还提供验收用事件模板：

- `正常状态`：P4 正常记录
- `血氧异常`：血氧低于 88%，触发 `spo2_low` 紧急风险
- `心率异常`：心率高于 130，触发 `heart_rate_abnormal`
- `CO2 偏高`：CO2 高于 1500 ppm，触发 `co2_high`
- `燃气泄漏`：燃气高于 100 ppm，触发 `gas_leak` P0 告警

点击模板后会自动填入生命体征和环境数据。评委可以直接点击 `一键发送当前模板`，也可以先修改具体数值，再单独提交生命体征或环境数据。

网页还提供 `风险事件时间轴触发`：

- 选择风险事件：`正常状态`、`严重低血氧`、`低血氧`、`心率异常`、`疑似跌倒`、`长时间静止`、`CO2 偏高`、`燃气异常`、`室温过高`、`室温过低`、`湿度异常`
- 选择风险发生房间：`bedroom`、`bathroom`、`living_room`、`kitchen`
- 通过滑块选择触发时间，例如第 `60` 秒
- 点击 `生成并发送风险事件时间轴`

这种方式会先生成 2 分钟基础 MQTT 数据，再在触发点前后向指定房间平滑注入风险事件。例如 `kitchen + 燃气异常 + 第 60 秒` 会让厨房燃气数据从低值逐步升高，并在触发点后超过 P0 阈值，而不是突然发送一条孤立异常值。

页面展示的 v2 处理链路为：

```text
网页提交 -> MQTT 发布 -> Mosquitto -> Edge MCP -> v2_raw_observations
-> Orchestrator 规则判断 -> workflow -> local_context_fusion
-> 本地 AI / 确定性规则跳过 -> 云端复核可选
-> 设备策略 -> HMI / 家属告警 -> Dashboard
```

对应接口：

```text
POST /api/scenario/publish
POST /api/scenario/start
POST /api/scenario/stop
GET  /api/scenario/status
```

推荐网页使用 `/api/scenario/start` 启动场景任务，再通过 `/api/scenario/status` 查询进度；如果按真实时间发送时需要中断，调用 `/api/scenario/stop`。`/api/scenario/publish` 保留兼容旧调用，内部等价于启动一个场景任务。

示例请求体：

```json
{
  "scene": "tv_evening",
  "event_type": "gas_leak",
  "trigger_second": 60,
  "elder_id": "elder_001",
  "duration_sec": 120,
  "interval_sec": 5,
  "realtime": false
}
```

## 采样间隔建议

建议使用 `5 秒/条`：

- 两分钟内每个场景 24 条，趋势足够清晰
- 数据量适中，后端、数据库和 dashboard 都容易观察
- 比 `1 秒/条` 更贴近日常居家传感器上报节奏

如果想更细粒度，可以改成：

```powershell
python Background_MQTT\generate_scenario_data.py --scene morning_getup --interval-sec 2 --realtime
```
