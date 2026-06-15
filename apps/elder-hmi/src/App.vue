<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;
const state = reactive<AnyRecord>({ current_risk_level: "P4", events: [], current_hmi_prompt: null });
const initialLoading = ref(true);
const refreshing = ref(false);
const submitting = ref(false);
const networkMessage = ref("");
let refreshTimer: number | undefined;

const currentEvent = computed(() => state.events?.[0] ?? null);
const currentPrompt = computed(() => state.current_hmi_prompt?.status === "waiting" ? state.current_hmi_prompt : null);
const statusText = computed(() => {
  if (state.current_risk_level === "P0" || state.current_risk_level === "P1") return "告警";
  if (state.current_risk_level === "P2" || state.current_risk_level === "P3") return "注意";
  return "正常";
});
const statusClass = computed(() => statusText.value === "告警" ? "danger" : statusText.value === "注意" ? "warning" : "normal");
const systemText = computed(() => currentPrompt.value?.message ?? currentEvent.value?.summary ?? "系统正常守护中");

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
      <span>{{ initialLoading ? "正在连接" : submitting ? "正在提交反馈" : "本地守护中" }}</span>
    </section>

    <p v-if="networkMessage" class="network-message">{{ networkMessage }}</p>

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
