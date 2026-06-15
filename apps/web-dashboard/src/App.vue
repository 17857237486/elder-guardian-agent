<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;
const DISPLAY_LIMIT = 10;
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
  alerts: []
});
const loading = ref(false);
const lastUpdated = ref("");
const loadError = ref("");
let refreshTimer: number | undefined;

const latestEvent = computed(() => state.events?.[0] ?? null);
const latestLocalAnalysis = computed(() =>
  state.workflow_steps?.find(
    (step: AnyRecord) =>
      step.event_id === latestEvent.value?.event_id && step.step_name === "local_multiframe_analysis"
  ) ?? null
);
const localSemanticStatus = computed(() => {
  const analysis = latestLocalAnalysis.value;
  if (analysis?.output?.reason === "deterministic_p3_rule") {
    return { state: "completed", text: "确定性规则处置，无需本地模型" };
  }
  if (analysis?.status === "failed" || analysis?.output?.fallback) {
    const fallbackType = analysis?.output?.fallback_type;
    if (fallbackType === "service_unavailable") return { state: "fallback", text: "本地模型服务暂不可用，已采用规则结果" };
    if (fallbackType === "timeout") return { state: "fallback", text: "本地模型分析超时，已采用规则结果" };
    if (fallbackType === "safety_rejected") return { state: "fallback", text: "模型输出未通过安全校验，已采用规则结果" };
    return { state: "fallback", text: "本地模型请求失败，已采用规则结果" };
  }
  if (latestEvent.value?.local_semantics) {
    return { state: "completed", text: latestEvent.value.local_semantics };
  }
  return { state: "pending", text: "等待 RK3588 本地模型分析" };
});

function eventTime(item: AnyRecord): string {
  return item.completed_at ?? item.responded_at ?? item.updated_at ?? item.created_at ?? "";
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
    const fallbackLabel: Record<string, string> = {
      service_unavailable: "模型服务暂不可用",
      timeout: "模型分析超时",
      safety_rejected: "输出未通过安全校验",
      request_failed: "模型请求失败"
    };
    return clip(`${output.event_semantics ?? "本地分析"} · ${output.risk_level ?? "--"}${output.fallback ? ` · ${fallbackLabel[output.fallback_type] ?? "规则回退"}` : ""}`);
  }
  if (step.step_name === "local_policy_execution") return clip(output.status ?? "本地策略已执行");
  if (step.step_name === "cloud_review") {
    if (output.reason === "deterministic_p3_rule") return "确定性规则处置，无需云端复核";
    return clip(`${output.status ?? step.status}${output.risk_level ? ` · ${output.risk_level}` : ""}${output.family_summary ? ` · ${output.family_summary}` : ""}`);
  }
  if (step.step_name === "final_advisory") return clip(`${output.final_risk_level ?? "--"} · ${output.family_summary ?? "最终建议已生成"}`);
  return clip(output.status ?? step.status);
}

const riskEvents = computed(() => (state.events ?? []).slice(0, DISPLAY_LIMIT));
const workflowSteps = computed(() =>
  (state.workflow_steps ?? [])
    .filter((step: AnyRecord) => IMPORTANT_STEPS.has(step.step_name) || step.status === "failed" || step.error)
    .slice(0, DISPLAY_LIMIT)
);
const policyExecutions = computed(() => {
  const executions = (state.action_executions ?? []).map((item: AnyRecord) => ({ ...item, itemType: "execution" }));
  const failedCalls = (state.tool_calls ?? [])
    .filter((item: AnyRecord) => !["accepted", "completed", "success"].includes(String(item.status).toLowerCase()))
    .map((item: AnyRecord) => ({ ...item, itemType: "tool" }));
  return [...executions, ...failedCalls]
    .sort((a, b) => new Date(eventTime(b)).getTime() - new Date(eventTime(a)).getTime())
    .slice(0, DISPLAY_LIMIT);
});
const hmiAlerts = computed(() => {
  const prompts = (state.hmi_prompts ?? []).map((item: AnyRecord) => ({ ...item, itemType: "prompt" }));
  const alerts = (state.alerts ?? []).map((item: AnyRecord) => ({ ...item, itemType: "alert" }));
  return [...prompts, ...alerts]
    .sort((a, b) => new Date(eventTime(b)).getTime() - new Date(eventTime(a)).getTime())
    .slice(0, DISPLAY_LIMIT);
});
const elderFeedback = computed(() => (state.hmi_responses ?? []).slice(0, DISPLAY_LIMIT));

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
        <p>最近更新 {{ lastUpdated || "--" }} · 北京时间<span v-if="loadError" class="error"> · {{ loadError }}</span></p>
      </div>
      <strong :class="state.current_risk_level">{{ state.current_risk_level }}</strong>
    </header>

    <section class="metrics">
      <article><span>老人 ID</span><b>{{ state.elder_id }}</b></article>
      <article><span>规则风险</span><b>{{ latestEvent?.rule_risk_level ?? "P4" }}</b></article>
      <article><span>本地模型</span><b>{{ latestEvent?.local_risk_level ?? "--" }}</b></article>
      <article><span>云端复核</span><b>{{ latestEvent?.cloud_risk_level ?? "--" }}</b></article>
      <article><span>最终风险</span><b>{{ latestEvent?.final_risk_level ?? state.current_risk_level }}</b></article>
      <article><span>决策来源</span><b>{{ latestEvent?.decision_source ?? "rule" }}</b></article>
    </section>

    <section class="local-semantics" :class="localSemanticStatus.state">
      <div><span>第二级：RK3588 本地模型事件语义</span><strong>{{ localSemanticStatus.text }}</strong></div>
      <p>{{ latestEvent?.event_type ?? "暂无事件" }} · 本地风险 {{ latestEvent?.local_risk_level ?? "--" }}</p>
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
            <div class="row-head"><strong>{{ item.status }}</strong><time>{{ formatTime(eventTime(item)) }}</time></div>
            <b v-if="item.itemType === 'execution'">{{ item.command?.room }}/{{ item.command?.device }} · {{ item.command?.action }}</b>
            <b v-else>{{ item.tool_name }} · 异常调用</b>
            <p>{{ clip(item.reason || item.error || "执行记录") }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>分析工作流</h2>
        <div class="panel-scroll"><p v-if="!workflowSteps.length" class="empty">暂无工作流记录</p><ul>
          <li v-for="step in workflowSteps" :key="step.step_id">
            <div class="row-head"><strong>{{ step.status }}</strong><time>{{ formatTime(eventTime(step)) }}</time></div>
            <b>{{ step.step_name }} · {{ step.model ?? "rules" }}</b>
            <p>{{ workflowSummary(step) }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel">
        <h2>HMI 与家属告警</h2>
        <div class="panel-scroll"><p v-if="!hmiAlerts.length" class="empty">暂无提示或告警</p><ul>
          <li v-for="item in hmiAlerts" :key="item.prompt_id ?? item.alert_id">
            <div class="row-head"><strong>{{ item.status }}</strong><time>{{ formatTime(eventTime(item)) }}</time></div>
            <b v-if="item.itemType === 'prompt'">HMI · {{ item.risk_level }} · {{ item.event_type }}</b>
            <b v-else>家属告警 · {{ item.alert_level }} · {{ item.channel }}</b>
            <p>{{ clip(item.message) }}</p>
          </li>
        </ul></div>
      </article>

      <article class="panel feedback-panel">
        <h2>老人反馈</h2>
        <div class="panel-scroll"><p v-if="!elderFeedback.length" class="empty">暂无老人反馈</p><ul>
          <li v-for="feedback in elderFeedback" :key="`${feedback.prompt_id}-${feedback.created_at}`">
            <div class="row-head"><strong>{{ feedback.response_text }}</strong><time>{{ formatTime(feedback.created_at) }}</time></div>
            <b>{{ feedback.outcome === "resolved" ? "已确认安全" : "已升级家属告警" }}</b>
            <p>事件 {{ feedback.event_id }} · {{ feedback.response_type }}</p>
          </li>
        </ul></div>
      </article>
    </section>
  </main>
</template>
