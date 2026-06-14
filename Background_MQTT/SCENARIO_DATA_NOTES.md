# 场景数据生成备注

本文说明 `generate_scenario_data.py` 中生活场景数据和插入事件数据的生成依据。它是 Markdown 源文件，扩展名为 `.md`，本质是纯文本；在 GitHub、VS Code 等工具中会被渲染成带标题、表格和列表的文档。

## 基础生成规则

- 默认生成 2 分钟数据，采样间隔 5 秒，共 24 组样本。
- 每组样本会拆成两条标准 MQTT 消息：
  - `elder/{elder_id}/sensor/vital`
  - `elder/{elder_id}/sensor/env`
- 每个场景使用 `progress = 当前序号 / 23` 表示 0 到 1 的进度。
- 平滑波动使用 `wave = sin(progress * pi) * amplitude`，模拟日常活动中的自然起伏。
- 场景基础数据默认保持在 guardian-core 正常范围内，异常只由插入事件主动制造。
- `smoke_ppm` 仍是底层 `SensorEnvSample` 字段，但当前页面不提供烟雾录入，也不提供烟雾异常插入事件。

## guardian-core 当前阈值

| 指标 | 正常范围 | 异常触发 |
| --- | --- | --- |
| 心率 `heart_rate` | 55-110 bpm | `<55` 或 `>110` 触发 P2；`<45` 或 `>130` 触发 P1 |
| 血氧 `spo2` | `>=92%` | `<92%` 触发 P1；`<88%` 触发 P0 |
| CO2 `co2_ppm` | `<1500 ppm` | `>=1500 ppm` 触发 P3 |
| 燃气 `gas_ppm` | `<100 ppm` | `>=100 ppm` 触发 P0 |
| 温度 `temperature` | 16-30°C | `<=16°C` 或 `>=30°C` 触发 P3 |

血压、体温、湿度当前只记录展示，不参与 guardian-core 规则分级。

## 生活场景基础数据

### 早上起床 `morning_getup`

生成依据：老人从躺卧到坐起、站立、缓慢走动，心率和血压逐步上升，卧室环境平稳。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `lying -> sitting -> standing -> walking` |
| 活动状态 | `waking_up -> slow_movement -> active` |
| 心率 | 约 66-83 bpm，随起床活动逐步上升 |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 122-130 / 76-80 mmHg |
| 体温 | 36.4-36.5°C |
| 房间 | `bedroom` |
| 温度 / 湿度 | 22.6-23.1°C / 51%-49% |
| CO2 | 820-940 ppm |
| 燃气 | 0 ppm |

### 中午午休 `midday_nap`

生成依据：午休时从坐姿进入躺卧，活动减少，心率和血压缓慢下降，卧室 CO2 随时间轻微上升。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `sitting -> lying` |
| 活动状态 | `preparing_nap -> static_rest` |
| 心率 | 约 62-72 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 126-121 / 78-75 mmHg |
| 体温 | 36.5-36.6°C |
| 房间 | `bedroom` |
| 温度 / 湿度 | 25.3-25.6°C / 48%-50% |
| CO2 | 760-940 ppm |
| 燃气 | 0 ppm |

### 晚上吃饭 `dinner`

生成依据：前半段在厨房准备晚餐，后半段在客厅/餐区吃饭；心率有轻微活动波动，厨房燃气基础读数低于告警阈值。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `standing -> sitting` |
| 活动状态 | `meal_preparation -> eating` |
| 心率 | 约 78-87 bpm |
| 血氧 | 前半段 96%，后半段 95% |
| 收缩压 / 舒张压 | 128-133 / 80-83 mmHg |
| 体温 | 36.6-36.7°C |
| 房间 | 前 35% 为 `kitchen`，之后为 `living_room` |
| 温度 / 湿度 | 26.0-26.7°C / 52%-55% |
| CO2 | 900-1120 ppm |
| 燃气 | 6-14 ppm |

### 夜间起夜 `night_bathroom`

生成依据：夜间短暂起床去卫生间，心率短时升高但不越界，房间从卧室切换到卫生间再回卧室。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `lying -> walking -> lying` |
| 活动状态 | `night_getup -> bathroom_visit -> back_to_sleep` |
| 心率 | 64-74 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 124-128 / 77-80 mmHg |
| 体温 | 36.5-36.6°C |
| 房间 | `bedroom -> bathroom -> bedroom` |
| 温度 / 湿度 | 23.0-23.4°C / 50%-55% |
| CO2 | 780-860 ppm |
| 燃气 | 0 ppm |

### 客厅看电视 `tv_evening`

生成依据：老人长时间坐在客厅，活动量低，生命体征平稳，CO2 随室内停留时间缓慢上升但低于通风阈值。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `sitting` |
| 活动状态 | `watching_tv` |
| 心率 | 74-77 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 126-129 / 78-80 mmHg |
| 体温 | 36.6-36.7°C |
| 房间 | `living_room` |
| 温度 / 湿度 | 24.8-25.1°C / 49%-51% |
| CO2 | 850-1110 ppm |
| 燃气 | 0 ppm |

### 厨房做饭 `cooking`

生成依据：做饭时老人站立活动，心率略高；厨房温度、CO2、燃气基础读数比普通场景略高，但默认仍低于规则阈值。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `standing -> sitting` |
| 活动状态 | `cooking -> rest_after_cooking` |
| 心率 | 80-87 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 130-134 / 82-85 mmHg |
| 体温 | 36.7-36.8°C |
| 房间 | `kitchen` |
| 温度 / 湿度 | 26.8-27.8°C / 53%-57% |
| CO2 | 930-1170 ppm |
| 燃气 | 12-30 ppm |

### 饭后散步 `after_meal_walk`

生成依据：饭后在室内缓慢走动，心率随活动自然上升，但保持在 110 bpm 以下。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `standing` |
| 活动状态 | `slow_walk` |
| 心率 | 82-100 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 128-134 / 80-83 mmHg |
| 体温 | 36.6-36.7°C |
| 房间 | `living_room` |
| 温度 / 湿度 | 24.5-24.9°C / 48%-50% |
| CO2 | 820-950 ppm |
| 燃气 | 0 ppm |

### 夜间睡眠 `sleep_night`

生成依据：夜间睡眠时长期躺卧，心率和血压缓慢下降，卧室 CO2 随时间轻微上升但低于通风阈值。

| 字段 | 具体生成数据 |
| --- | --- |
| 姿态 | `lying` |
| 活动状态 | `sleeping` |
| 心率 | 60-68 bpm |
| 血氧 | 固定 96% |
| 收缩压 / 舒张压 | 122-118 / 76-74 mmHg |
| 体温 | 36.3-36.4°C |
| 房间 | `bedroom` |
| 温度 / 湿度 | 22.8-23.0°C / 50%-52% |
| CO2 | 760-980 ppm |
| 燃气 | 0 ppm |

## 插入事件生成规则

插入事件通过 `event_intensity()` 生成平滑变化，而不是突然跳变。

- 默认触发时间为第 60 秒，可在网页滑块修改。
- 触发点前 20 秒开始变化。
- 若触发时间是 60 秒：
  - 0-35 秒：保持基础场景数据
  - 40 秒：异常强度 0
  - 45 秒：异常强度 0.25
  - 50 秒：异常强度 0.5
  - 55 秒：异常强度 0.75
  - 60 秒及以后：异常强度 1.0
- 数值计算方式：`当前值 + (目标异常值 - 当前值) * 异常强度`

## 插入事件具体数值

以下目标值是事件注入后的最终目标值，达到或超过 guardian-core 对应阈值后触发风险事件。

| 插入事件 | 变化依据 | 最终目标值 | 预期触发 |
| --- | --- | --- | --- |
| 正常状态 `normal` | 不注入异常，保持基础场景曲线 | 无额外变化 | P4 正常记录 |
| 血氧异常 `spo2_low` | 模拟血氧持续下降，同时心率因不适略升高 | `spo2 -> 86%`，`heart_rate -> 至少 92 bpm` | 血氧 `<88%`，触发 P0 |
| 心率异常 `heart_rate_abnormal` | 模拟明显心率升高，并带动血压升高 | `heart_rate -> 138 bpm`，`systolic_bp -> 145`，`diastolic_bp -> 88` | 心率 `>130 bpm`，触发 P1 |
| CO2 偏高 `co2_high` | 模拟室内通风不足，CO2 平滑升高 | `co2_ppm -> 1800 ppm`，湿度至少 55% | CO2 `>=1500 ppm`，触发 P3 |
| 燃气异常 `gas_leak` | 模拟厨房燃气浓度持续升高 | `room -> kitchen`，`gas_ppm -> 180 ppm`，`heart_rate -> 至少 92 bpm` | 燃气 `>=100 ppm`，触发 P0 |
| 室温过高 `temperature_high` | 模拟室温持续升高 | `temperature -> 31.0°C`，CO2 至少 1000 ppm | 温度 `>=30°C`，触发 P3 |
| 室温过低 `temperature_low` | 模拟室温持续下降 | `temperature -> 15.0°C` | 温度 `<=16°C`，触发 P3 |

## 发送后的标准协议

每个综合场景样本最终会转换成两个 Pydantic schema：

- `SensorVitalSample`
  - `heart_rate`
  - `spo2`
  - `systolic_bp`
  - `diastolic_bp`
  - `body_temperature`
  - `timestamp`
- `SensorEnvSample`
  - `room`
  - `temperature`
  - `humidity`
  - `co2_ppm`
  - `gas_ppm`
  - `smoke_ppm`
  - `timestamp`

场景语义字段，例如 `scene`、`activity`、`risk_hint`、`note` 只用于生成解释，不会发布给 guardian-core。guardian-core 只接收标准 MQTT payload。
