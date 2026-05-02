import json


SYSTEM_PROMPT = """你是居家老人健康守护与环境协同 Agent。
你只能基于输入上下文进行判断，不要编造不存在的数据。
规则引擎负责安全底线：P0 不能被降级，P1 不能降级到 P3/P4。
你不能直接生成底层 MQTT 指令，只能给出建议动作。
输出必须是 JSON，不要输出 Markdown。"""


def build_user_prompt(context: dict) -> str:
    rule = context.get("rule_result", {})
    risk_level = str(rule.get("risk_level", "P4"))
    risk_score = float(rule.get("risk_score", 0.0) or 0.0)
    event_type = str(rule.get("event_type", "normal"))
    summary = str(rule.get("summary", "未发现异常。"))
    template = {
        "risk_level": risk_level,
        "risk_score": risk_score,
        "event_type": event_type,
        "reasoning_summary": summary,
        "recommended_actions": _default_actions(risk_level),
        "need_elder_confirmation": risk_level in {"P1", "P2"},
        "need_family_notification": risk_level in {"P0", "P1"},
        "alert_priority": risk_level,
        "device_actions": [],
    }
    return (
        "你只输出一行合法 JSON，不输出 Markdown，不换行，不解释。"
        f"根据事件:risk_level={risk_level} risk_score={risk_score} event_type={event_type} summary={summary}。"
        "输出必须使用这个 schema 和已给默认值，可微调 reasoning_summary，但不要改变 risk_level 或 alert_priority："
        f"{json.dumps(template, ensure_ascii=False, separators=(',', ':'), default=str)}"
    )


def _default_actions(risk_level: str) -> list[str]:
    actions: dict[str, list[str]] = {
        "P0": ["立即告警", "通知家属", "执行安全联动", "记录事件"],
        "P1": ["本地询问老人", "同步通知家属", "持续观察"],
        "P2": ["本地询问老人", "超时升级通知家属"],
        "P3": ["自动调节环境", "记录事件"],
        "P4": ["记录正常状态"],
    }
    return actions.get(risk_level, actions["P4"])
