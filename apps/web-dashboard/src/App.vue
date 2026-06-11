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
  const response = await fetch(`${API_BASE}/api/v2/dashboard/state`);
  Object.assign(state, await response.json());
  lastUpdated.value = new Date().toLocaleTimeString();
  loading.value = false;
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
        <h1>居家老人健康守护 v2 Dashboard</h1>
        <p>Edge MCP / Orchestrator 复盘视图 · {{ loading ? "刷新中" : `最近刷新 ${lastUpdated}` }}</p>
      </div>
      <strong :class="state.current_risk_level">{{ state.current_risk_level }}</strong>
    </header>

    <section class="metrics">
      <article>
        <span>老人 ID</span>
        <b>{{ state.elder_id }}</b>
      </article>
      <article>
        <span>当前风险</span>
        <b>{{ state.current_risk_level }}</b>
      </article>
      <article>
        <span>最近事件</span>
        <b>{{ latestEvent?.event_type ?? "--" }}</b>
      </article>
      <article>
        <span>最近观测</span>
        <b>{{ latestObservation?.kind ?? "--" }}</b>
      </article>
      <article>
        <span>最近 Workflow</span>
        <b>{{ latestWorkflow?.status ?? "--" }}</b>
      </article>
      <article>
        <span>工具调用</span>
        <b>{{ state.tool_calls?.length ?? 0 }}</b>
      </article>
    </section>

    <section class="grid">
      <article class="panel wide">
        <h2>v2 事件链路</h2>
        <ul>
          <li v-for="event in state.events" :key="event.event_id">
            <strong>{{ event.risk_level }}</strong>
            <span>{{ event.event_type }} · {{ event.state }}</span>
            <p>{{ event.summary }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>原始观测</h2>
        <ul>
          <li v-for="observation in state.observations" :key="observation.observation_id">
            <strong>{{ observation.kind }}</strong>
            <span>{{ observation.topic ?? observation.source }}</span>
            <p>{{ JSON.stringify(observation.payload) }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>Workflow Steps</h2>
        <ul>
          <li v-for="step in state.workflow_steps" :key="step.step_id">
            <strong>{{ step.status }}</strong>
            <span>{{ step.step_name }} · {{ step.model ?? "mock" }}</span>
            <p>{{ JSON.stringify(step.output) }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>MCP 工具与设备执行</h2>
        <ul>
          <li v-for="call in state.tool_calls" :key="call.call_id">
            <strong>{{ call.status }}</strong>
            <span>{{ call.tool_name }}</span>
            <p>{{ call.reason || JSON.stringify(call.result) }}</p>
          </li>
        </ul>
        <ul>
          <li v-for="execution in state.action_executions" :key="execution.execution_id">
            <strong>{{ execution.status }}</strong>
            <span>{{ execution.command?.room }}/{{ execution.command?.device }} {{ execution.command?.action }}</span>
            <p>{{ execution.reason }} {{ execution.mqtt_topic ?? "" }}</p>
          </li>
        </ul>
      </article>

      <article class="panel wide">
        <h2>HMI 与告警</h2>
        <ul>
          <li v-for="prompt in state.hmi_prompts" :key="prompt.prompt_id">
            <strong>{{ prompt.status }}</strong>
            <span>{{ prompt.risk_level }} · {{ prompt.event_type }}</span>
            <p>{{ prompt.message }}</p>
          </li>
        </ul>
        <ul>
          <li v-for="alert in state.alerts" :key="alert.alert_id">
            <strong>{{ alert.alert_level }}</strong>
            <span>{{ alert.channel }} · {{ alert.status }}</span>
            <p>{{ alert.message }}</p>
          </li>
        </ul>
      </article>
    </section>
  </main>
</template>
