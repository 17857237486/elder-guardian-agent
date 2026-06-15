from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from guardian_shared.v2 import (
    ActionExecutionV2,
    AlertRecordV2,
    HmiPromptV2,
    HmiResponseV2,
    NormalizedEventV2,
    RawObservationV2,
    ToolCallV2,
    WorkflowStepV2,
    WorkflowV2,
)

from app import models


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, default=str)
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def row_to_dict(row: Any) -> dict[str, Any]:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    for key in [
        "payload_json",
        "trigger_observation_ids_json",
        "rule_trace_json",
        "evidence_json",
        "image_refs_json",
        "input_json",
        "output_json",
        "arguments_json",
        "result_json",
        "command_json",
        "options_json",
    ]:
        if key in data:
            base = key.replace("_json", "")
            default: Any = [] if key in {"trigger_observation_ids_json", "options_json", "evidence_json", "image_refs_json"} else {}
            data[base] = _loads(data.pop(key), default)
    return data


def create_observation(db: Session, observation: RawObservationV2) -> dict[str, Any]:
    obj = models.RawObservationModel(
        observation_id=observation.observation_id,
        elder_id=observation.elder_id,
        kind=str(observation.kind),
        source=observation.source,
        topic=observation.topic,
        payload_json=_json(observation.payload),
        observed_at=observation.observed_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_observations(db: Session, elder_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        db.query(models.RawObservationModel)
        .filter(models.RawObservationModel.elder_id == elder_id)
        .order_by(desc(models.RawObservationModel.observed_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def recent_observations(db: Session, elder_id: str, limit: int = 30) -> list[dict[str, Any]]:
    return list_observations(db, elder_id=elder_id, limit=limit)


def create_event(db: Session, event: NormalizedEventV2) -> dict[str, Any]:
    obj = models.NormalizedEventModel(
        event_id=event.event_id,
        elder_id=event.elder_id,
        event_type=str(event.event_type),
        risk_level=str(event.risk_level),
        risk_score=event.risk_score,
        state=str(event.state),
        room=event.room,
        summary=event.summary,
        trigger_observation_ids_json=_json(event.trigger_observation_ids),
        rule_trace_json=_json(event.rule_trace),
        source_kind=str(event.source_kind) if event.source_kind else None,
        evidence_json=_json(event.evidence),
        frame_set_id=event.frame_set_id,
        image_refs_json=_json(event.image_refs),
        rule_risk_level=str(event.rule_risk_level or event.risk_level),
        local_risk_level=str(event.local_risk_level or event.risk_level),
        cloud_risk_level=str(event.cloud_risk_level) if event.cloud_risk_level else None,
        final_risk_level=str(event.final_risk_level or event.risk_level),
        decision_source=event.decision_source,
        confidence=event.confidence,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def update_event_state(
    db: Session,
    event_id: str,
    *,
    state: str,
    risk_level: str | None = None,
    summary: str | None = None,
) -> dict[str, Any] | None:
    obj = db.query(models.NormalizedEventModel).filter(models.NormalizedEventModel.event_id == event_id).first()
    if obj is None:
        return None
    obj.state = state
    if risk_level is not None:
        obj.risk_level = risk_level
    if summary is not None:
        obj.summary = summary
    obj.updated_at = datetime.now()
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def get_event(db: Session, event_id: str) -> dict[str, Any] | None:
    row = db.query(models.NormalizedEventModel).filter(models.NormalizedEventModel.event_id == event_id).first()
    return row_to_dict(row) if row else None


def update_event_analysis(db: Session, event_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    obj = db.query(models.NormalizedEventModel).filter(models.NormalizedEventModel.event_id == event_id).first()
    if obj is None:
        return None
    scalar_fields = [
        "local_risk_level",
        "local_semantics",
        "cloud_risk_level",
        "final_risk_level",
        "decision_source",
        "confidence",
        "frame_set_id",
        "source_kind",
    ]
    for field in scalar_fields:
        if field in payload:
            setattr(obj, field, payload[field])
    if "image_refs" in payload:
        obj.image_refs_json = _json(payload["image_refs"])
    if "evidence" in payload:
        obj.evidence_json = _json(payload["evidence"])
    if "summary" in payload:
        obj.summary = str(payload["summary"])
    if "final_risk_level" in payload:
        obj.risk_level = str(payload["final_risk_level"])
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_events(db: Session, elder_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        db.query(models.NormalizedEventModel)
        .filter(models.NormalizedEventModel.elder_id == elder_id)
        .order_by(desc(models.NormalizedEventModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_workflow(db: Session, workflow: WorkflowV2) -> dict[str, Any]:
    obj = models.WorkflowModel(
        workflow_id=workflow.workflow_id,
        event_id=workflow.event_id,
        elder_id=workflow.elder_id,
        status=str(workflow.status),
        current_step=workflow.current_step,
        model=workflow.model,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def update_workflow_status(db: Session, workflow_id: str, *, status: str, current_step: str | None = None) -> dict[str, Any] | None:
    obj = db.query(models.WorkflowModel).filter(models.WorkflowModel.workflow_id == workflow_id).first()
    if obj is None:
        return None
    obj.status = status
    if current_step is not None:
        obj.current_step = current_step
    obj.updated_at = datetime.now()
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_workflows(db: Session, elder_id: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = (
        db.query(models.WorkflowModel)
        .filter(models.WorkflowModel.elder_id == elder_id)
        .order_by(desc(models.WorkflowModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_workflow_step(db: Session, step: WorkflowStepV2) -> dict[str, Any]:
    obj = models.WorkflowStepModel(
        step_id=step.step_id,
        workflow_id=step.workflow_id,
        event_id=step.event_id,
        elder_id=step.elder_id,
        step_name=step.step_name,
        status=str(step.status),
        model=step.model,
        input_json=_json(step.input),
        output_json=_json(step.output),
        error=step.error,
        created_at=step.created_at,
        completed_at=step.completed_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_workflow_steps(db: Session, elder_id: str, limit: int = 80) -> list[dict[str, Any]]:
    rows = (
        db.query(models.WorkflowStepModel)
        .filter(models.WorkflowStepModel.elder_id == elder_id)
        .order_by(desc(models.WorkflowStepModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_tool_call(db: Session, call: ToolCallV2) -> dict[str, Any]:
    obj = models.ToolCallModel(
        call_id=call.call_id,
        workflow_id=call.workflow_id,
        event_id=call.event_id,
        elder_id=call.elder_id,
        tool_name=call.tool_name,
        arguments_json=_json(call.arguments),
        status=str(call.status),
        result_json=_json(call.result),
        reason=call.reason,
        created_at=call.created_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_tool_calls(db: Session, elder_id: str, limit: int = 80) -> list[dict[str, Any]]:
    rows = (
        db.query(models.ToolCallModel)
        .filter(models.ToolCallModel.elder_id == elder_id)
        .order_by(desc(models.ToolCallModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_action_execution(db: Session, execution: ActionExecutionV2) -> dict[str, Any]:
    obj = models.ActionExecutionModel(
        execution_id=execution.execution_id,
        request_id=execution.request_id,
        event_id=execution.event_id,
        elder_id=execution.elder_id,
        command_json=_json(execution.command),
        status=str(execution.status),
        reason=execution.reason,
        mqtt_topic=execution.mqtt_topic,
        created_at=execution.created_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_action_executions(db: Session, elder_id: str, limit: int = 80) -> list[dict[str, Any]]:
    rows = (
        db.query(models.ActionExecutionModel)
        .filter(models.ActionExecutionModel.elder_id == elder_id)
        .order_by(desc(models.ActionExecutionModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_alert(db: Session, alert: AlertRecordV2) -> dict[str, Any]:
    obj = models.AlertRecordModel(
        alert_id=alert.alert_id,
        workflow_id=alert.workflow_id,
        event_id=alert.event_id,
        elder_id=alert.elder_id,
        alert_level=str(alert.alert_level),
        channel=alert.channel,
        message=alert.message,
        status=alert.status,
        created_at=alert.created_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_alerts(db: Session, elder_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        db.query(models.AlertRecordModel)
        .filter(models.AlertRecordModel.elder_id == elder_id)
        .order_by(desc(models.AlertRecordModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def create_hmi_prompt(db: Session, prompt: HmiPromptV2) -> dict[str, Any]:
    obj = models.HmiPromptModel(
        prompt_id=prompt.prompt_id,
        workflow_id=prompt.workflow_id,
        event_id=prompt.event_id,
        elder_id=prompt.elder_id,
        risk_level=str(prompt.risk_level),
        event_type=prompt.event_type,
        message=prompt.message,
        options_json=_json(prompt.options),
        status=prompt.status,
        timeout_sec=prompt.timeout_sec,
        created_at=prompt.created_at,
        responded_at=prompt.responded_at,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def latest_waiting_hmi_prompt(db: Session, elder_id: str) -> dict[str, Any] | None:
    row = (
        db.query(models.HmiPromptModel)
        .filter(models.HmiPromptModel.elder_id == elder_id, models.HmiPromptModel.status == "waiting")
        .order_by(desc(models.HmiPromptModel.created_at))
        .first()
    )
    return row_to_dict(row) if row else None


def list_hmi_prompts(db: Session, elder_id: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = (
        db.query(models.HmiPromptModel)
        .filter(models.HmiPromptModel.elder_id == elder_id)
        .order_by(desc(models.HmiPromptModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def respond_hmi_prompt(db: Session, response: HmiResponseV2) -> dict[str, Any] | None:
    obj = (
        db.query(models.HmiPromptModel)
        .filter(models.HmiPromptModel.prompt_id == response.prompt_id, models.HmiPromptModel.status == "waiting")
        .first()
    )
    if obj is None:
        return None
    safe = response.response_type in {"safe", "我没事"}
    response_record = models.HmiResponseModel(
        prompt_id=response.prompt_id,
        event_id=response.event_id,
        elder_id=response.elder_id,
        response_type=response.response_type,
        response_text=response.response_text,
        outcome="resolved" if safe else "family_alert",
        created_at=response.created_at,
    )
    db.add(response_record)
    obj.status = "responded"
    obj.responded_at = response.created_at
    db.commit()
    db.refresh(obj)
    return row_to_dict(obj)


def list_hmi_responses(db: Session, elder_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = (
        db.query(models.HmiResponseModel)
        .filter(models.HmiResponseModel.elder_id == elder_id)
        .order_by(desc(models.HmiResponseModel.created_at))
        .limit(limit)
        .all()
    )
    return [row_to_dict(row) for row in rows]


def dashboard_state(db: Session, elder_id: str) -> dict[str, Any]:
    events = list_events(db, elder_id=elder_id, limit=30)
    observations = recent_observations(db, elder_id=elder_id, limit=20)
    latest = events[0] if events else None
    return {
        "elder_id": elder_id,
        "current_risk_level": latest["risk_level"] if latest else "P4",
        "events": events,
        "observations": observations,
        "workflows": list_workflows(db, elder_id=elder_id),
        "workflow_steps": list_workflow_steps(db, elder_id=elder_id),
        "tool_calls": list_tool_calls(db, elder_id=elder_id),
        "action_executions": list_action_executions(db, elder_id=elder_id),
        "hmi_prompts": list_hmi_prompts(db, elder_id=elder_id),
        "hmi_responses": list_hmi_responses(db, elder_id=elder_id, limit=10),
        "current_hmi_prompt": latest_waiting_hmi_prompt(db, elder_id=elder_id),
        "alerts": list_alerts(db, elder_id=elder_id),
    }
