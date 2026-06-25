<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;
type DemoNodeState = "idle" | "running" | "completed" | "skipped" | "failed";
type DemoTarget = { kind: "event" | "candidate" | "normal_input" | "risk_input"; id: string; item: AnyRecord } | null;
type ObservationRiskHint = { event_type: string; risk_level: string; room?: string; observed_at?: string } | null;
const DISPLAY_LIMIT = 10;
const ROOM_ORDER = ["bedroom", "bathroom", "living_room", "kitchen"];
const RISK_ORDER: Record<string, number> = { P4: 0, P3: 1, P2: 2, P1: 3, P0: 4 };
const DEFAULT_HOME_ENV: Record<string, AnyRecord> = {
  bedroom: { room: "bedroom", temperature: 24.0, humidity: 50.0, co2_ppm: 820, gas_ppm: 0, smoke_ppm: 0, presence: false, is_default: true },
  bathroom: { room: "bathroom", temperature: 24.0, humidity: 58.0, co2_ppm: 780, gas_ppm: 0, smoke_ppm: 0, presence: false, is_default: true },
  living_room: { room: "living_room", temperature: 24.5, humidity: 49.0, co2_ppm: 850, gas_ppm: 0, smoke_ppm: 0, presence: false, is_default: true },
  kitchen: { room: "kitchen", temperature: 25.0, humidity: 52.0, co2_ppm: 880, gas_ppm: 0, smoke_ppm: 0, presence: false, is_default: true }
};
const ROOM_LABELS: Record<string, string> = {
  bedroom: "卧室",
  bathroom: "卫生间",
  living_room: "客厅",
  kitchen: "厨房"
};
const EVENT_LABELS: Record<string, string> = {
  normal: "正常状态",
  gas_leak: "燃气异常",
  spo2_low: "低血氧",
  heart_rate_abnormal: "心率异常",
  suspected_fall: "疑似跌倒",
  long_static: "长时间静止",
  co2_high: "CO2 偏高",
  temperature_high: "室温过高",
  temperature_low: "室温过低",
  humidity_abnormal: "湿度异常",
  vital_baseline_anomaly: "生命体征基线异常",
  bathroom_stay_anomaly: "卫生间停留过长"
};
const IMPORTANT_STEPS = new Set([
  "rule_gate",
  "frame_collection",
  "local_multiframe_analysis",
  "local_policy_execution",
  "cloud_review",
  "final_advisory"
]);

const state = reactive<AnyRecord>({
  elder_id: "elder_001",
  current_risk_level: "P4",
  events: [],
  workflows: [],
  workflow_steps: [],
  tool_calls: [],
  action_executions: [],
  hmi_prompts: [],
  hmi_responses: [],
  device_readings_latest: [],
  alerts: [],
  daily_health_summaries: []
});
const loading = ref(false);
const lastUpdated = ref("");
const loadError = ref("");
const clearNotice = ref("");
const clearing = ref(false);
const clearedAt = ref<string | null>(null);
const deviceControlBusy = ref("");
const deviceControlMessage = ref("");
localStorage.removeItem("dashboardClearedAt");
let refreshTimer: number | undefined;

const isCleared = computed(() => Boolean(clearNotice.value));

function itemTimestamp(item?: AnyRecord | null): string {
  if (!item) return "";
  return item.completed_at ?? item.responded_at ?? item.updated_at ?? item.created_at ?? item.observed_at ?? "";
}

function isAfterClearTime(item?: AnyRecord | null): boolean {
  return Boolean(item);
}

const filteredEvents = computed(() => ((state.events ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredWorkflowSteps = computed(() => ((state.workflow_steps ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredToolCalls = computed(() => ((state.tool_calls ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredActionExecutions = computed(() => ((state.action_executions ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredPrompts = computed(() => ((state.hmi_prompts ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredResponses = computed(() => ((state.hmi_responses ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredAlerts = computed(() => ((state.alerts ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredDeviceReadings = computed(() => ((state.device_readings_latest ?? []) as AnyRecord[]).filter(isAfterClearTime));
const filteredObservations = computed(() => ((state.observations ?? []) as AnyRecord[]).filter(isAfterClearTime));

const latestEvent = computed(() => filteredEvents.value?.[0] ?? null);
const latestInputObservation = computed(() =>
  filteredObservations.value.find((observation: AnyRecord) =>
    ["vital", "environment", "device_state"].includes(String(observation.kind ?? "").toLowerCase())
  ) ?? null
);
function latestInputObservationGroup(observation: AnyRecord | null): AnyRecord[] {
  if (!observation) return [];
  const timestamp = new Date(eventTime(observation)).getTime();
  if (Number.isNaN(timestamp)) return [observation];
  return filteredObservations.value.filter((item: AnyRecord) => {
    if (!["vital", "environment", "device_state"].includes(String(item.kind ?? "").toLowerCase())) return false;
    const itemTime = new Date(eventTime(item)).getTime();
    return !Number.isNaN(itemTime) && Math.abs(itemTime - timestamp) <= 5000;
  });
}

function numericPayloadValue(payload: AnyRecord, key: string): number | null {
  const value = payload?.[key];
  if (value === null || value === undefined || value === "") return null;
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function observationRiskHint(observation?: AnyRecord | null): ObservationRiskHint {
  if (!observation) return null;
  const kind = String(observation.kind ?? "").toLowerCase();
  const payload = observation.payload && typeof observation.payload === "object" ? observation.payload : {};
  const room = String(payload.room ?? observation.room ?? "");
  const observed_at = eventTime(observation);
  if (kind === "environment") {
    const gas = numericPayloadValue(payload, "gas_ppm");
    const co2 = numericPayloadValue(payload, "co2_ppm");
    const temperature = numericPayloadValue(payload, "temperature");
    const humidity = numericPayloadValue(payload, "humidity");
    if (gas !== null && gas >= 100) return { event_type: "gas_leak", risk_level: "P0", room, observed_at };
    if (co2 !== null && co2 >= 1500) return { event_type: "co2_high", risk_level: "P3", room, observed_at };
    if (temperature !== null && temperature >= 30) return { event_type: "temperature_high", risk_level: "P3", room, observed_at };
    if (temperature !== null && temperature <= 16) return { event_type: "temperature_low", risk_level: "P3", room, observed_at };
    if (humidity !== null && (humidity < 25 || humidity > 75)) return { event_type: "humidity_abnormal", risk_level: "P3", room, observed_at };
  }
  if (kind === "vital") {
    const spo2 = numericPayloadValue(payload, "spo2");
    const heartRate = numericPayloadValue(payload, "heart_rate");
    if (spo2 !== null && spo2 < 88) return { event_type: "spo2_low", risk_level: "P0", room, observed_at };
    if (spo2 !== null && spo2 < 92) return { event_type: "spo2_low", risk_level: "P1", room, observed_at };
    if (heartRate !== null && (heartRate < 45 || heartRate > 130)) return { event_type: "heart_rate_abnormal", risk_level: "P1", room, observed_at };
  }
  return null;
}

const RISK_PRIORITY: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3, P4: 4 };

function riskPriority(risk: string): number {
  return RISK_PRIORITY[risk] ?? 99;
}

function observationGroupRiskHint(observations: AnyRecord[]): ObservationRiskHint {
  return observations
    .map((observation) => observationRiskHint(observation))
    .filter((hint): hint is NonNullable<ObservationRiskHint> => Boolean(hint))
    .sort((a, b) => {
      const priorityDelta = (RISK_PRIORITY[a.risk_level] ?? 99) - (RISK_PRIORITY[b.risk_level] ?? 99);
      if (priorityDelta !== 0) return priorityDelta;
      return new Date(b.observed_at ?? "").getTime() - new Date(a.observed_at ?? "").getTime();
    })[0] ?? null;
}

function eventMatchesObservationRisk(event: AnyRecord | null, hint: ObservationRiskHint): boolean {
  if (!event || !hint) return false;
  const eventType = String(event.event_type ?? "");
  const eventRisk = String(event.final_risk_level ?? event.risk_level ?? "");
  const eventRoom = String(event.room ?? "");
  return (
    eventType === hint.event_type &&
    eventRisk === hint.risk_level &&
    (!hint.room || !eventRoom || eventRoom === hint.room)
  );
}

function riskInputTarget(observation: AnyRecord, hint: NonNullable<ObservationRiskHint>): DemoTarget {
  return {
    kind: "risk_input",
    id: String(observation.observation_id ?? `${hint.event_type}-${eventTime(observation)}`),
    item: {
      ...observation,
      event_type: hint.event_type,
      risk_level: hint.risk_level,
      rule_risk_level: hint.risk_level,
      final_risk_level: hint.risk_level,
      decision_source: "rule_pending",
      room: hint.room ?? observation.payload?.room ?? observation.room,
      summary: `${hint.event_type} risk input received`
    }
  };
}

const activeDemoTarget = computed<DemoTarget>(() => {
  const events = filteredEvents.value;
  const steps = filteredWorkflowSteps.value;
  const candidates = ((state.ai_review_candidates ?? []) as AnyRecord[]).filter(isAfterClearTime);
  const sortedCandidates = [...candidates].sort((a, b) => new Date(eventTime(b)).getTime() - new Date(eventTime(a)).getTime());
  const latestCandidate = sortedCandidates[0] ?? null;
  const latestCandidateTime = latestCandidate ? new Date(eventTime(latestCandidate)).getTime() : 0;
  const latestCandidateStatus = String(latestCandidate?.status ?? "").toLowerCase();
  const candidateInProgress = ["pending", "reviewing"].includes(latestCandidateStatus);
  const latestObservation = latestInputObservation.value;
  const latestObservationTime = latestObservation ? new Date(eventTime(latestObservation)).getTime() : 0;
  const latestEventTime = latestEvent.value ? new Date(eventTime(latestEvent.value)).getTime() : 0;
  const promotedCandidateFallback = candidates.find((candidate) =>
    String(candidate.status ?? "").toLowerCase() === "promoted" &&
    candidate.promoted_event_id &&
    !events.some((event) => event.event_id === candidate.promoted_event_id)
  );
  const promotedCandidateFallbackTime = promotedCandidateFallback ? new Date(eventTime(promotedCandidateFallback)).getTime() : 0;
  const latestObservationGroup = latestInputObservationGroup(latestObservation);
  const latestObservationRisk = observationGroupRiskHint(latestObservationGroup);
  const latestEventRisk = latestEvent.value ? riskOf(latestEvent.value) : "";

  const activeStates = new Set(["event_detected", "rule_classified", "action_planned", "ask_elder", "wait_response", "family_alert", "emergency_alert", "escalated"]);
  const activeEvent = events.find((event) =>
    riskPriority(String(event.final_risk_level ?? event.risk_level ?? "")) <= riskPriority("P3") &&
    activeStates.has(String(event.state ?? "").toLowerCase())
  );

  if (latestObservationRisk && latestObservation?.observation_id) {
    if (latestObservationTime < latestCandidateTime && candidateInProgress && latestCandidate?.candidate_id) {
      return { kind: "candidate", id: String(latestCandidate.candidate_id), item: latestCandidate };
    }
    const matchedLatestEvent = latestEvent.value;
    if (matchedLatestEvent && eventMatchesObservationRisk(matchedLatestEvent, latestObservationRisk)) {
      return { kind: "event", id: String(matchedLatestEvent.event_id), item: matchedLatestEvent };
    }
    if (activeEvent && eventMatchesObservationRisk(activeEvent, latestObservationRisk)) {
      return { kind: "event", id: String(activeEvent.event_id), item: activeEvent };
    }
    return riskInputTarget(latestObservation, latestObservationRisk);
  }

  if (
    latestObservation?.observation_id &&
    latestObservationTime > latestEventTime &&
    !latestObservationRisk &&
    !(candidateInProgress && latestCandidate?.candidate_id) &&
    (!latestEvent.value || riskPriority(latestEventRisk) >= riskPriority("P3"))
  ) {
    return {
      kind: "normal_input",
      id: String(latestObservation.observation_id),
      item: {
        ...latestObservation,
        event_type: "normal",
        risk_level: "P4",
        rule_risk_level: "P4",
        final_risk_level: "P4",
        decision_source: "rule"
      }
    };
  }

  if (latestCandidate?.candidate_id && latestCandidateTime >= latestEventTime) {
    if (latestCandidateStatus === "promoted" && latestCandidate.promoted_event_id) {
      const promotedEvent = events.find((event) => event.event_id === latestCandidate.promoted_event_id);
      if (promotedEvent?.event_id) return { kind: "event", id: String(promotedEvent.event_id), item: promotedEvent };
    }
    return { kind: "candidate", id: String(latestCandidate.candidate_id), item: latestCandidate };
  }

  if (activeEvent?.event_id) {
    return { kind: "event", id: String(activeEvent.event_id), item: activeEvent };
  }

  if (promotedCandidateFallback?.candidate_id && promotedCandidateFallbackTime > latestEventTime) {
    return { kind: "candidate", id: String(promotedCandidateFallback.candidate_id), item: promotedCandidateFallback };
  }

  const workflowEvent = events.find((event) => steps.some((step) => step.event_id === event.event_id));
  if (workflowEvent?.event_id) return { kind: "event", id: String(workflowEvent.event_id), item: workflowEvent };

  if (latestEvent.value?.event_id) return { kind: "event", id: String(latestEvent.value.event_id), item: latestEvent.value };
  if (latestCandidate?.candidate_id) return { kind: "candidate", id: String(latestCandidate.candidate_id), item: latestCandidate };
  if (promotedCandidateFallback?.candidate_id) return { kind: "candidate", id: String(promotedCandidateFallback.candidate_id), item: promotedCandidateFallback };
  return null;
});
const latestLocalAnalysis = computed(() =>
  findTargetStep(activeDemoTarget.value, "local_multiframe_analysis")
);
const isNormalInputDemo = computed(() => activeDemoTarget.value?.kind === "normal_input");
const isRiskInputDemo = computed(() => activeDemoTarget.value?.kind === "risk_input");
const localSemanticStatus = computed(() => {
  if (isNormalInputDemo.value) {
    return { state: "completed", text: "P4 正常状态，无需本地模型" };
  }
  if (isRiskInputDemo.value) {
    if (activeDemoTarget.value?.kind === "risk_input" && isDeterministicVitalRiskInput(activeDemoTarget.value.item)) {
      return { state: "completed", text: "确定性生命体征规则，无需本地模型" };
    }
    return { state: "pending", text: "异常数据已输入，等待规则判断生成风险事件" };
  }
  const analysis = latestLocalAnalysis.value;
  if (analysis?.output?.reason === "deterministic_p3_rule") {
    return { state: "completed", text: "确定性规则处置，无需本地模型" };
  }
  if (analysis?.output?.reason === "deterministic_vital_rule") {
    return { state: "completed", text: "确定性生命体征规则，无需本地模型" };
  }
  if (analysis?.status === "failed" || analysis?.output?.fallback) {
    const fallbackType = analysis?.output?.fallback_type;
    if (fallbackType === "service_unavailable") return { state: "fallback", text: "本地模型服务暂不可用，已采用规则结果" };
    if (fallbackType === "timeout") return { state: "fallback", text: "本地模型分析超时，已采用规则结果" };
    if (fallbackType === "safety_rejected") return { state: "fallback", text: "模型输出未通过安全校验，已采用规则结果" };
    return { state: "fallback", text: "本地模型请求失败，已采用规则结果" };
  }
  const target = activeDemoTarget.value;
  const output = analysis?.output ?? {};
  if (analysis && ["running", "pending", "reviewing"].includes(String(analysis.status ?? "").toLowerCase())) {
    return { state: "pending", text: "等待 RK3588 本地模型分析" };
  }
  if (output.event_semantics || output.family_summary || output.risk_level) {
    const latency = output.latency_ms !== undefined ? ` · 模型耗时 ${durationText(output.latency_ms)}` : "";
    const resultText = output.event_semantics ?? output.family_summary ?? "本地模型已完成";
    const suffix = target?.kind === "candidate" && String(target.item.status ?? "").toLowerCase() === "dismissed"
      ? " · 未升级为正式风险"
      : "";
    return { state: "completed", text: `${resultText} · 本地风险 ${output.risk_level ?? "--"}${latency}${suffix}` };
  }
  if (target?.kind === "event" && target.item?.local_semantics) {
    return { state: "completed", text: target.item.local_semantics };
  }
  if (target?.kind === "candidate") {
    const result = candidateLocalResult(target.item);
    if (result.event_semantics || result.family_summary || result.risk_level) {
      const latency = result.latency_ms !== undefined ? ` · 模型耗时 ${durationText(result.latency_ms)}` : "";
      const suffix = String(target.item.status ?? "").toLowerCase() === "dismissed" ? " · 未升级为正式风险" : "";
      return {
        state: result.fallback ? "fallback" : "completed",
        text: `${result.event_semantics ?? result.family_summary ?? "本地模型已完成"} · 本地风险 ${result.risk_level ?? "--"}${latency}${suffix}`
      };
    }
  }
  return { state: "pending", text: "等待 RK3588 本地模型分析" };
});

function eventTime(item: AnyRecord): string {
  return itemTimestamp(item);
}

function formatTime(value?: string): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

function clip(value: unknown, length = 72): string {
  const text = String(value ?? "").trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function shortText(value: unknown, length = 40): string {
  const text = String(value ?? "").trim();
  return text.length > length ? `${text.slice(0, length)}...` : text;
}

function riskOf(item: AnyRecord): string {
  return String(item.final_risk_level ?? item.local_risk_level ?? item.rule_risk_level ?? item.risk_level ?? "--");
}

function candidateLocalResult(item: AnyRecord): AnyRecord {
  const features = item.features && typeof item.features === "object" ? item.features : {};
  const result = features.local_result && typeof features.local_result === "object" ? features.local_result : {};
  return result;
}

function demoRisk(target: DemoTarget): string {
  if (!target) return "--";
  if (target.kind === "normal_input") return "P4";
  if (target.kind === "risk_input") return riskOf(target.item);
  if (target.kind === "candidate") return String(candidateLocalResult(target.item).risk_level ?? target.item.risk_level ?? "--");
  return riskOf(target.item);
}

function eventLabel(itemOrType?: AnyRecord | string | null): string {
  const item = typeof itemOrType === "object" && itemOrType !== null ? itemOrType : null;
  const eventType = typeof itemOrType === "string" ? itemOrType : String(item?.event_type ?? "");
  if (eventType === "spo2_low" && item && riskOf(item) === "P0") return "严重低血氧";
  return EVENT_LABELS[eventType] ?? (eventType || "未知事件");
}

const RULE_RESULT_STATES = new Set([
  "event_detected",
  "rule_classified",
  "action_planned",
  "ask_elder",
  "wait_response",
  "family_alert",
  "emergency_alert",
  "escalated",
  "resolved",
  "completed",
  "handled",
  "closed",
  "final_advisory"
]);

function hasEventRuleResult(item: AnyRecord, risk: string): boolean {
  const stateValue = String(item.state ?? "").toLowerCase();
  const eventType = String(item.event_type ?? "");
  return Boolean(eventType && ["P0", "P1", "P2", "P3"].includes(risk) && RULE_RESULT_STATES.has(stateValue));
}

function ruleNodeState(item: AnyRecord, risk: string, step?: AnyRecord | null): DemoNodeState {
  if (step) return stepState(step);
  return hasEventRuleResult(item, risk) ? "completed" : "idle";
}

function isDeterministicRuleOnlyEvent(item: AnyRecord, risk: string): boolean {
  const eventType = String(item.event_type ?? "");
  return (
    (risk === "P0" && eventType === "gas_leak") ||
    (risk === "P3" && ["co2_high", "temperature_high", "temperature_low", "humidity_abnormal"].includes(eventType)) ||
    isDeterministicVitalRuleEvent(item, risk)
  );
}

function isP3EnvironmentRiskInput(item: AnyRecord): boolean {
  return riskOf(item) === "P3" && ["co2_high", "temperature_high", "temperature_low", "humidity_abnormal"].includes(String(item.event_type ?? ""));
}

function isDeterministicVitalRiskInput(item: AnyRecord): boolean {
  const risk = riskOf(item);
  const eventType = String(item.event_type ?? "");
  return (risk === "P1" && ["heart_rate_abnormal", "spo2_low"].includes(eventType)) || (risk === "P0" && eventType === "spo2_low");
}

function ruleNodeNote(item: AnyRecord, risk: string, step?: AnyRecord | null): string {
  if (step?.output?.accepted) return `规则命中 ${risk}`;
  if (step) return "规则处理中";
  if (hasEventRuleResult(item, risk)) return `规则命中 ${item.event_type ?? "risk_event"} · ${risk}`;
  return "规则处理中";
}

const summaryRisk = computed(() => {
  const target = activeDemoTarget.value;
  if (target?.kind === "normal_input") return "P4";
  if (target?.kind === "risk_input") return riskOf(target.item);
  if (target?.kind === "candidate") return demoRisk(target);
  return latestEvent.value?.final_risk_level ?? (isCleared.value ? "P4" : state.current_risk_level);
});
const summaryRuleRisk = computed(() => {
  const target = activeDemoTarget.value;
  if (target?.kind === "normal_input") return "P4";
  if (target?.kind === "risk_input") return riskOf(target.item);
  if (target?.kind === "candidate") return "Candidate";
  return latestEvent.value?.rule_risk_level ?? "P4";
});
const summaryLocalRisk = computed(() => {
  const target = activeDemoTarget.value;
  if (isNormalInputDemo.value || isRiskInputDemo.value) return "--";
  if (target?.kind === "candidate") return String(candidateLocalResult(target.item).risk_level ?? "--");
  return target?.kind === "event" ? (target.item.local_risk_level ?? "--") : "--";
});
const summaryCloudRisk = computed(() => {
  const target = activeDemoTarget.value;
  if (isNormalInputDemo.value || isRiskInputDemo.value || target?.kind === "candidate") return "--";
  return target?.kind === "event" ? (target.item.cloud_risk_level ?? "--") : "--";
});
const summaryDecisionSource = computed(() => {
  const target = activeDemoTarget.value;
  if (isNormalInputDemo.value) return "rule";
  if (isRiskInputDemo.value) return "rule_pending";
  if (target?.kind === "candidate") return "candidate";
  return target?.kind === "event" ? (target.item.decision_source ?? "rule") : "rule";
});

function nodeLabel(stateValue: DemoNodeState): string {
  return {
    idle: "未开始",
    running: "进行中",
    completed: "已完成",
    skipped: "已跳过",
    failed: "失败"
  }[stateValue];
}

function stepState(step?: AnyRecord | null): DemoNodeState {
  if (!step) return "idle";
  const status = String(step.status ?? "").toLowerCase();
  const output = step.output ?? {};
  if (status === "failed" || step.error || output.error || output.fallback) return "failed";
  if (
    status === "skipped" ||
    ["deterministic_p3_rule", "deterministic_vital_rule"].includes(String(output.reason ?? "")) ||
    ["disabled", "not_required"].includes(String(output.status ?? "").toLowerCase())
  ) return "skipped";
  if (["running", "pending", "reviewing"].includes(status)) return "running";
  return "completed";
}

function durationText(ms?: unknown): string {
  const value = Number(ms);
  if (!Number.isFinite(value)) return "";
  if (value < 1000) return `${Math.max(0, Math.round(value))}ms`;
  return `${Math.round(value / 100) / 10}s`;
}

function elapsedSince(value?: string): string {
  if (!value) return "";
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return "";
  const elapsed = Math.max(0, Date.now() - timestamp);
  return durationText(elapsed);
}

function isDeterministicVitalRuleEvent(item: AnyRecord, risk: string): boolean {
  const eventType = String(item.event_type ?? "");
  return (
    (risk === "P1" && ["heart_rate_abnormal", "spo2_low"].includes(eventType)) ||
    (risk === "P0" && eventType === "spo2_low")
  );
}

function targetSteps(target: DemoTarget): AnyRecord[] {
  if (!target) return [];
  if (target.kind === "normal_input" || target.kind === "risk_input") return [];
  const ids = targetWorkflowIds(target);
  return filteredWorkflowSteps.value.filter((step: AnyRecord) => ids.includes(String(step.event_id ?? "")));
}

function findTargetStep(target: DemoTarget, name: string): AnyRecord | null {
  return targetSteps(target).find((step) => step.step_name === name) ?? null;
}

function promotedCandidateIdForEvent(event: AnyRecord): string | null {
  const fromList = ((state.ai_review_candidates ?? []) as AnyRecord[]).find(
    (candidate) => candidate.promoted_event_id === event.event_id
  )?.candidate_id;
  if (fromList) return String(fromList);
  for (const evidence of event.evidence ?? []) {
    const candidateId = evidence?.candidate?.candidate_id;
    if (candidateId) return String(candidateId);
  }
  return null;
}

function isCandidatePromotedEvent(item: AnyRecord): boolean {
  return String(item.source_kind ?? "") === "ai_review_candidate" || Boolean(promotedCandidateIdForEvent(item));
}

function targetWorkflowIds(target: DemoTarget): string[] {
  if (!target) return [];
  if (target.kind === "normal_input" || target.kind === "risk_input") return [];
  if (target.kind === "candidate") return [target.id];
  const candidateId = promotedCandidateIdForEvent(target.item);
  return candidateId ? [target.id, candidateId] : [target.id];
}

function relatedItems(target: DemoTarget, key: string): AnyRecord[] {
  if (!target) return [];
  if (target.kind === "normal_input" || target.kind === "risk_input") return [];
  const source: Record<string, AnyRecord[]> = {
    action_executions: filteredActionExecutions.value,
    hmi_prompts: filteredPrompts.value,
    hmi_responses: filteredResponses.value,
    alerts: filteredAlerts.value
  };
  return (source[key] ?? []).filter((item: AnyRecord) => item.event_id === target.id);
}

function workflowSummary(step: AnyRecord): string {
  const output = step.output ?? {};
  if (step.error) return clip(step.error);
  if (output.error) return clip(output.error);
  if (step.step_name === "rule_gate") return output.accepted ? "规则已命中并进入处置" : "规则未命中";
  if (step.step_name === "frame_collection") {
    const count = output.frames?.filter((frame: AnyRecord) => !frame.missing).length;
    return `${output.status ?? "已完成"}${count !== undefined ? ` · ${count}/5 帧` : ""}`;
  }
  if (step.step_name === "local_multiframe_analysis") {
    if (output.reason === "deterministic_p3_rule") return "确定性规则处置，无需本地模型";
    if (output.reason === "deterministic_vital_rule") return "确定性生命体征规则，无需本地模型";
    const fallbackLabel: Record<string, string> = {
      service_unavailable: "模型服务暂不可用",
      timeout: "模型分析超时",
      safety_rejected: "输出未通过安全校验",
      request_failed: "模型请求失败"
    };
    const latency = output.latency_ms !== undefined ? ` · 模型耗时 ${durationText(output.latency_ms)}` : "";
    const queue = output.queue_wait_ms ? ` · 排队等待 ${durationText(output.queue_wait_ms)}` : "";
    return clip(`${output.event_semantics ?? "本地分析"} · ${output.risk_level ?? "--"}${latency}${queue}${output.fallback ? ` · ${fallbackLabel[output.fallback_type] ?? "规则回退"}` : ""}`);
  }
  if (step.step_name === "local_policy_execution") return clip(output.status ?? "本地策略已执行");
  if (step.step_name === "cloud_review") {
    if (output.reason === "deterministic_p3_rule") return "确定性规则处置，无需云端复核";
    if (output.reason === "deterministic_vital_rule") return "确定性生命体征规则，无需云端复核";
    return clip(`${output.status ?? step.status}${output.risk_level ? ` · ${output.risk_level}` : ""}${output.family_summary ? ` · ${output.family_summary}` : ""}`);
  }
  if (step.step_name === "final_advisory") return clip(`${output.final_risk_level ?? "--"} · ${output.family_summary ?? "最终建议已生成"}`);
  return clip(output.status ?? step.status);
}

function candidateLabel(type?: unknown): string {
  const value = String(type ?? "");
  if (value === "bathroom_stay_anomaly") return "卫生间停留过长";
  if (value === "vital_baseline_anomaly") return "生命体征基线异常";
  return value || "ai_review_candidate";
}

function candidateStatusText(status?: unknown): string {
  const value = String(status ?? "").toLowerCase();
  return {
    pending: "等待本地复核",
    reviewing: "RK3588 本地模型分析中",
    dismissed: "本地复核完成，未升级为风险",
    failed: "本地复核失败，已记录",
    promoted: "本地复核已升级为正式风险"
  }[value] ?? (value || "--");
}

const demoTitle = computed(() => {
  const target = activeDemoTarget.value;
  if (!target) return "当前演示：暂无演示事件";
  if (target.kind === "candidate") {
    const item = target.item;
    if (String(item.status ?? "").toLowerCase() === "promoted") {
      return `当前演示：${candidateLabel(item.candidate_type)} · ${candidateStatusText(item.status)}`;
    }
    return `当前演示：${candidateLabel(item.candidate_type)} · ${candidateStatusText(item.status)}`;
  }
  if (target.kind === "risk_input") {
    return `当前演示：${eventLabel(target.item)} · ${riskOf(target.item)} · 异常数据已输入`;
  }
  if (target.kind === "normal_input") {
    return "当前演示：正常状态 · P4 · 正常数据已记录";
  }
  const item = target.item;
  const risk = riskOf(item);
  const prompts = relatedItems(target, "hmi_prompts");
  const responses = relatedItems(target, "hmi_responses");
  const waitingPrompt = prompts.some((prompt) => String(prompt.status ?? "").toLowerCase() === "waiting");
  if (waitingPrompt) {
    return `当前演示：${eventLabel(item)} · ${risk} · 等待老人反馈`;
  }
  if (responses.length) {
    return `当前演示：${eventLabel(item)} · ${risk} · 老人反馈已完成`;
  }
  if (isDeterministicRuleOnlyEvent(item, risk)) {
    const actions = relatedItems(target, "action_executions");
    const alerts = relatedItems(target, "alerts");
    const phase = hasEventRuleResult(item, risk) || actions.length || alerts.length
      ? "规则处置已完成"
      : "规则处理中";
    return `当前演示：${eventLabel(item)} · ${risk} · ${phase}`;
  }
  const finalStep = findTargetStep(target, "final_advisory");
  const localStep = findTargetStep(target, "local_multiframe_analysis");
  const phase = finalStep ? "最终建议已生成" : localStep ? "本地模型已处理" : "规则处理中";
  return `当前演示：${eventLabel(item)} · ${riskOf(item)} · ${phase}`;
});

const demoNodes = computed(() => {
  const target = activeDemoTarget.value;
  if (!target) {
    return [
      { key: "input", name: "数据输入", state: "idle" as DemoNodeState, note: "暂无演示数据", time: "" },
      { key: "edge", name: "Edge MCP", state: "idle" as DemoNodeState, note: "等待数据接入", time: "" },
      { key: "rule", name: "规则判断", state: "idle" as DemoNodeState, note: "等待规则输入", time: "" },
      { key: "local", name: "本地 AI / Candidate", state: "idle" as DemoNodeState, note: "等待分析", time: "" },
      { key: "cloud", name: "云端复核", state: "idle" as DemoNodeState, note: "等待本地结果", time: "" },
      { key: "policy", name: "设备策略", state: "idle" as DemoNodeState, note: "等待处置", time: "" },
      { key: "hmi", name: "HMI / 家属", state: "idle" as DemoNodeState, note: "等待提示或告警", time: "" }
    ];
  }

  const ruleStep = findTargetStep(target, "rule_gate");
  const localStep = findTargetStep(target, "local_multiframe_analysis");
  const cloudStep = findTargetStep(target, "cloud_review");
  const policyStep = findTargetStep(target, "local_policy_execution");
  const finalStep = findTargetStep(target, "final_advisory");
  const actions = relatedItems(target, "action_executions");
  const prompts = relatedItems(target, "hmi_prompts");
  const responses = relatedItems(target, "hmi_responses");
  const alerts = relatedItems(target, "alerts");
  const isCandidate = target.kind === "candidate";
  const item = target.item;
  const isPromotedCandidateEvent = target.kind === "event" && isCandidatePromotedEvent(item);
  const candidateStatus = String(item.status ?? "").toLowerCase();
  const risk = riskOf(item);
  const dataTime = eventTime(item) || (filteredObservations.value?.[0] ? eventTime(filteredObservations.value[0]) : "");
  const localOutput = localStep?.output ?? {};
  const localLatency = localOutput.latency_ms !== undefined ? ` · 模型耗时 ${durationText(localOutput.latency_ms)}` : "";
  const queueWait = localOutput.queue_wait_ms ? ` · 排队等待 ${durationText(localOutput.queue_wait_ms)}` : "";
  const cloudOutput = cloudStep?.output ?? {};
  const waitingPrompt = prompts.find((prompt) => String(prompt.status ?? "").toLowerCase() === "waiting");
  const hasHmiResponse = Boolean(responses.length);
  const deterministicRuleOnly = target.kind === "event" && isDeterministicRuleOnlyEvent(item, risk);
  const deterministicVitalRule = target.kind === "event" && isDeterministicVitalRuleEvent(item, risk);
  const localWait = !localStep && ["P0", "P1", "P2"].includes(risk) && !deterministicRuleOnly
    ? `等待本地模型队列 · 已等待 ${elapsedSince(eventTime(item)) || "--"}`
    : "等待本地分析";
  const localNote = localOutput.reason === "deterministic_p3_rule"
    ? "P3 确定性规则，无需本地模型"
    : localOutput.reason === "deterministic_vital_rule" || deterministicVitalRule
      ? "确定性生命体征规则，无需本地模型"
      : `${localOutput.event_semantics ?? localOutput.status ?? localWait}${localOutput.risk_level ? ` · ${localOutput.risk_level}` : ""}${localLatency}${queueWait}`;
  const localState = deterministicRuleOnly || localOutput.reason === "deterministic_vital_rule"
    ? "skipped" as DemoNodeState
    : localStep
      ? stepState(localStep)
      : (["P0", "P1", "P2"].includes(risk) ? "running" as DemoNodeState : "idle" as DemoNodeState);

  if (target.kind === "normal_input") {
    const group = latestInputObservationGroup(target.item);
    const kinds = group.map((item) => item.kind).filter(Boolean).join(" / ") || target.item.kind || "observation";
    const room = group.find((item) => item.kind === "environment")?.payload?.room ?? target.item.payload?.room ?? "--";
    return [
      {
        key: "input",
        name: "数据输入",
        state: "completed" as DemoNodeState,
        note: shortText(`收到正常 MQTT 数据：${kinds}`),
        time: eventTime(target.item)
      },
      {
        key: "edge",
        name: "Edge MCP",
        state: "completed" as DemoNodeState,
        note: shortText(`已写入 v2_raw_observations · ${room}`),
        time: eventTime(target.item)
      },
      {
        key: "rule",
        name: "规则判断",
        state: "completed" as DemoNodeState,
        note: "未命中风险规则，保持 P4",
        time: eventTime(target.item)
      },
      {
        key: "local",
        name: "本地 AI / Candidate",
        state: "skipped" as DemoNodeState,
        note: "P4 正常状态，无需本地模型",
        time: eventTime(target.item)
      },
      {
        key: "cloud",
        name: "云端复核",
        state: "skipped" as DemoNodeState,
        note: "P4 正常状态，无需云端复核",
        time: eventTime(target.item)
      },
      {
        key: "policy",
        name: "设备策略",
        state: "skipped" as DemoNodeState,
        note: "无风险，不执行设备动作",
        time: eventTime(target.item)
      },
      {
        key: "hmi",
        name: "HMI / 家属",
        state: "skipped" as DemoNodeState,
        note: "无需询问或告警",
        time: eventTime(target.item)
      }
    ];
  }

  if (target.kind === "risk_input") {
    const group = latestInputObservationGroup(target.item);
    const kinds = group.map((item) => item.kind).filter(Boolean).join(" / ") || target.item.kind || "observation";
    const room = target.item.room ?? group.find((item) => item.kind === "environment")?.payload?.room ?? target.item.payload?.room ?? "--";
    const isP3Environment = isP3EnvironmentRiskInput(target.item);
    const isDeterministicVital = isDeterministicVitalRiskInput(target.item);
    return [
      {
        key: "input",
        name: "数据输入",
        state: "completed" as DemoNodeState,
        note: shortText(`收到异常 MQTT 数据：${kinds}`),
        time: eventTime(target.item)
      },
      {
        key: "edge",
        name: "Edge MCP",
        state: "completed" as DemoNodeState,
        note: shortText(`已写入 v2_raw_observations · ${room}`),
        time: eventTime(target.item)
      },
      {
        key: "rule",
        name: "规则判断",
        state: (isP3Environment || isDeterministicVital ? "completed" : "running") as DemoNodeState,
        note: isP3Environment
          ? "P3 环境规则已命中"
          : isDeterministicVital
            ? "生命体征硬规则已命中"
            : "等待 Orchestrator 生成风险事件",
        time: eventTime(target.item)
      },
      {
        key: "local",
        name: "本地 AI / Candidate",
        state: (isP3Environment || isDeterministicVital ? "skipped" : "idle") as DemoNodeState,
        note: isP3Environment
          ? "确定性 P3 规则，无需本地模型"
          : isDeterministicVital
            ? "确定性生命体征规则，无需本地模型"
            : "等待风险事件",
        time: eventTime(target.item)
      },
      {
        key: "cloud",
        name: "云端复核",
        state: (isP3Environment || isDeterministicVital ? "skipped" : "idle") as DemoNodeState,
        note: isP3Environment
          ? "确定性 P3 规则，无需云端复核"
          : isDeterministicVital
            ? "确定性生命体征规则，无需云端复核"
            : "等待本地处置结果",
        time: eventTime(target.item)
      },
      {
        key: "policy",
        name: "设备策略",
        state: (isP3Environment ? "completed" : isDeterministicVital ? "skipped" : "idle") as DemoNodeState,
        note: isP3Environment
          ? "环境联动策略已记录或等待执行"
          : isDeterministicVital
            ? "生命体征事件无需设备动作"
            : "等待规则处置",
        time: eventTime(target.item)
      },
      {
        key: "hmi",
        name: "HMI / 家属",
        state: (isP3Environment || isDeterministicVital ? "running" : "idle") as DemoNodeState,
        note: isP3Environment
          ? "等待老人反馈"
          : isDeterministicVital
            ? "沿用首条异常的 HMI 询问和家属告警"
            : "等待提示或告警",
        time: eventTime(target.item)
      }
    ];
  }

  return [
    {
      key: "input",
      name: "数据输入",
      state: "completed" as DemoNodeState,
      note: isCandidate ? "Candidate 已创建" : "MQTT/API 数据已进入",
      time: dataTime
    },
    {
      key: "edge",
      name: "Edge MCP",
      state: "completed" as DemoNodeState,
      note: shortText(isCandidate ? `候选事件 ${candidateStatusText(candidateStatus)}` : `事件 ${eventLabel(item)}`),
      time: eventTime(item)
    },
    {
      key: "rule",
      name: "规则判断",
      state: isCandidate || isPromotedCandidateEvent ? "skipped" as DemoNodeState : ruleNodeState(item, risk, ruleStep),
      note: isCandidate
        ? "非硬规则事件，进入候选复核"
        : isPromotedCandidateEvent
          ? "Candidate 复核升级，非硬规则"
          : shortText(ruleNodeNote(item, risk, ruleStep)),
      time: eventTime(ruleStep ?? item)
    },
    {
      key: "local",
      name: "本地 AI / Candidate",
      state: localState,
      note: shortText(localNote),
      time: eventTime(localStep ?? item)
    },
    {
      key: "cloud",
      name: "云端复核",
      state: deterministicRuleOnly ? "skipped" as DemoNodeState : stepState(cloudStep),
      note: shortText(
        cloudOutput.reason === "deterministic_vital_rule" || deterministicVitalRule
          ? "确定性生命体征规则，无需云端复核"
          : cloudOutput.reason === "deterministic_p3_rule"
            ? "确定性规则跳过云端"
            : `${cloudOutput.status ?? "等待复核"}${cloudOutput.risk_level ? ` · ${cloudOutput.risk_level}` : ""}`
      ),
      time: eventTime(cloudStep ?? item)
    },
    {
      key: "policy",
      name: "设备策略",
      state: actions.length ? "completed" as DemoNodeState : policyStep ? stepState(policyStep) : ["P3", "P4"].includes(risk) || candidateStatus === "dismissed" ? "skipped" as DemoNodeState : "idle" as DemoNodeState,
      note: shortText(actions.length ? `设备动作 ${actions.length} 条` : policyStep?.output?.status ?? (candidateStatus === "dismissed" ? "候选已记录，不执行设备" : "等待策略")),
      time: eventTime(actions[0] ?? policyStep ?? item)
    },
    {
      key: "hmi",
      name: "HMI / 家属",
      state: waitingPrompt
        ? "running" as DemoNodeState
        : hasHmiResponse || alerts.length
          ? "completed" as DemoNodeState
          : prompts.length
            ? "running" as DemoNodeState
            : ["P3", "P4"].includes(risk) || candidateStatus === "dismissed"
              ? "skipped" as DemoNodeState
              : "idle" as DemoNodeState,
      note: shortText(responses.length ? `老人反馈 ${responses[0].response_text}` : alerts.length ? `家属告警 ${statusLabel(alerts[0].status)}` : prompts.length ? `HMI ${statusLabel(prompts[0].status)}` : "无需询问或告警"),
      time: eventTime(responses[0] ?? alerts[0] ?? prompts[0] ?? finalStep ?? item)
    }
  ];
});

const riskEvents = computed(() => filteredEvents.value.slice(0, DISPLAY_LIMIT));
const workflowSteps = computed(() => {
  const target = activeDemoTarget.value;
  const source = target
    ? (target.kind === "event" || target.kind === "candidate" ? targetSteps(target) : [])
    : filteredWorkflowSteps.value;
  return source
    .filter((step: AnyRecord) => IMPORTANT_STEPS.has(step.step_name) || step.status === "failed" || step.error)
    .slice(0, DISPLAY_LIMIT);
});
const workflowEmptyText = computed(() =>
  activeDemoTarget.value?.kind === "risk_input" && isP3EnvironmentRiskInput(activeDemoTarget.value.item)
    ? "P3 冷却窗口内不重复生成工作流，已按规则演示处置链路"
    : activeDemoTarget.value?.kind === "risk_input" && isDeterministicVitalRiskInput(activeDemoTarget.value.item)
      ? "生命体征冷却窗口内不重复生成工作流，当前异常数据已入库，并沿用首条异常的 HMI 与家属告警链路"
    : activeDemoTarget.value?.kind === "risk_input"
      ? "等待规则判断生成工作流"
      : "暂无工作流记录"
);

const DEVICE_LABELS: Record<string, string> = {
  air_conditioner: "空调",
  window: "窗户",
  gas_valve: "燃气阀",
  local_alarm: "本地报警器",
  alarm: "本地报警器",
  light: "灯光",
  fan: "风扇"
};
const ACTION_LABELS: Record<string, string> = {
  turn_on: "打开",
  turn_off: "关闭",
  open: "打开",
  close: "关闭",
  set_temperature: "设置温度",
  alarm_on: "启动报警",
  alarm_off: "关闭报警"
};
const DASHBOARD_DEVICE_CONTROLS = [
  {
    room: "bedroom",
    devices: [
      { device: "light", label: "灯光", on: "turn_on", off: "turn_off" },
      { device: "air_conditioner", label: "空调", on: "turn_on", off: "turn_off" },
      { device: "window", label: "窗户", on: "open", off: "close" }
    ]
  },
  {
    room: "bathroom",
    devices: [
      { device: "light", label: "灯光", on: "turn_on", off: "turn_off" },
      { device: "air_conditioner", label: "暖风/空调", on: "turn_on", off: "turn_off" },
      { device: "window", label: "窗户", on: "open", off: "close" }
    ]
  },
  {
    room: "living_room",
    devices: [
      { device: "light", label: "灯光", on: "turn_on", off: "turn_off" },
      { device: "air_conditioner", label: "空调", on: "turn_on", off: "turn_off" },
      { device: "window", label: "窗户", on: "open", off: "close" }
    ]
  },
  {
    room: "kitchen",
    devices: [
      { device: "light", label: "灯光", on: "turn_on", off: "turn_off" },
      { device: "window", label: "窗户", on: "open", off: "close" },
      { device: "gas_valve", label: "燃气阀", on: "open", off: "close" }
    ]
  }
];
const STATUS_LABELS: Record<string, string> = {
  completed: "已完成",
  success: "已完成",
  sent: "已发送",
  accepted: "已接受",
  pending: "等待中",
  waiting: "等待中",
  responded: "已反馈",
  failed: "失败",
  skipped: "已跳过",
  running: "进行中",
  local_completed: "本地统计已完成",
  cloud_completed: "云端摘要已完成",
  cloud_disabled: "云端未启用",
  cloud_failed: "云端摘要失败"
};

function deviceLabel(value: unknown): string {
  const key = String(value ?? "");
  return DEVICE_LABELS[key] ?? key ?? "--";
}

function actionLabel(value: unknown): string {
  const key = String(value ?? "");
  return ACTION_LABELS[key] ?? key ?? "--";
}

function statusLabel(value: unknown): string {
  const key = String(value ?? "").toLowerCase();
  return STATUS_LABELS[key] ?? String(value ?? "--");
}

function policyTitle(item: AnyRecord): string {
  if (item.itemType !== "execution") return `${item.tool_name ?? "工具调用"} · 异常调用`;
  const command = item.command ?? {};
  const room = command.room ? `${command.room} · ` : "";
  return `${room}${deviceLabel(command.device)} · ${actionLabel(command.action)}`;
}

const policyExecutions = computed<AnyRecord[]>(() => {
  const executions = filteredActionExecutions.value.map((item: AnyRecord) => ({ ...item, itemType: "execution" }));
  const failedCalls = filteredToolCalls.value
    .filter((item: AnyRecord) => !["accepted", "completed", "success"].includes(String(item.status).toLowerCase()))
    .map((item: AnyRecord) => ({ ...item, itemType: "tool" }));
  return [...executions, ...failedCalls]
    .sort((a, b) => new Date(eventTime(b)).getTime() - new Date(eventTime(a)).getTime())
    .slice(0, DISPLAY_LIMIT);
});
const hmiPromptItems = computed<AnyRecord[]>(() => {
  const prompts = filteredPrompts.value.map((item: AnyRecord) => ({ ...item, itemType: "prompt" }));
  const responses = filteredResponses.value.map((item: AnyRecord) => ({ ...item, itemType: "response" }));
  return [...prompts, ...responses]
    .sort((a, b) => new Date(eventTime(b)).getTime() - new Date(eventTime(a)).getTime())
    .slice(0, DISPLAY_LIMIT);
});
const familyAlertItems = computed<AnyRecord[]>(() => filteredAlerts.value.slice(0, DISPLAY_LIMIT));
const realDevices = computed(() => filteredDeviceReadings.value.slice(0, DISPLAY_LIMIT));
const latestDailyHealthSummary = computed<AnyRecord | null>(() => {
  const summaries = ((state.daily_health_summaries ?? []) as AnyRecord[])
    .filter(Boolean)
    .sort((a, b) => new Date(b.updated_at ?? b.created_at ?? b.summary_date ?? "").getTime() - new Date(a.updated_at ?? a.created_at ?? a.summary_date ?? "").getTime());
  return summaries[0] ?? null;
});
const monthlyHealthSummaryBase = computed<AnyRecord[]>(() =>
  ((state.daily_health_summaries ?? []) as AnyRecord[])
    .filter(Boolean)
    .sort((a, b) => new Date(b.summary_date ?? b.updated_at ?? "").getTime() - new Date(a.summary_date ?? a.updated_at ?? "").getTime())
    .slice(0, 30)
);
const dailySummaryStats = computed(() => latestDailyHealthSummary.value?.local_stats ?? {});
const dailySummaryCloud = computed(() => latestDailyHealthSummary.value?.cloud_summary ?? {});
const latestContextFusion = computed(() =>
  filteredWorkflowSteps.value.find(
    (step: AnyRecord) =>
      step.event_id === latestEvent.value?.event_id && step.step_name === "local_context_fusion"
  ) ?? null
);
const normalInputRoom = computed(() =>
  latestInputObservationGroup(activeDemoTarget.value?.kind === "normal_input" ? activeDemoTarget.value.item : null)
    .find((item) => item.kind === "environment")?.payload?.room
);
const riskInputRoom = computed(() =>
  activeDemoTarget.value?.kind === "risk_input"
    ? (activeDemoTarget.value.item.room ?? activeDemoTarget.value.item.payload?.room ?? "--")
    : null
);
const localAiRoom = computed(() =>
  isNormalInputDemo.value
    ? (normalInputRoom.value ?? "--")
    : isRiskInputDemo.value
      ? (riskInputRoom.value ?? "--")
      : (latestContextFusion.value?.output?.elder_location?.current_room ?? "--")
);
const localSemanticEventType = computed(() => {
  const target = activeDemoTarget.value;
  if (target?.kind === "normal_input") return "normal";
  if (target?.kind === "risk_input") return target.item.event_type ?? "risk_input";
  return latestEvent.value?.event_type ?? "暂无事件";
});
const homeEnvironmentRooms = computed(() => {
  const rooms = new Map<string, AnyRecord>(ROOM_ORDER.map((room) => [room, { ...DEFAULT_HOME_ENV[room] }]));
  for (const observation of filteredObservations.value) {
    const payload = observation.payload ?? {};
    const room = payload.room;
    if (!room) continue;
    const current = rooms.get(room) ?? { room };
    if (observation.kind === "environment") {
      rooms.set(room, { ...current, ...payload, observed_at: observation.observed_at, is_default: false });
    }
    if (observation.kind === "device_state" && ["pir_presence", "presence_sensor"].includes(String(payload.device ?? ""))) {
      rooms.set(room, { ...current, presence: payload.present, presence_state: payload.state, presence_at: observation.observed_at, is_default: false });
    }
  }
  return ROOM_ORDER.map((room) => rooms.get(room) ?? { ...DEFAULT_HOME_ENV[room] });
});

function metricValue(reading: AnyRecord, metric: string): string {
  const value = reading.metrics?.[metric];
  if (value === undefined || value === null || value === "") return "--";
  const unit = reading.units?.[metric] ?? (metric === "temperature" ? "°C" : metric === "humidity" ? "%" : "");
  return `${value}${unit}`;
}


async function requestDashboardDeviceAction(room: string, device: string, action: string) {
  const key = `${room}:${device}:${action}`;
  if (deviceControlBusy.value) return;
  deviceControlBusy.value = key;
  deviceControlMessage.value = "";
  try {
    const response = await fetch(`${API_BASE}/api/v2/tools/request-home-action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_id: "dashboard_manual_control",
        elder_id: state.elder_id ?? "elder_001",
        requested_by: "web-dashboard",
        priority: "P3",
        reason: "Dashboard ??????",
        commands: [
          {
            room,
            device,
            action,
            reason: "Dashboard ??????"
          }
        ]
      })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    const status = String(result.status ?? result.executions?.[0]?.status ?? "accepted");
    deviceControlMessage.value = `${ROOM_LABELS[room] ?? room} ${deviceLabel(device)} ${actionLabel(action)}?${statusLabel(status)}`;
    await loadState();
  } catch (error) {
    deviceControlMessage.value = `???????${error instanceof Error ? error.message : "????"}`;
  } finally {
    deviceControlBusy.value = "";
  }
}

function clearDashboardDisplay() {
  const value = new Date().toISOString();
  clearedAt.value = value;
  localStorage.setItem("dashboardClearedAt", value);
  clearNotice.value = "已清空当前屏幕，等待新数据进入";
}

function restoreDashboardDisplay() {
  clearedAt.value = null;
  localStorage.removeItem("dashboardClearedAt");
  clearNotice.value = "";
}

function resetRuntimeState() {
  state.current_risk_level = "P4";
  state.events = [];
  state.observations = [];
  state.device_readings = [];
  state.device_readings_latest = [];
  state.ai_review_candidates = [];
  state.workflows = [];
  state.workflow_steps = [];
  state.tool_calls = [];
  state.action_executions = [];
  state.hmi_prompts = [];
  state.hmi_responses = [];
  state.current_hmi_prompt = null;
  state.alerts = [];
}

async function clearDashboardHistory() {
  if (clearing.value) return;
  clearing.value = true;
  loadError.value = "";
  try {
    const response = await fetch(`${API_BASE}/api/v2/dashboard/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ elder_id: state.elder_id ?? "elder_001" })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    resetRuntimeState();
    clearedAt.value = new Date().toISOString();
    const total = Object.values(result.deleted ?? {}).reduce((sum: number, value) => sum + Number(value || 0), 0);
    clearNotice.value = `演示历史已清除，共删除 ${total} 条记录，等待新数据进入`;
    lastUpdated.value = formatTime(new Date().toISOString());
  } catch (error) {
    loadError.value = `清除失败：${error instanceof Error ? error.message : "请求失败"}`;
  } finally {
    clearing.value = false;
  }
}

async function refreshDashboardDisplay() {
  clearNotice.value = "";
  clearedAt.value = null;
  await loadState();
}

function deviceOnline(reading: AnyRecord): boolean {
  if (typeof reading.online === "boolean") return reading.online;
  const observed = new Date(reading.observed_at ?? reading.created_at ?? "");
  if (Number.isNaN(observed.getTime())) return false;
  return Date.now() - observed.getTime() <= 30000;
}

function roomMetric(room: AnyRecord, metric: string, unit = ""): string {
  const value = room?.[metric];
  return value === undefined || value === null || value === "" ? "--" : `${value}${unit}`;
}

function roomTimeLabel(room: AnyRecord): string {
  if (room.is_default) return "默认值";
  return formatTime(room.observed_at ?? room.presence_at);
}

function dailyMetric(label: string, value: unknown, suffix = ""): string {
  const text = value === undefined || value === null || value === "" ? "--" : `${value}${suffix}`;
  return `${label}：${text}`;
}

function dailyVitalsText(): string {
  const vitals = dailySummaryStats.value.vitals ?? {};
  const heart = vitals.heart_rate ?? {};
  const spo2 = vitals.spo2 ?? {};
  return [
    dailyMetric("心率样本", heart.count),
    dailyMetric("心率平均", heart.avg, " bpm"),
    dailyMetric("血氧样本", spo2.count),
    dailyMetric("血氧平均", spo2.avg, "%")
  ].join(" · ");
}

function dailyBehaviorText(): string {
  const behavior = dailySummaryStats.value.behavior ?? {};
  const rooms = (behavior.room_stay_top ?? [])
    .map((item: AnyRecord) => `${item.room ?? "--"} ${item.duration_min ?? "--"}分钟`)
    .join("，");
  return [
    dailyMetric("主要房间", rooms || "--"),
    dailyMetric("卫生间次数", behavior.bathroom_visits),
    dailyMetric("最长停留", behavior.bathroom_stay_max_sec, " 秒")
  ].join(" · ");
}

function dailyEventsText(): string {
  const events = dailySummaryStats.value.events ?? {};
  const byLevel = events.by_level ?? {};
  return [
    dailyMetric("最高风险", events.highest_risk ?? latestDailyHealthSummary.value?.risk_level),
    `P0/P1/P2/P3/P4：${byLevel.P0 ?? 0}/${byLevel.P1 ?? 0}/${byLevel.P2 ?? 0}/${byLevel.P3 ?? 0}/${byLevel.P4 ?? 0}`
  ].join(" · ");
}

function dailyCloudText(): string {
  const summary = latestDailyHealthSummary.value;
  if (!summary) return "暂无每日健康摘要";
  const cloud = dailySummaryCloud.value;
  if (summary.status === "cloud_disabled") return "云端摘要未启用，本地统计摘要有效";
  if (summary.status === "cloud_failed") return `云端摘要失败：${summary.cloud_error ?? "--"}`;
  return cloud.family_message ?? cloud.overall_status ?? summary.status ?? "本地统计摘要已生成";
}

function monthlyTrendBaseText(): string {
  const summaries = monthlyHealthSummaryBase.value;
  if (!summaries.length) return "近30天趋势基础：暂无每日摘要";
  const riskCounts = summaries.reduce((acc: Record<string, number>, item: AnyRecord) => {
    const level = String(item.risk_level ?? "P4");
    acc[level] = (acc[level] ?? 0) + 1;
    return acc;
  }, {});
  const highest = summaries.reduce((current: string, item: AnyRecord) => {
    const level = String(item.risk_level ?? "P4");
    return (RISK_ORDER[level] ?? 0) > (RISK_ORDER[current] ?? 0) ? level : current;
  }, "P4");
  return `近30天趋势基础：已有 ${summaries.length} 天摘要 · 最高风险 ${highest} · P0/P1/P2/P3/P4 ${riskCounts.P0 ?? 0}/${riskCounts.P1 ?? 0}/${riskCounts.P2 ?? 0}/${riskCounts.P3 ?? 0}/${riskCounts.P4 ?? 0}`;
}

async function loadState() {
  if (loading.value) return;
  loading.value = true;
  try {
    const response = await fetch(`${API_BASE}/api/v2/dashboard/state`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    Object.assign(state, await response.json());
    lastUpdated.value = formatTime(new Date().toISOString());
    loadError.value = "";
  } catch (error) {
    loadError.value = `数据更新失败：${error instanceof Error ? error.message : "网络异常"}`;
  } finally {
    loading.value = false;
  }
}

function scheduleRefresh() {
  refreshTimer = window.setTimeout(async () => {
    await loadState();
    scheduleRefresh();
  }, 3000);
}

onMounted(async () => {
  await loadState();
  scheduleRefresh();
});
onBeforeUnmount(() => refreshTimer && window.clearTimeout(refreshTimer));
</script>

<template>
  <main class="dashboard">
    <header>
      <div>
        <h1>居家老人健康守护 v2</h1>
        <p>
          最近更新 {{ lastUpdated || "--" }} · 北京时间
          <span v-if="isCleared"> · 已清屏 {{ formatTime(clearedAt || undefined) }}</span>
          <span v-if="loadError" class="error"> · {{ loadError }}</span>
        </p>
      </div>
      <div class="header-actions">
        <button type="button" :disabled="clearing" @click="clearDashboardHistory">{{ clearing ? "清除中..." : "清除演示历史" }}</button>
        <button type="button" @click="refreshDashboardDisplay">刷新当前状态</button>
        <strong :class="summaryRisk">
          {{ summaryRisk }}
        </strong>
      </div>
    </header>
    <p v-if="isCleared || clearNotice" class="clear-notice">{{ clearNotice || "已清空当前屏幕，等待新数据进入" }}</p>

    <section class="metrics">
      <article><span>老人 ID</span><b>{{ state.elder_id }}</b></article>
      <article><span>规则风险</span><b>{{ summaryRuleRisk }}</b></article>
      <article><span>本地模型</span><b>{{ summaryLocalRisk }}</b></article>
      <article><span>云端复核</span><b>{{ summaryCloudRisk }}</b></article>
      <article><span>最终风险</span><b>{{ summaryRisk }}</b></article>
      <article><span>决策来源</span><b>{{ summaryDecisionSource }}</b></article>
    </section>

    <section class="demo-overview">
      <div class="demo-head">
        <div>
          <span>演示总览</span>
          <h2>{{ demoTitle }}</h2>
        </div>
        <p>数据输入 → Edge MCP → 规则判断 → 本地 AI / Candidate → 云端复核 → 设备策略 → HMI / 家属</p>
      </div>
      <ol class="demo-steps">
        <li v-for="node in demoNodes" :key="node.key" :class="node.state">
          <div class="step-dot">{{ nodeLabel(node.state) }}</div>
          <div class="step-body">
            <strong>{{ node.name }}</strong>
            <p>{{ node.note }}</p>
            <time>{{ formatTime(node.time) }}</time>
          </div>
        </li>
      </ol>
    </section>

    <section class="local-semantics" :class="localSemanticStatus.state">
      <div><span>第二级：RK3588 本地模型事件语义</span><strong>{{ localSemanticStatus.text }}</strong></div>
      <p>{{ localSemanticEventType }} · 本地风险 {{ summaryLocalRisk }} · AI房间 {{ localAiRoom }}</p>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>三级风险事件</h2>
        <div class="panel-scroll"><p v-if="!riskEvents.length" class="empty">暂无风险事件</p><ul>
          <li v-for="event in riskEvents" :key="event.event_id">
            <div class="row-head"><strong>{{ event.final_risk_level ?? event.risk_level }}</strong><time>{{ formatTime(eventTime(event)) }}</time></div>
            <b>{{ event.event_type }} · {{ event.state }}</b>
            <p>{{ clip(event.local_semantics || event.summary) }}</p>
            <small>规则 {{ event.rule_risk_level ?? event.risk_level }} → 本地 {{ event.local_risk_level ?? "--" }} → 云端 {{ event.cloud_risk_level ?? "--" }} · {{ event.decision_source ?? "rule" }}</small>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>策略与设备执行</h2>
        <div class="panel-scroll"><p v-if="!policyExecutions.length" class="empty">暂无设备执行</p><ul>
          <li v-for="item in policyExecutions" :key="item.execution_id ?? item.call_id">
            <div class="row-head"><strong>{{ statusLabel(item.status) }}</strong><time>{{ formatTime(eventTime(item)) }}</time></div>
            <b>{{ policyTitle(item) }}</b>
            <p>{{ clip(item.reason || item.error || "执行记录") }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>房间设备开关</h2>
        <p v-if="deviceControlMessage" class="device-control-message">{{ deviceControlMessage }}</p>
        <div class="device-control-grid">
          <section v-for="room in DASHBOARD_DEVICE_CONTROLS" :key="room.room" class="device-control-room">
            <h3>{{ ROOM_LABELS[room.room] ?? room.room }}</h3>
            <div v-for="control in room.devices" :key="`${room.room}-${control.device}`" class="device-control-row">
              <span>{{ control.label }}</span>
              <button :disabled="deviceControlBusy === `${room.room}:${control.device}:${control.on}`" @click="requestDashboardDeviceAction(room.room, control.device, control.on)">打开</button>
              <button :disabled="deviceControlBusy === `${room.room}:${control.device}:${control.off}`" @click="requestDashboardDeviceAction(room.room, control.device, control.off)">关闭</button>
            </div>
          </section>
        </div>
      </article>

      <article class="panel">
        <h2>分析工作流</h2>
        <div class="panel-scroll"><p v-if="!workflowSteps.length" class="empty">{{ workflowEmptyText }}</p><ul>
          <li v-for="step in workflowSteps" :key="step.step_id">
            <div class="row-head"><strong>{{ step.status }}</strong><time>{{ formatTime(eventTime(step)) }}</time></div>
            <b>{{ step.step_name }} · {{ step.model ?? "rules" }}</b>
            <p>{{ workflowSummary(step) }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>老人 HMI 反馈</h2>
        <div class="panel-scroll"><p v-if="!hmiPromptItems.length" class="empty">暂无老人提示或反馈</p><ul>
          <li v-for="item in hmiPromptItems" :key="item.prompt_id ? `prompt-${item.prompt_id}` : `response-${item.created_at}`">
            <div class="row-head"><strong>{{ statusLabel(item.status ?? item.response_type) }}</strong><time>{{ formatTime(eventTime(item)) }}</time></div>
            <b v-if="item.itemType === 'prompt'">HMI · {{ item.risk_level }} · {{ item.event_type }}</b>
            <b v-else>老人反馈 · {{ item.response_text }}</b>
            <p>{{ clip(item.message) }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>家属告警</h2>
        <div class="panel-scroll"><p v-if="!familyAlertItems.length" class="empty">暂无家属告警</p><ul>
          <li v-for="item in familyAlertItems" :key="item.alert_id">
            <div class="row-head"><strong>{{ statusLabel(item.status) }}</strong><time>{{ formatTime(eventTime(item)) }}</time></div>
            <b>家属告警 · {{ item.alert_level }} · {{ item.channel }}</b>
            <p>{{ clip(item.message) }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>真实设备数据</h2>
        <div class="panel-scroll"><p v-if="!realDevices.length" class="empty">暂无真实设备读数</p><ul>
          <li v-for="reading in realDevices" :key="reading.device_id">
            <div class="row-head">
              <strong :class="deviceOnline(reading) ? 'online' : 'offline'">{{ deviceOnline(reading) ? "在线" : "离线" }}</strong>
              <time>{{ formatTime(reading.observed_at ?? reading.created_at) }}</time>
            </div>
            <b>{{ reading.room }} / {{ reading.device_id }}</b>
            <p>温度 {{ metricValue(reading, "temperature") }} · 湿度 {{ metricValue(reading, "humidity") }}</p>
            <small>{{ reading.device_type ?? "unknown" }} · {{ reading.source ?? "real_device" }} · 仅展示，不进入 AI</small>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>整屋环境状态</h2>
        <div class="panel-scroll"><ul>
          <li v-for="room in homeEnvironmentRooms" :key="room.room">
            <div class="row-head">
              <strong :class="room.presence ? 'online' : 'offline'">{{ room.presence ? "有人" : "无人" }}</strong>
              <time>{{ roomTimeLabel(room) }}</time>
            </div>
            <b>{{ room.room }}</b>
            <p>温度 {{ roomMetric(room, "temperature", "°C") }} · 湿度 {{ roomMetric(room, "humidity", "%") }} · CO2 {{ roomMetric(room, "co2_ppm", "ppm") }}</p>
            <small>燃气 {{ roomMetric(room, "gas_ppm", "ppm") }} · 烟雾 {{ roomMetric(room, "smoke_ppm", "ppm") }} · 光照 {{ roomMetric(room, "illuminance_lux", "lux") }}</small>
          </li>
        </ul></div>
      </article>

      <article class="panel daily-summary-panel">
        <h2>每日健康摘要</h2>
        <div class="panel-scroll">
          <p v-if="!latestDailyHealthSummary" class="empty">暂无每日健康摘要</p>
          <ul v-else>
            <li>
              <div class="row-head">
                <strong>{{ latestDailyHealthSummary.risk_level ?? "--" }}</strong>
                <time>{{ latestDailyHealthSummary.summary_date ?? formatTime(latestDailyHealthSummary.updated_at) }}</time>
              </div>
              <b>状态：{{ statusLabel(latestDailyHealthSummary.status) }}</b>
              <p>{{ dailyVitalsText() }}</p>
              <p>{{ dailyBehaviorText() }}</p>
              <p>{{ dailyEventsText() }}</p>
              <p>{{ monthlyTrendBaseText() }}</p>
              <small>{{ clip(dailyCloudText(), 120) }}</small>
            </li>
          </ul>
        </div>
      </article>

    </section>
  </main>
</template>
