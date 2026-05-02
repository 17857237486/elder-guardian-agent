import json


SYSTEM_PROMPT = """你是居家老人健康守护与环境协同 Agent。
你只能基于输入上下文进行判断，不要编造不存在的数据。
规则引擎负责安全底线：P0 不能被降级，P1 不能降级到 P3/P4。
你不能直接生成底层 MQTT 指令，只能给出建议动作。
输出必须是 JSON，不要输出 Markdown。"""


def build_user_prompt(context: dict) -> str:
    context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
    return (
        "请基于以下紧凑上下文输出一个 AgentDecision JSON 对象：\n"
        f"{context_json}\n"
        "必须只输出 JSON，不要 Markdown。严格使用这些字段和类型："
        '{"risk_level":"P0|P1|P2|P3|P4","risk_score":0.0,"event_type":"string",'
        '"reasoning_summary":"string","recommended_actions":["string"],'
        '"need_elder_confirmation":false,"need_family_notification":false,'
        '"alert_priority":"P0|P1|P2|P3|P4","device_actions":[]}'
        "。alert_priority 必须是 P0/P1/P2/P3/P4，不要写高/中/低。device_actions 只能是建议，不要生成 MQTT 指令。"
    )
