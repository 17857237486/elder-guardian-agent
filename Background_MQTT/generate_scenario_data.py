from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "guardian-shared"))

from guardian_shared.schemas import SensorEnvSample, SensorVitalSample
from guardian_shared.topics import elder_sensor_env, elder_sensor_vital
from guardian_shared.utils import model_to_json

SCENE_LABELS = {
    "morning_getup": "早上起床",
    "midday_nap": "中午午休",
    "dinner": "晚上吃饭",
    "night_bathroom": "夜间起夜",
    "tv_evening": "客厅看电视",
    "cooking": "厨房做饭",
    "away": "外出",
    "sleep_night": "夜间睡眠",
}

EVENT_LABELS = {
    "normal": "正常状态",
    "spo2_critical": "严重低血氧",
    "spo2_low": "低血氧",
    "heart_rate_abnormal": "心率异常",
    "heart_rate_baseline_anomaly": "P2 心率基线异常",
    "spo2_baseline_anomaly": "P2 血氧基线异常",
    "suspected_fall": "疑似跌倒",
    "long_static": "长时间静止",
    "co2_high": "CO2 偏高",
    "gas_leak": "燃气异常",
    "temperature_high": "室温过高",
    "temperature_low": "室温过低",
    "humidity_abnormal": "湿度异常",
}

ROOM_KEYS = ("bedroom", "kitchen", "living_room", "bathroom")


def iso_at(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def wave(index: int, total: int, amplitude: float = 1.0) -> float:
    if total <= 1:
        return 0.0
    return math.sin((index / (total - 1)) * math.pi) * amplitude


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def lerp(start: float, end: float, intensity: float) -> float:
    return start + (end - start) * clamp(intensity, 0.0, 1.0)


def event_intensity(time_offset_sec: int, trigger_second: int, *, lead_sec: int = 20) -> float:
    if time_offset_sec < trigger_second - lead_sec:
        return 0.0
    if time_offset_sec <= trigger_second:
        return (time_offset_sec - (trigger_second - lead_sec)) / max(1, lead_sec)
    return 1.0


def scene_base_time(scene: str, base_time: datetime | None = None) -> datetime:
    if base_time is not None:
        return base_time
    today = datetime.now(timezone.utc).date()
    hour_map = {
        "morning_getup": 6,
        "midday_nap": 12,
        "dinner": 18,
        "away": 10,
    }
    minute_map = {
        "morning_getup": 30,
        "midday_nap": 40,
        "dinner": 30,
        "away": 0,
    }
    return datetime(
        today.year,
        today.month,
        today.day,
        hour_map[scene],
        minute_map[scene],
        tzinfo=timezone.utc,
    )


def scenario_sample(
    scene: str,
    index: int,
    total: int,
    interval_sec: int,
    elder_id: str,
    run_id: str,
    base_time: datetime,
) -> dict[str, Any]:
    progress = index / max(1, total - 1)
    time_offset_sec = index * interval_sec
    timestamp = iso_at(scene_base_time(scene, base_time), time_offset_sec)

    if scene == "morning_getup":
        posture = "lying" if progress < 0.25 else "sitting" if progress < 0.45 else "standing" if progress < 0.7 else "walking"
        motion_state = "waking_up" if progress < 0.3 else "slow_movement" if progress < 0.75 else "active"
        heart_rate = round(66 + progress * 17 + wave(index, total, 2))
        spo2 = 96
        systolic_bp = round(122 + progress * 8)
        diastolic_bp = round(76 + progress * 4)
        body_temperature = round(36.4 + wave(index, total, 0.1), 1)
        env = {
            "room": "bedroom",
            "temperature": round(22.6 + progress * 0.5, 1),
            "humidity": round(51 - progress * 2, 1),
            "co2_ppm": round(820 + progress * 120),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "起床阶段，心率逐步上升，属于正常活动变化。"
    elif scene == "midday_nap":
        posture = "sitting" if progress < 0.15 else "lying"
        motion_state = "preparing_nap" if progress < 0.2 else "static_rest"
        heart_rate = round(72 - progress * 10 + wave(index, total, 1))
        spo2 = 96
        systolic_bp = round(126 - progress * 5)
        diastolic_bp = round(78 - progress * 3)
        body_temperature = round(36.6 - wave(index, total, 0.1), 1)
        env = {
            "room": "bedroom",
            "temperature": round(25.3 + wave(index, total, 0.3), 1),
            "humidity": round(48 + progress * 2, 1),
            "co2_ppm": round(760 + progress * 180),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "午休阶段，长时间静止但生命体征平稳。"
    elif scene == "dinner":
        posture = "standing" if progress < 0.35 else "sitting"
        motion_state = "meal_preparation" if progress < 0.35 else "eating"
        heart_rate = round(78 + wave(index, total, 8) + progress * 3)
        spo2 = 95 if progress > 0.6 else 96
        systolic_bp = round(128 + wave(index, total, 5))
        diastolic_bp = round(80 + wave(index, total, 3))
        body_temperature = round(36.6 + wave(index, total, 0.1), 1)
        env = {
            "room": "kitchen" if progress < 0.35 else "living_room",
            "temperature": round(26.0 + wave(index, total, 0.7), 1),
            "humidity": round(52 + wave(index, total, 3), 1),
            "co2_ppm": round(900 + progress * 220),
            "gas_ppm": round(6 + wave(index, total, 8)),
            "smoke_ppm": 0,
        }
        note = "晚餐阶段，厨房燃气读数低于告警阈值，属于安全范围。"
    elif scene == "night_bathroom":
        posture = "lying" if progress < 0.2 else "walking" if progress < 0.75 else "lying"
        motion_state = "night_getup" if progress < 0.3 else "bathroom_visit" if progress < 0.75 else "back_to_sleep"
        heart_rate = round(64 + wave(index, total, 10))
        spo2 = 96
        systolic_bp = round(124 + wave(index, total, 4))
        diastolic_bp = round(77 + wave(index, total, 3))
        body_temperature = round(36.5 + wave(index, total, 0.1), 1)
        env = {
            "room": "bedroom" if progress < 0.25 or progress > 0.78 else "bathroom",
            "temperature": round(23.0 + wave(index, total, 0.4), 1),
            "humidity": round(50 + wave(index, total, 5), 1),
            "co2_ppm": round(780 + progress * 80),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "夜间起夜阶段，心率短暂上升但仍在正常范围，环境数据保持安全。"
    elif scene == "tv_evening":
        posture = "sitting"
        motion_state = "watching_tv"
        heart_rate = round(74 + wave(index, total, 3))
        spo2 = 96
        systolic_bp = round(126 + wave(index, total, 3))
        diastolic_bp = round(78 + wave(index, total, 2))
        body_temperature = round(36.6 + wave(index, total, 0.1), 1)
        env = {
            "room": "living_room",
            "temperature": round(24.8 + wave(index, total, 0.3), 1),
            "humidity": round(49 + wave(index, total, 2), 1),
            "co2_ppm": round(850 + progress * 260),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "客厅看电视阶段，活动量低但生命体征平稳，CO2 轻微上升但未到通风阈值。"
    elif scene == "cooking":
        posture = "standing" if progress < 0.8 else "sitting"
        motion_state = "cooking" if progress < 0.8 else "rest_after_cooking"
        heart_rate = round(80 + wave(index, total, 7))
        spo2 = 96
        systolic_bp = round(130 + wave(index, total, 4))
        diastolic_bp = round(82 + wave(index, total, 3))
        body_temperature = round(36.7 + wave(index, total, 0.1), 1)
        env = {
            "room": "kitchen",
            "temperature": round(26.8 + wave(index, total, 1.0), 1),
            "humidity": round(53 + wave(index, total, 4), 1),
            "co2_ppm": round(930 + progress * 240),
            "gas_ppm": round(12 + wave(index, total, 18)),
            "smoke_ppm": round(wave(index, total, 8)),
        }
        note = "厨房做饭阶段，燃气和烟雾读数略有波动但低于告警阈值。"
    elif scene == "away":
        posture = "away"
        motion_state = "out_of_home"
        heart_rate = round(76 + wave(index, total, 2))
        spo2 = 96
        systolic_bp = round(126 + wave(index, total, 2))
        diastolic_bp = round(78 + wave(index, total, 2))
        body_temperature = round(36.5 + wave(index, total, 0.1), 1)
        env = {
            "room": "outside",
            "presence": "away",
            "temperature": round(24.0 + wave(index, total, 0.4), 1),
            "humidity": round(50 + wave(index, total, 2), 1),
            "co2_ppm": round(760 + progress * 90),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "外出阶段，老人不在家；非燃气环境异常只记录，不联动家具。"
    elif scene == "sleep_night":
        posture = "lying"
        motion_state = "sleeping"
        heart_rate = round(68 - progress * 8 + wave(index, total, 1))
        spo2 = 96
        systolic_bp = round(122 - progress * 4)
        diastolic_bp = round(76 - progress * 2)
        body_temperature = round(36.4 - wave(index, total, 0.1), 1)
        env = {
            "room": "bedroom",
            "temperature": round(22.8 + wave(index, total, 0.2), 1),
            "humidity": round(50 + progress * 2, 1),
            "co2_ppm": round(760 + progress * 220),
            "gas_ppm": 0,
            "smoke_ppm": 0,
        }
        note = "夜间睡眠阶段，心率降低但保持正常，环境数据平稳。"
    else:
        raise ValueError(f"unknown scene: {scene}")

    env.setdefault("occupant_room", env.get("room"))
    env.setdefault("life_scene", scene)

    return {
        "sample_id": f"{run_id}_{index + 1:03d}",
        "elder_id": elder_id,
        "scene": scene,
        "scene_label": SCENE_LABELS[scene],
        "sequence": index + 1,
        "total": total,
        "time_offset_sec": time_offset_sec,
        "injected_event": "normal",
        "timestamp": timestamp,
        "vital": {
            "heart_rate": heart_rate,
            "spo2": spo2,
            "systolic_bp": systolic_bp,
            "diastolic_bp": diastolic_bp,
            "body_temperature": body_temperature,
        },
        "environment": env,
        "activity": {
            "posture": posture,
            "motion_state": motion_state,
        },
        "risk_hint": classify_hint(scene, heart_rate, spo2, env, motion_state),
        "note": note,
    }


def classify_hint(scene: str, heart_rate: int, spo2: int, env: dict[str, Any], motion_state: str) -> dict[str, str]:
    if env["gas_ppm"] >= 100:
        return {"level": "P0", "reason": "燃气浓度达到紧急阈值。"}
    if spo2 < 88:
        return {"level": "P0", "reason": "血氧低于 88%，属于紧急风险。"}
    if spo2 < 92:
        return {"level": "P1", "reason": "血氧明显偏低。"}
    if heart_rate < 45 or heart_rate > 130:
        return {"level": "P1", "reason": "心率明显异常。"}
    if env["co2_ppm"] >= 1500:
        return {"level": "P3", "reason": "CO2 偏高，建议通风。"}
    if env["temperature"] >= 30:
        return {"level": "P3", "reason": "室温偏高，建议降温。"}
    if env["temperature"] <= 16:
        return {"level": "P3", "reason": "室温偏低，建议升温。"}
    if scene == "midday_nap" and motion_state == "static_rest":
        return {"level": "P4", "reason": "午休静止符合场景预期。"}
    return {"level": "P4", "reason": "场景数据处于正常范围。"}


def build_samples(scene: str, duration_sec: int, interval_sec: int, elder_id: str) -> list[dict[str, Any]]:
    total = duration_sec // interval_sec
    if total <= 0:
        raise ValueError("duration_sec must be >= interval_sec")
    run_id = f"{scene}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:6]}"
    base_time = datetime.now(timezone.utc)
    return [scenario_sample(scene, index, total, interval_sec, elder_id, run_id, base_time) for index in range(total)]


def inject_event(
    samples: list[dict[str, Any]],
    event_type: str,
    trigger_second: int,
    event_room: str = "living_room",
) -> list[dict[str, Any]]:
    if event_type == "normal":
        return samples
    if event_type not in EVENT_LABELS:
        raise ValueError(f"unknown event_type: {event_type}")
    if event_room not in ROOM_KEYS:
        raise ValueError(f"unknown event_room: {event_room}")

    for sample in samples:
        time_offset_sec = int(sample.get("time_offset_sec", 0))
        intensity = event_intensity(time_offset_sec, trigger_second)
        vital = sample["vital"]
        env = sample["environment"]

        if intensity <= 0:
            sample["injected_event"] = "normal"
            continue

        sample["injected_event"] = event_type
        env.setdefault("life_scene", sample["scene"])
        env["room"] = event_room
        env["occupant_room"] = event_room
        if event_type == "spo2_critical":
            vital["spo2"] = round(lerp(vital["spo2"], 86, intensity))
            vital["heart_rate"] = round(lerp(vital["heart_rate"], max(vital["heart_rate"], 92), intensity))
        elif event_type == "spo2_low":
            vital["spo2"] = round(lerp(vital["spo2"], 90, intensity))
        elif event_type == "heart_rate_abnormal":
            vital["heart_rate"] = round(lerp(vital["heart_rate"], 138, intensity))
            vital["systolic_bp"] = round(lerp(vital["systolic_bp"], 145, intensity))
            vital["diastolic_bp"] = round(lerp(vital["diastolic_bp"], 88, intensity))
        elif event_type == "heart_rate_baseline_anomaly":
            vital["heart_rate"] = round(lerp(vital["heart_rate"], 115, intensity))
            vital["systolic_bp"] = round(lerp(vital["systolic_bp"], 136, intensity))
            vital["diastolic_bp"] = round(lerp(vital["diastolic_bp"], 84, intensity))
        elif event_type == "spo2_baseline_anomaly":
            vital["spo2"] = round(lerp(vital["spo2"], 94, intensity))
            vital["heart_rate"] = round(lerp(vital["heart_rate"], max(vital["heart_rate"], 86), intensity))
        elif event_type == "co2_high":
            env["co2_ppm"] = round(lerp(env["co2_ppm"], 1800, intensity))
            env["temperature"] = round(lerp(env["temperature"], max(env["temperature"], 26.0), intensity), 1)
            env["humidity"] = round(lerp(env["humidity"], max(env["humidity"], 55.0), intensity), 1)
        elif event_type == "gas_leak":
            env["gas_ppm"] = round(lerp(env["gas_ppm"], 180, intensity))
            vital["heart_rate"] = round(lerp(vital["heart_rate"], max(vital["heart_rate"], 92), intensity))
        elif event_type == "temperature_high":
            env["temperature"] = round(lerp(env["temperature"], 31.0, intensity), 1)
            env["co2_ppm"] = round(lerp(env["co2_ppm"], max(env["co2_ppm"], 1000), intensity))
        elif event_type == "temperature_low":
            env["temperature"] = round(lerp(env["temperature"], 15.0, intensity), 1)
        elif event_type == "humidity_abnormal":
            env["humidity"] = round(lerp(env["humidity"], 82.0, intensity), 1)

        sample["risk_hint"] = classify_hint(
            sample["scene"],
            vital["heart_rate"],
            vital["spo2"],
            env,
            sample["activity"]["motion_state"],
        )
        if event_type == "heart_rate_baseline_anomaly":
            sample["risk_hint"] = {"level": "P2", "reason": "心率持续高于个人 p90，进入 Candidate 本地复核。"}
        if event_type == "spo2_baseline_anomaly":
            sample["risk_hint"] = {"level": "P2", "reason": "血氧持续低于个人 p10，进入 Candidate 本地复核。"}
        sample["note"] = f"{sample['scene_label']} 场景中在第 {trigger_second} 秒注入 {EVENT_LABELS[event_type]} 事件。"
    return samples


def build_event_samples(
    scene: str,
    event_type: str,
    trigger_second: int,
    duration_sec: int,
    interval_sec: int,
    elder_id: str,
    event_room: str = "living_room",
) -> list[dict[str, Any]]:
    samples = build_samples(scene, duration_sec, interval_sec, elder_id)
    return inject_event(samples, event_type, trigger_second, event_room)


def to_standard_samples(sample: dict[str, Any]) -> tuple[SensorVitalSample, SensorEnvSample]:
    timestamp = parse_iso_datetime(sample["timestamp"])
    vital = sample["vital"]
    env = sample["environment"]
    vital_sample = SensorVitalSample(
        sample_id=f"vital_{sample['sample_id']}",
        elder_id=sample["elder_id"],
        heart_rate=vital["heart_rate"],
        spo2=vital["spo2"],
        systolic_bp=vital["systolic_bp"],
        diastolic_bp=vital["diastolic_bp"],
        body_temperature=vital["body_temperature"],
        timestamp=timestamp,
    )
    env_sample = SensorEnvSample(
        sample_id=f"env_{sample['sample_id']}",
        elder_id=sample["elder_id"],
        room=env["room"],
        temperature=env["temperature"],
        humidity=env["humidity"],
        co2_ppm=env["co2_ppm"],
        gas_ppm=env["gas_ppm"],
        smoke_ppm=env.get("smoke_ppm", 0),
        timestamp=timestamp,
    )
    return vital_sample, env_sample


def publish_json(client: mqtt.Client, topic: str, body: str) -> None:
    result = client.publish(topic, body, qos=1)
    result.wait_for_publish()


def publish_standard_sample(client: mqtt.Client, sample: dict[str, Any]) -> int:
    vital_sample, env_sample = to_standard_samples(sample)
    vital_topic = elder_sensor_vital(sample["elder_id"])
    env_topic = elder_sensor_env(sample["elder_id"])

    publish_json(client, vital_topic, model_to_json(vital_sample))
    print(f"publish {vital_topic}: {vital_sample.sample_id}")
    publish_json(client, env_topic, model_to_json(env_sample))
    print(f"publish {env_topic}: {env_sample.sample_id}")

    return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate elder home scenario samples and publish guardian-core compatible MQTT sensor samples."
    )
    parser.add_argument("--scene", choices=SCENE_LABELS.keys(), default="morning_getup")
    parser.add_argument("--event-type", choices=EVENT_LABELS.keys(), default="normal")
    parser.add_argument("--trigger-second", type=int, default=60)
    parser.add_argument("--event-room", choices=ROOM_KEYS, default="living_room")
    parser.add_argument("--duration-sec", type=int, default=120)
    parser.add_argument("--interval-sec", type=int, default=5)
    parser.add_argument("--elder-id", default="elder_001")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Sleep interval-sec between scenario samples. Default publishes all samples immediately.",
    )
    args = parser.parse_args()

    samples = build_event_samples(
        args.scene,
        args.event_type,
        args.trigger_second,
        args.duration_sec,
        args.interval_sec,
        args.elder_id,
        args.event_room,
    )

    client = mqtt.Client(client_id=f"background-scenario-generator-{int(time.time())}")
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    print(
        f"connected mqtt://{args.host}:{args.port}; "
        f"samples={len(samples)}; standard_messages={len(samples) * 2}; "
        f"scene={args.scene}; event_type={args.event_type}; "
        f"event_room={args.event_room}; trigger_second={args.trigger_second}"
    )
    published_count = 0
    try:
        for sample in samples:
            published_count += publish_standard_sample(client, sample)
            if args.realtime:
                time.sleep(args.interval_sec)
    finally:
        client.loop_stop()
        client.disconnect()
    print(f"done; published_messages={published_count}")


if __name__ == "__main__":
    main()
