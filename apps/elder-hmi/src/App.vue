<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;

const state = reactive<AnyRecord>({
  current_risk_level: "P4",
  events: []
});
const loading = ref(false);

const currentEvent = computed(() => state.events?.[0] ?? null);
const currentPrompt = computed(() => state.current_hmi_prompt?.status === "waiting" ? state.current_hmi_prompt : null);
const statusText = computed(() => {
  if (state.current_risk_level === "P0" || state.current_risk_level === "P1") return "告警";
  if (state.current_risk_level === "P2" || state.current_risk_level === "P3") return "注意";
  return "正常";
});
const statusClass = computed(() => {
  if (statusText.value === "告警") return "danger";
  if (statusText.value === "注意") return "warning";
  return "normal";
});
const systemText = computed(() => currentPrompt.value?.message ?? currentEvent.value?.summary ?? "系统正常守护中");

async function loadState() {
  loading.value = true;
  const response = await fetch(`${API_BASE}/api/v2/dashboard/state`);
  Object.assign(state, await response.json());
  loading.value = false;
}

async function respond(responseType: "safe" | "help" | "contact_family", responseText: string) {
  if (!currentPrompt.value) return;
  loading.value = true;
  await fetch(`${API_BASE}/api/v2/hmi/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt_id: currentPrompt.value.prompt_id,
      event_id: currentPrompt.value.event_id,
      elder_id: currentPrompt.value.elder_id,
      response_type: responseType,
      response_text: responseText
    })
  });
  await loadState();
}

onMounted(async () => {
  await loadState();
  window.setInterval(loadState, 3000);
});
</script>

<template>
  <main class="screen" :class="statusClass">
    <section class="topbar">
      <span>居家健康守护 v2</span>
      <span>{{ loading ? "刷新中" : "本地守护中" }}</span>
    </section>

    <section class="status">
      <p>当前状态</p>
      <h1>{{ statusText }}</h1>
      <h2>{{ systemText }}</h2>
    </section>

    <section class="actions">
      <button class="safe" :disabled="!currentPrompt || loading" @click="respond('safe', '我没事')">我没事</button>
      <button class="help" :disabled="!currentPrompt || loading" @click="respond('help', '需要帮助')">需要帮助</button>
      <button class="family" :disabled="!currentPrompt || loading" @click="respond('contact_family', '联系家属')">联系家属</button>
    </section>
  </main>
</template>
