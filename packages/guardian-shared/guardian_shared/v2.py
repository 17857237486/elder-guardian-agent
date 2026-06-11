from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from .enums import DeviceAction, DeviceType, EventState, EventType, RiskLevel
from .schemas import GuardianModel, new_id, utc_now


class ObservationKind(StrEnum):
    VITAL = "vital"
    ENVIRONMENT = "environment"
    VISION = "vision"
    DEVICE_STATE = "device_state"
    DEVICE_ACK = "device_ack"
    HMI_RESPONSE = "hmi_response"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HMI = "waiting_hmi"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ToolCallStatus(StrEnum):
    ACCEPTED = "accepted"
    DENIED = "denied"
    PARTIAL = "partial"
    FAILED = "failed"


class RawObservationV2(GuardianModel):
    observation_id: str = Field(default_factory=lambda: new_id("obs"))
    elder_id: str
    kind: ObservationKind
    source: str = "mqtt"
    topic: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)


class NormalizedEventV2(GuardianModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    elder_id: str
    event_type: EventType | str
    risk_level: RiskLevel
    risk_score: float = 0.0
    state: EventState = EventState.RULE_CLASSIFIED
    room: str | None = None
    summary: str = ""
    trigger_observation_ids: list[str] = Field(default_factory=list)
    rule_trace: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkflowV2(GuardianModel):
    workflow_id: str = Field(default_factory=lambda: new_id("wf"))
    event_id: str
    elder_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: str = "rule_gate"
    model: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkflowStepV2(GuardianModel):
    step_id: str = Field(default_factory=lambda: new_id("step"))
    workflow_id: str
    event_id: str
    elder_id: str
    step_name: str
    status: StepStatus = StepStatus.PENDING
    model: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ToolCallV2(GuardianModel):
    call_id: str = Field(default_factory=lambda: new_id("tool"))
    workflow_id: str | None = None
    event_id: str | None = None
    elder_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus
    result: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class ActionCommandV2(GuardianModel):
    room: str
    device: DeviceType | str
    action: DeviceAction | str
    value: Any = None
    reason: str = ""


class ActionRequestV2(GuardianModel):
    request_id: str = Field(default_factory=lambda: new_id("actreq"))
    workflow_id: str | None = None
    event_id: str
    elder_id: str
    requested_by: str = "guardian-orchestrator"
    commands: list[ActionCommandV2] = Field(default_factory=list)
    reason: str = ""
    priority: RiskLevel = RiskLevel.P3
    created_at: datetime = Field(default_factory=utc_now)


class ActionExecutionV2(GuardianModel):
    execution_id: str = Field(default_factory=lambda: new_id("exec"))
    request_id: str
    event_id: str
    elder_id: str
    command: ActionCommandV2
    status: ToolCallStatus
    reason: str = ""
    mqtt_topic: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class AlertRequestV2(GuardianModel):
    workflow_id: str | None = None
    event_id: str
    elder_id: str
    alert_level: RiskLevel
    message: str
    channel: str = "mock_wechat"


class WorkflowNoteV2(GuardianModel):
    workflow_id: str
    event_id: str
    elder_id: str
    step_name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AlertRecordV2(GuardianModel):
    alert_id: str = Field(default_factory=lambda: new_id("alert"))
    workflow_id: str | None = None
    event_id: str
    elder_id: str
    alert_level: RiskLevel
    channel: str = "mock_wechat"
    message: str
    status: str = "sent"
    created_at: datetime = Field(default_factory=utc_now)


class HmiPromptV2(GuardianModel):
    prompt_id: str = Field(default_factory=lambda: new_id("prompt"))
    workflow_id: str
    event_id: str
    elder_id: str
    risk_level: RiskLevel
    event_type: str
    message: str
    options: list[str] = Field(default_factory=lambda: ["我没事", "需要帮助", "联系家属"])
    status: str = "waiting"
    timeout_sec: int = 30
    created_at: datetime = Field(default_factory=utc_now)
    responded_at: datetime | None = None


class HmiResponseV2(GuardianModel):
    prompt_id: str
    event_id: str
    elder_id: str
    response_type: str
    response_text: str
    created_at: datetime = Field(default_factory=utc_now)
