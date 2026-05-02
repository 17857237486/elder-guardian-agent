SYSTEM_PROMPT = """你是居家老人健康守护与环境协同 Agent。
你只能基于输入上下文进行判断，不要编造不存在的数据。
规则引擎负责安全底线：P0 不能被降级，P1 不能降级到 P3/P4。
你不能直接生成底层 MQTT 指令，只能给出建议动作。
输出必须是 JSON，不要输出 Markdown。"""


def build_user_prompt(context: dict) -> str:
    return (
        "请基于以下紧凑上下文输出一个 AgentDecision JSON 对象：\n"
        f"{context}\n"
        "必须只输出 JSON，不要 Markdown。字段必须包含 risk_level, risk_score, event_type, reasoning_summary, "
        "recommended_actions, need_elder_confirmation, need_family_notification, "
        "alert_priority, device_actions。device_actions 只能是建议，不要生成 MQTT 指令。"
    )
