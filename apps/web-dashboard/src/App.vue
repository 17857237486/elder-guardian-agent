<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { API_BASE } from "@elder-guardian/frontend-shared";

type AnyRecord = Record<string, any>;

const state = reactive<AnyRecord>({
  elder_id: "elder_001",
  current_risk_level: "P4",
  events: [],
  observations: [],
  workflows: [],
  workflow_steps: [],
  tool_calls: [],
  action_executions: [],
  hmi_prompts: [],
  alerts: []
});
const loading = ref(false);
const lastUpdated = ref("");
const latestEvent = computed(() => state.events?.[0] ?? null);
const latestObservation = computed(() => state.observations?.[0] ?? null);
const latestWorkflow = computed(() => state.workflows?.[0] ?? null);

async function loadState() {
  loading.value = true;
  try {
    const response = await fetch(`${API_BASE}/api/v2/dashboard/state`);
    Object.assign(state, await response.json());
    lastUpdated.value = new Date().toLocaleTimeString();
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  await loadState();
  window.setInterval(loadState, 3000);
});
</script>

<template>
  <main class="dashboard">
    <header>
      <div>
        <h1>居家老人健康守护 v2</h1>
        <p>规则、本地多模态模型与云端复核 · {{ loading ? "刷新中" : `最近刷新 ${lastUpdated}` }}</p>
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

    <section class="grid">
      <article class="panel wide">
        <h2>三级风险事件</h2>
        <ul>
          <li v-for="event in state.events" :key="event.event_id">
            <strong>{{ event.final_risk_level ?? event.risk_level }}</strong>
            <span>{{ event.event_type }} · {{ event.state }} · {{ event.decision_source ?? "rule" }}</span>
            <p>{{ event.summary }}</p>
            <p class="tiers">
              规则 {{ event.rule_risk_level ?? event.risk_level }} →
              本地 {{ event.local_risk_level ?? "--" }} →
              云端 {{ event.cloud_risk_level ?? "--" }}
            </p>
            <p v-if="event.image_refs?.length">关键帧：{{ event.image_refs.join(" · ") }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>原始观察</h2>
        <ul>
          <li v-for="observation in state.observations" :key="observation.observation_id">
            <strong>{{ observation.kind }}</strong>
            <span>{{ observation.topic ?? observation.source }}</span>
            <p>{{ JSON.stringify(observation.payload) }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>分析工作流</h2>
        <ul>
          <li v-for="step in state.workflow_steps" :key="step.step_id">
            <strong>{{ step.status }}</strong>
            <span>{{ step.step_name }} · {{ step.model ?? "rules" }}</span>
            <p>{{ JSON.stringify(step.output) }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>策略与设备执行</h2>
        <ul>
          <li v-for="execution in state.action_executions" :key="execution.execution_id">
            <strong>{{ execution.status }}</strong>
            <span>{{ execution.command?.room }}/{{ execution.command?.device }} {{ execution.command?.action }}</span>
            <p>{{ execution.reason }} {{ execution.mqtt_topic ?? "" }}</p>
          </li>
          <li v-for="call in state.tool_calls" :key="call.call_id">
            <strong>{{ call.status }}</strong><span>{{ call.tool_name }}</span>
            <p>{{ call.reason || JSON.stringify(call.result) }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>HMI 与家属告警</h2>
        <ul>
          <li v-for="prompt in state.hmi_prompts" :key="prompt.prompt_id">
            <strong>{{ prompt.status }}</strong><span>{{ prompt.risk_level }} · {{ prompt.event_type }}</span>
            <p>{{ prompt.message }}</p>
          </li>
          <li v-for="alert in state.alerts" :key="alert.alert_id">
            <strong>{{ alert.alert_level }}</strong><span>{{ alert.channel }} · {{ alert.status }}</span>
            <p>{{ alert.message }}</p>
          </li>
        </ul>
      </article>

      <article class="panel">
        <h2>运行概览</h2>
        <p>最近事件：{{ latestEvent?.event_type ?? "--" }}</p>
        <p>最近观察：{{ latestObservation?.kind ?? "--" }}</p>
        <p>最近工作流：{{ latestWorkflow?.status ?? "--" }}</p>
      </article>
    </section>
  </main>
</template>
