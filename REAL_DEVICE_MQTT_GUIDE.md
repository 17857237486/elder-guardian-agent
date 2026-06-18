# 真实设备 MQTT 接入说明：只展示，不进入 AI

本文说明如何把真实温湿度设备接入 RK3588 上的 Elder Guardian 项目，并在 Dashboard 中展示数据。

## 1. 接入目标

真实设备展示链路只用于证明硬件数据链路正常：

```text
真实温湿度设备
→ MQTT Broker
→ Edge MCP 保存真实设备读数
→ Dashboard 显示
```

这条链路不会触发风险事件、不会调用本地 AI、不会调用云端复核，也不会控制空调、窗户等设备。

## 2. MQTT Broker 地址

RK3588 部署后，真实设备连接：

```text
host: 192.168.10.64
port: 1883
username: 无
password: 无
```

## 3. 展示专用 Topic

真实设备只展示时，必须发布到：

```text
elder/{elder_id}/device/{device_id}/telemetry
```

示例：

```text
elder/elder_001/device/dht22_living_room_01/telemetry
```

字段含义：

- `elder_001`：老人 ID。
- `dht22_living_room_01`：设备 ID，由你自己给真实设备命名。
- `telemetry`：设备遥测读数，表示“这个设备当前测到了什么”。

## 4. 推荐 Payload

```json
{
  "device_type": "temperature_humidity_sensor",
  "room": "living_room",
  "source": "real_device",
  "metrics": {
    "temperature": 24.6,
    "humidity": 51.2
  },
  "units": {
    "temperature": "°C",
    "humidity": "%"
  }
}
```

也兼容扁平格式：

```json
{
  "device_type": "temperature_humidity_sensor",
  "room": "living_room",
  "temperature": 24.6,
  "humidity": 51.2
}
```

系统会统一保存为 `metrics.temperature` 和 `metrics.humidity`。

## 5. mosquitto_pub 测试命令

在任意能访问 RK3588 的机器上执行：

```bash
mosquitto_pub -h 192.168.10.64 -p 1883 \
  -t elder/elder_001/device/dht22_living_room_01/telemetry \
  -m '{"device_type":"temperature_humidity_sensor","room":"living_room","source":"real_device","metrics":{"temperature":24.6,"humidity":51.2},"units":{"temperature":"°C","humidity":"%"}}'
```

然后打开：

```text
http://192.168.10.64:5173
```

Dashboard 的“真实设备数据”面板应显示该设备的温度、湿度和在线状态。

## 6. HTTP 调试接口

如果设备暂时不会 MQTT，或者你只是想调试，可以直接调用 Edge MCP HTTP 接口：

```bash
curl -X POST http://192.168.10.64:8010/api/v2/device-readings \
  -H "Content-Type: application/json" \
  -d '{"elder_id":"elder_001","device_id":"dht22_living_room_01","device_type":"temperature_humidity_sensor","room":"living_room","source":"real_device","metrics":{"temperature":24.6,"humidity":51.2},"units":{"temperature":"°C","humidity":"%"}}'
```

查看最新真实设备读数：

```bash
curl "http://192.168.10.64:8010/api/v2/device-readings/latest?elder_id=elder_001"
```

## 7. Python 设备端示例

```python
import json
import time

import paho.mqtt.client as mqtt

BROKER_HOST = "192.168.10.64"
BROKER_PORT = 1883
TOPIC = "elder/elder_001/device/dht22_living_room_01/telemetry"

client = mqtt.Client()
client.connect(BROKER_HOST, BROKER_PORT, 60)

while True:
    payload = {
        "device_type": "temperature_humidity_sensor",
        "room": "living_room",
        "source": "real_device",
        "metrics": {
            "temperature": 24.6,
            "humidity": 51.2,
        },
        "units": {
            "temperature": "°C",
            "humidity": "%",
        },
    }
    client.publish(TOPIC, json.dumps(payload, ensure_ascii=False), qos=1)
    time.sleep(5)
```

把示例中的 `temperature` 和 `humidity` 替换为真实传感器读数即可。

## 8. ESP32 / Arduino 接入思路

ESP32 或 Arduino 设备端逻辑保持一致：

```text
连接 Wi-Fi
→ 连接 MQTT 服务器 192.168.10.64:1883
→ 读取温湿度传感器
→ publish 到 elder/elder_001/device/{device_id}/telemetry
→ payload 使用 JSON
```

建议每 3 到 10 秒上报一次。Dashboard 中超过 30 秒没有新读数会显示离线。

## 9. 与 AI 风险链路的区别

只展示真实设备数据时使用：

```text
elder/{elder_id}/device/{device_id}/telemetry
```

这条链路只进入 Dashboard，不进入 AI。

正式风险判断数据使用：

```text
elder/{elder_id}/sensor/env
```

例如：

```json
{
  "elder_id": "elder_001",
  "room": "living_room",
  "temperature": 31.0,
  "humidity": 50.0,
  "co2_ppm": 900,
  "gas_ppm": 0,
  "smoke_ppm": 0
}
```

这条链路会进入规则引擎，可能触发 `temperature_high`、`temperature_low`、`humidity_abnormal` 等风险事件。

## 10. 验证和排查

检查 Edge MCP 是否收到数据：

```bash
curl "http://192.168.10.64:8010/api/v2/device-readings/latest?elder_id=elder_001"
```

检查 Dashboard 聚合状态：

```bash
curl "http://192.168.10.64:8010/api/v2/dashboard/state"
```

如果 Dashboard 没有显示：

1. 确认 topic 是 `elder/elder_001/device/{device_id}/telemetry`。
2. 确认 JSON 格式合法。
3. 确认设备能访问 `192.168.10.64:1883`。
4. 查看 RK3588 上 Edge MCP 日志：

```bash
ssh root@192.168.10.64 "cd /opt/elder-guardian-agent && docker compose -f docker-compose.images.yml logs --tail=100 edge-mcp-server"
```

如果你希望数据触发 AI 或设备联动，不要使用本说明书的 telemetry topic，而应使用正式 `sensor/env` 风险链路。
