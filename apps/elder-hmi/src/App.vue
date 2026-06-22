<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;
const state = reactive<AnyRecord>({ current_risk_level: "P4", events: [], current_hmi_prompt: null });
const initialLoading = ref(true);
const refreshing = ref(false);
const submitting = ref(false);
const clearing = ref(false);
const networkMessage = ref("");
const clearMessage = ref("");
const clearedAt = ref<string | null>(null);
localStorage.removeItem("hmiClearedAt");
let refreshTimer: number | undefined;

const isCleared = computed(() => Boolean(clearMessage.value));

function itemTimestamp(item?: AnyRecord | null): string {
  if (!item) return "";
  return item.completed_at ?? item.responded_at ?? item.updated_at ?? item.created_at ?? item.observed_at ?? "";
}

function isAfterClearTime(item?: AnyRecord | null): boolean {
  return Boolean(item);
}

const ACTIVE_EVENT_STATES = new Set([
  "event_detected",
  "rule_classified",
  "action_planned",
  "ask_elder",
  "wait_response",
  "family_alert",
  "emergency_alert",
  "escalated"
]);

function isActiveEvent(event?: AnyRecord | null): boolean {
  if (!event || !isAfterClearTime(event)) return false;
  return ACTIVE_EVENT_STATES.has(String(event.state ?? "").toLowerCase());
}

const rawWaitingPrompt = computed(() => state.current_hmi_prompt?.status === "waiting" ? state.current_hmi_prompt : null);
const currentEvent = computed(() => (state.events ?? []).find((event: AnyRecord) => isActiveEvent(event)) ?? null);
const currentPrompt = computed(() => rawWaitingPrompt.value ?? null);
const visibleRiskLevel = computed(() => {
  if (currentPrompt.value) return String(currentPrompt.value.risk_level ?? state.current_risk_level);
  if (currentEvent.value) return String(currentEvent.value.final_risk_level ?? currentEvent.value.risk_level ?? state.current_risk_level);
  if (isCleared.value && ["P0", "P1"].includes(String(state.current_risk_level ?? ""))) return String(state.current_risk_level);
  return "P4";
});
const statusText = computed(() => {
  if (visibleRiskLevel.value === "P0" || visibleRiskLevel.value === "P1") return "告警";
  if (visibleRiskLevel.value === "P2" || visibleRiskLevel.value === "P3") return "注意";
  return "正常";
});
const statusClass = computed(() => statusText.value === "告警" ? "danger" : statusText.value === "注意" ? "warning" : "normal");
const systemText = computed(() => {
  if (currentPrompt.value?.message) return currentPrompt.value.message;
  if (currentEvent.value?.summary) return currentEvent.value.summary;
  if (isCleared.value && ["P0", "P1"].includes(String(state.current_risk_level ?? ""))) return "存在高风险事件，请注意安全";
  return "系统正常守护中";
});

async function loadState(options: { initial?: boolean } = {}) {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    const response = await fetch(`${API_BASE}/api/v2/dashboard/state`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    Object.assign(state, await response.json());
    networkMessage.value = "";
  } catch (error) {
    networkMessage.value = `网络暂时不可用，页面将自动重试（${error instanceof Error ? error.message : "请求失败"}）`;
  } finally {
    refreshing.value = false;
    if (options.initial) initialLoading.value = false;
  }
}

async function respond(responseType: "safe" | "help" | "contact_family", responseText: string) {
  const prompt = currentPrompt.value ? { ...currentPrompt.value } : null;
  if (!prompt || submitting.value) return;
  submitting.value = true;
  networkMessage.value = "";
  try {
    const response = await fetch(`${API_BASE}/api/v2/hmi/respond`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt_id: prompt.prompt_id,
        event_id: prompt.event_id,
        elder_id: prompt.elder_id,
        response_type: responseType,
        response_text: responseText
      })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    if (result.status === "ignored") throw new Error(result.reason ?? "反馈未被接受");
    if (state.current_hmi_prompt?.prompt_id === prompt.prompt_id) state.current_hmi_prompt = null;
    networkMessage.value = `已提交：${responseText}`;
    await loadState();
  } catch (error) {
    networkMessage.value = `提交失败，请再次点击（${error instanceof Error ? error.message : "请求失败"}）`;
  } finally {
    submitting.value = false;
  }
}

function clearHmiDisplay() {
  const value = new Date().toISOString();
  clearedAt.value = value;
  localStorage.setItem("hmiClearedAt", value);
  networkMessage.value = "";
  clearMessage.value = rawWaitingPrompt.value ? "已清除旧提示，当前待确认提示仍会保留" : "已清空提示，系统正常守护中";
}

function restoreHmiDisplay() {
  clearedAt.value = null;
  localStorage.removeItem("hmiClearedAt");
  clearMessage.value = "";
}

function resetHmiState() {
  state.current_risk_level = "P4";
  state.events = [];
  state.current_hmi_prompt = null;
  state.hmi_prompts = [];
  state.hmi_responses = [];
  state.alerts = [];
}

async function clearHmiHistory() {
  if (clearing.value) return;
  clearing.value = true;
  networkMessage.value = "";
  try {
    const response = await fetch(`${API_BASE}/api/v2/hmi/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ elder_id: state.elder_id ?? "elder_001" })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    await response.json();
    resetHmiState();
    clearedAt.value = new Date().toISOString();
    clearMessage.value = "演示提示已清除，系统正常守护中";
  } catch (error) {
    networkMessage.value = `清除失败：${error instanceof Error ? error.message : "请求失败"}`;
  } finally {
    clearing.value = false;
  }
}

async function refreshHmiDisplay() {
  clearMessage.value = "";
  clearedAt.value = null;
  await loadState();
}

function scheduleRefresh() {
  refreshTimer = window.setTimeout(async () => {
    await loadState();
    scheduleRefresh();
  }, 3000);
}

onMounted(async () => {
  await loadState({ initial: true });
  scheduleRefresh();
});
onBeforeUnmount(() => refreshTimer && window.clearTimeout(refreshTimer));
</script>

<template>
  <main class="screen" :class="statusClass">
    <section class="topbar">
      <span>居家健康守护 v2</span>
      <div class="topbar-right">
        <span>{{ initialLoading ? "正在连接" : submitting ? "正在提交反馈" : "本地守护中" }}</span>
        <button type="button" :disabled="clearing" @click="clearHmiHistory">{{ clearing ? "清除中..." : "清除提示历史" }}</button>
        <button type="button" @click="refreshHmiDisplay">刷新当前状态</button>
      </div>
    </section>

    <p v-if="networkMessage" class="network-message">{{ networkMessage }}</p>
    <p v-if="clearMessage" class="network-message">{{ clearMessage }}</p>

    <section class="status">
      <p>当前状态</p>
      <h1>{{ statusText }}</h1>
      <h2>{{ initialLoading ? "正在加载守护状态" : systemText }}</h2>
    </section>

    <section class="actions">
      <button class="safe" :disabled="!currentPrompt || submitting" @click="respond('safe', '我没事')">我没事</button>
      <button class="help" :disabled="!currentPrompt || submitting" @click="respond('help', '需要帮助')">需要帮助</button>
      <button class="family" :disabled="!currentPrompt || submitting" @click="respond('contact_family', '联系家属')">联系家属</button>
    </section>
  </main>
</template>
