# RK3588 部署与数据接入简明说明

本文用于演示交付和现场复现，只保留最常用的部署与数据输入方法。

## 1. 如何部署到 RK3588

在开发电脑的项目根目录执行：

```powershell
wsl bash -lc "cd /mnt/d/3_Works/2026.6/elderly_Project_4 && REMOTE_HOST=192.168.10.64 REMOTE_USER=root ./scripts/deploy_rk3588.sh"
```

部署脚本会在 RK3588 上拉取 GitHub Actions 构建好的 `latest` 镜像，并重启 Compose 服务。

部署后检查：

```bash
ssh root@192.168.10.64
cd /opt/elder-guardian-agent
docker compose -f docker-compose.images.yml ps
```

常用页面：

- MQTT 传感器数据记录：http://192.168.10.64:8090
- Dashboard：http://192.168.10.64:5173
- 老人 HMI：http://192.168.10.64:5174
- Edge MCP API：http://192.168.10.64:8010/health
- Orchestrator API：http://192.168.10.64:8020/health
- Vision Service：http://192.168.10.64:8101/health

## 2. 温湿度等真实设备如何传到 MQTT

真实传感器推荐发布到：

```text
elder/{elder_id}/sensor/env
```

整屋环境快照示例：

```json
{
  "schema": "home_environment_snapshot_v1",
  "elder_id": "elder_001",
  "rooms": {
    "bedroom": {"temperature": 24.0, "humidity": 50, "co2_ppm": 820, "gas_ppm": 0, "smoke_ppm": 0, "presence": false},
    "bathroom": {"temperature": 24.0, "humidity": 58, "co2_ppm": 780, "gas_ppm": 0, "smoke_ppm": 0, "presence": false},
    "living_room": {"temperature": 24.5, "humidity": 49, "co2_ppm": 850, "gas_ppm": 0, "smoke_ppm": 0, "presence": true},
    "kitchen": {"temperature": 25.0, "humidity": 52, "co2_ppm": 880, "gas_ppm": 0, "smoke_ppm": 0, "presence": false}
  }
}
```

生命体征推荐发布到：

```text
elder/{elder_id}/sensor/vital
```

示例：

```json
{
  "elder_id": "elder_001",
  "heart_rate": 78,
  "spo2": 96,
  "systolic_bp": 128,
  "diastolic_bp": 80,
  "body_temperature": 36.6
}
```

传感器只负责上报数据，设备控制由系统策略生成，不建议传感器端直接下发控制命令。

## 3. 摄像头图片如何传入后端

当前演示链路使用 Vision Service 的“五张图片池”：

1. 在 8090 页面打开“真实摄像头拍照验证”。
2. 可以点击“拍一张照片”，由 Vision Service 调真实摄像头 snapshot URL 保存一张。
3. 也可以点击“选择五张图片”，从本机选择五张图片上传到 RK3588。
4. 上传后这五张图片作为最近五张待使用图片。
5. 触发“疑似跌倒”或“长时间静止”时，系统用这五张生成视觉事件。

模型使用方式：

- RK3588 本地模型只分析中间三张，也就是第 2、3、4 张。
- 云端复核使用五张原图，并结合最近生命体征、环境和个人基线摘要。
- 已绑定到风险事件的图片不会被“一键清除已拍摄图片”删除；清除只影响待使用图片池。

## 4. MQTT 传感器数据记录网页怎么用

页面地址：

```text
http://192.168.10.64:8090
```

主要功能：

- 手动录入生命体征、环境数据和设备状态。
- 通过“风险事件时间轴”快速触发 P0-P4 演示事件。
- 设置个人基线，包括心率、血氧和卫生间停留基线。
- 自动生成心率/血氧基线、卫生间停留基线和 30 日健康摘要。
- 上传五张图片到视觉服务，用于疑似跌倒或长时间静止复核。
- 查看 MQTT 回流记录，确认数据是否进入系统。
- 查看或同步设备状态，辅助确认 Dashboard 的设备开关是否生效。

演示建议：

1. 先点击清空显示，避免历史事件干扰。
2. 设置心率、血氧、卫生间停留基线。
3. 使用风险事件时间轴触发目标事件。
4. 在 Dashboard 观察规则判断、本地模型、云端复核、设备策略、HMI/家属告警。
5. 在老人 HMI 点击“我没事 / 需要帮助 / 联系家属”，验证闭环。
