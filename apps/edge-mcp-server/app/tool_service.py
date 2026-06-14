from __future__ import annotations

from typing import Any

from guardian_shared.enums import EventType, RiskLevel
from guardian_shared.schemas import new_id
from guardian_shared.utils import model_to_dict
from guardian_shared.v2 import (
    ActionExecutionV2,
    ActionRequestV2,
    AlertRecordV2,
    AlertRequestV2,
    HmiPromptV2,
    HmiResponseV2,
    NormalizedEventV2,
    ToolCallStatus,
    ToolCallV2,
    WorkflowNoteV2,
    WorkflowStepV2,
    WorkflowV2,
)

from app.database import SessionLocal
from app import repository
from app.mqtt_bridge import MqttBridge
from app.policy import DevicePolicy


class EdgeToolService:
    def __init__(self, mqtt_bridge: MqttBridge) -> None:
        self.mqtt_bridge = mqtt_bridge
        self.device_policy = DevicePolicy()

    def get_current_event(self, event_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            event = repository.get_event(db, event_id)
            self._record_tool_call(db, "get_current_event", {"event_id": event_id}, ToolCallStatus.ACCEPTED, event or {})
        return event or {"event_id": event_id, "status": "not_found"}

    def get_recent_sensor_context(self, elder_id: str, limit: int = 30) -> dict[str, Any]:
        with SessionLocal() as db:
            observations = repository.recent_observations(db, elder_id=elder_id, limit=limit)
            result = {"elder_id": elder_id, "observations": observations}
            self._record_tool_call(db, "get_recent_sensor_context", {"elder_id": elder_id, "limit": limit}, ToolCallStatus.ACCEPTED, result)
        return result

    def get_home_device_snapshot(self, elder_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            observations = [
                item for item in repository.recent_observations(db, elder_id=elder_id, limit=100)
                if item.get("kind") in {"device_state", "device_ack"}
            ]
            result = {"elder_id": elder_id, "devices": observations}
            self._record_tool_call(db, "get_home_device_snapshot", {"elder_id": elder_id}, ToolCallStatus.ACCEPTED, result)
        return result

    def create_normalized_event(self, event: NormalizedEventV2) -> dict[str, Any]:
        with SessionLocal() as db:
            record = repository.create_event(db, event)
        return record

    def update_event_analysis(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with SessionLocal() as db:
            record = repository.update_event_analysis(db, event_id, payload)
        return record or {"event_id": event_id, "status": "not_found"}

    def create_workflow(self, workflow: WorkflowV2) -> dict[str, Any]:
        with SessionLocal() as db:
            return repository.create_workflow(db, workflow)

    def record_workflow_step(self, step: WorkflowStepV2) -> dict[str, Any]:
        with SessionLocal() as db:
            return repository.create_workflow_step(db, step)

    def request_home_action(self, request: ActionRequestV2) -> dict[str, Any]:
        with SessionLocal() as db:
            event = repository.get_event(db, request.event_id)
            event_type = event.get("event_type") if event else None
            executions: list[dict[str, Any]] = []
            statuses: list[ToolCallStatus] = []
            for command in request.commands:
                allowed, reason = self.device_policy.check(command, event_type=event_type)
                status = ToolCallStatus.DENIED
                mqtt_topic: str | None = None
                if allowed:
                    ok, publish_result = self.mqtt_bridge.publish_command(
                        room=command.room,
                        device=str(command.device),
                        payload={
                            "cmd_id": new_id("cmd"),
                            "elder_id": request.elder_id,
                            "room": command.room,
                            "device": str(command.device),
                            "action": str(command.action),
                            "value": command.value,
                            "reason": command.reason or request.reason,
                            "priority": str(request.priority),
                        },
                    )
                    status = ToolCallStatus.ACCEPTED if ok else ToolCallStatus.FAILED
                    reason = "published" if ok else publish_result
                    mqtt_topic = publish_result if ok else None
                execution = ActionExecutionV2(
                    request_id=request.request_id,
                    event_id=request.event_id,
                    elder_id=request.elder_id,
                    command=command,
                    status=status,
                    reason=reason,
                    mqtt_topic=mqtt_topic,
                )
                executions.append(repository.create_action_execution(db, execution))
                statuses.append(status)
            final_status = ToolCallStatus.ACCEPTED if statuses and all(item == ToolCallStatus.ACCEPTED for item in statuses) else ToolCallStatus.PARTIAL
            if statuses and all(item == ToolCallStatus.DENIED for item in statuses):
                final_status = ToolCallStatus.DENIED
            if statuses and all(item == ToolCallStatus.FAILED for item in statuses):
                final_status = ToolCallStatus.FAILED
            result = {"request_id": request.request_id, "executions": executions}
            self._record_tool_call(
                db,
                "request_home_action",
                model_to_dict(request),
                final_status,
                result,
                reason="policy gated execution",
                workflow_id=request.workflow_id,
                event_id=request.event_id,
                elder_id=request.elder_id,
            )
        return {"status": str(final_status), **result}

    def raise_family_alert(self, alert: AlertRequestV2) -> dict[str, Any]:
        record = AlertRecordV2(
            workflow_id=alert.workflow_id,
            event_id=alert.event_id,
            elder_id=alert.elder_id,
            alert_level=alert.alert_level,
            channel=alert.channel,
            message=alert.message,
        )
        with SessionLocal() as db:
            result = repository.create_alert(db, record)
            self._record_tool_call(
                db,
                "raise_family_alert",
                model_to_dict(alert),
                ToolCallStatus.ACCEPTED,
                result,
                workflow_id=alert.workflow_id,
                event_id=alert.event_id,
                elder_id=alert.elder_id,
            )
        return result

    def create_hmi_prompt(self, prompt: HmiPromptV2) -> dict[str, Any]:
        with SessionLocal() as db:
            record = repository.create_hmi_prompt(db, prompt)
            repository.update_workflow_status(db, prompt.workflow_id, status="waiting_hmi", current_step="hmi_followup")
            repository.update_event_state(db, prompt.event_id, state="wait_response")
            self._record_tool_call(
                db,
                "create_hmi_prompt",
                model_to_dict(prompt),
                ToolCallStatus.ACCEPTED,
                record,
                workflow_id=prompt.workflow_id,
                event_id=prompt.event_id,
                elder_id=prompt.elder_id,
            )
        return record

    def get_current_hmi_prompt(self, elder_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            prompt = repository.latest_waiting_hmi_prompt(db, elder_id)
        return prompt or {"status": "none", "elder_id": elder_id}

    def respond_hmi(self, response: HmiResponseV2) -> dict[str, Any]:
        with SessionLocal() as db:
            prompt = repository.respond_hmi_prompt(db, response)
            if prompt is None:
                return {"status": "ignored", "reason": "prompt_not_found"}
            safe = response.response_type in {"safe", "我没事"}
            new_state = "resolved" if safe else "family_alert"
            workflow_status = "resolved" if safe else "escalated"
            repository.update_event_state(db, response.event_id, state=new_state)
            repository.update_workflow_status(db, prompt["workflow_id"], status=workflow_status, current_step="hmi_response")
            result = {"status": new_state, "prompt": prompt, "response": model_to_dict(response)}
            self._record_tool_call(
                db,
                "respond_hmi",
                model_to_dict(response),
                ToolCallStatus.ACCEPTED,
                result,
                workflow_id=prompt["workflow_id"],
                event_id=response.event_id,
                elder_id=response.elder_id,
            )
        if not safe:
            alert = self.raise_family_alert(
                AlertRequestV2(
                    workflow_id=prompt["workflow_id"],
                    event_id=response.event_id,
                    elder_id=response.elder_id,
                    alert_level=RiskLevel.P1,
                    message=f"老人回复：{response.response_text}",
                )
            )
            result["alert"] = alert
        return result

    def record_workflow_note(self, note: WorkflowNoteV2) -> dict[str, Any]:
        step = WorkflowStepV2(
            workflow_id=note.workflow_id,
            event_id=note.event_id,
            elder_id=note.elder_id,
            step_name=note.step_name,
            status="completed",
            input=note.payload,
            output={"recorded": True},
        )
        return self.record_workflow_step(step)

    @staticmethod
    def _record_tool_call(
        db: Any,
        name: str,
        arguments: dict[str, Any],
        status: ToolCallStatus,
        result: dict[str, Any],
        *,
        reason: str = "",
        workflow_id: str | None = None,
        event_id: str | None = None,
        elder_id: str | None = None,
    ) -> dict[str, Any]:
        return repository.create_tool_call(
            db,
            ToolCallV2(
                workflow_id=workflow_id,
                event_id=event_id,
                elder_id=elder_id,
                tool_name=name,
                arguments=arguments,
                status=status,
                result=result,
                reason=reason,
            ),
        )
