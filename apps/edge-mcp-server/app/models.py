from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RawObservationModel(Base):
    __tablename__ = "v2_raw_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    observation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class DeviceReadingModel(Base):
    __tablename__ = "v2_device_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    reading_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    device_type: Mapped[str] = mapped_column(String(128), default="unknown", index=True)
    room: Mapped[str] = mapped_column(String(64), default="living_room", index=True)
    source: Mapped[str] = mapped_column(String(64), default="real_device", index=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    units_json: Mapped[str] = mapped_column(Text, default="{}")
    topic: Mapped[str | None] = mapped_column(String(256), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class BehaviorSegmentModel(Base):
    __tablename__ = "v2_behavior_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    segment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    segment_type: Mapped[str] = mapped_column(String(64), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    room: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_kinds_json: Mapped[str] = mapped_column(Text, default="[]")
    start_observation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    end_observation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="closed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class PersonalBaselineModel(Base):
    __tablename__ = "v2_personal_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    baseline_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    baseline_type: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(64), default="default", index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lookback_days: Mapped[int] = mapped_column(Integer, default=14)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    quality: Mapped[str] = mapped_column(String(64), default="insufficient_data", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AiReviewCandidateModel(Base):
    __tablename__ = "v2_ai_review_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_type: Mapped[str] = mapped_column(String(64), index=True)
    priority: Mapped[str] = mapped_column(String(32), default="low", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    source_segment_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    baseline_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class WorkerStateModel(Base):
    __tablename__ = "v2_worker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    worker_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class NormalizedEventModel(Base):
    __tablename__ = "v2_normalized_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    risk_level: Mapped[str] = mapped_column(String(8), index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(64), index=True)
    room: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    trigger_observation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_trace_json: Mapped[str] = mapped_column(Text, default="{}")
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    frame_set_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    image_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    local_risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    local_semantics: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cloud_risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    final_risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    decision_source: Mapped[str] = mapped_column(String(32), default="rule")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class WorkflowModel(Base):
    __tablename__ = "v2_workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workflow_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    current_step: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class WorkflowStepModel(Base):
    __tablename__ = "v2_workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    step_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    step_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCallModel(Base):
    __tablename__ = "v2_mcp_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    elder_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(64), index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ActionExecutionModel(Base):
    __tablename__ = "v2_action_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    execution_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    command_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    mqtt_topic: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AlertRecordModel(Base):
    __tablename__ = "v2_alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    alert_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    alert_level: Mapped[str] = mapped_column(String(8), index=True)
    channel: Mapped[str] = mapped_column(String(64), default="mock_wechat")
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), default="sent", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class HmiPromptModel(Base):
    __tablename__ = "v2_hmi_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    prompt_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    risk_level: Mapped[str] = mapped_column(String(8), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(64), default="waiting", index=True)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HmiResponseModel(Base):
    __tablename__ = "v2_hmi_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    prompt_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    response_type: Mapped[str] = mapped_column(String(64), index=True)
    response_text: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(64), default="recorded", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class DailyHealthSummaryModel(Base):
    __tablename__ = "v2_daily_health_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    summary_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    elder_id: Mapped[str] = mapped_column(String(64), index=True)
    summary_date: Mapped[str] = mapped_column(String(32), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    local_stats_json: Mapped[str] = mapped_column(Text, default="{}")
    cloud_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    cloud_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(8), default="P4", index=True)
    generated_by: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
