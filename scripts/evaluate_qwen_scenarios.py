#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str
    payload: dict[str, Any]
    terminal_steps: tuple[str, ...]


SCENARIOS: dict[str, Scenario] = {
    "co2_high": Scenario(
        name="co2_high",
        kind="environment",
        payload={"room": "living_room", "co2_ppm": 1800, "temperature": 25.2},
        terminal_steps=("action_request_conversation",),
    ),
    "temperature_high": Scenario(
        name="temperature_high",
        kind="environment",
        payload={"room": "bedroom", "temperature": 31.5, "co2_ppm": 650},
        terminal_steps=("action_request_conversation",),
    ),
    "temperature_low": Scenario(
        name="temperature_low",
        kind="environment",
        payload={"room": "bedroom", "temperature": 15.5, "co2_ppm": 650},
        terminal_steps=("action_request_conversation",),
    ),
    "heart_rate_abnormal": Scenario(
        name="heart_rate_abnormal",
        kind="vital",
        payload={"heart_rate": 138, "spo2": 96},
        terminal_steps=("hmi_followup",),
    ),
    "long_static": Scenario(
        name="long_static",
        kind="vision",
        payload={"room": "living_room", "event_type": "long_static", "confidence": 0.72},
        terminal_steps=("hmi_followup",),
    ),
    "gas_leak": Scenario(
        name="gas_leak",
        kind="environment",
        payload={"room": "kitchen", "gas_ppm": 180, "smoke_ppm": 15, "co2_ppm": 700, "temperature": 25.0},
        terminal_steps=("action_request_conversation",),
    ),
}
LLM_STEP_NAMES = {"context_fetch_conversation", "sensor_fusion_conversation", "risk_decision_conversation", "advisory_conversation"}
LLM_TERMINAL_STEPS = {"advisory_conversation", "llm_chain_timeout"}


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc


def post_observation(edge_url: str, elder_id: str, scenario: Scenario) -> tuple[str, float]:
    payload = {
        "elder_id": elder_id,
        "kind": scenario.kind,
        "source": "qwen_eval",
        "topic": f"eval/{scenario.name}",
        "payload": scenario.payload,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    started = time.monotonic()
    response = request_json("POST", f"{edge_url}/api/v2/observations", payload, timeout=30.0)
    elapsed = time.monotonic() - started
    observation = response.get("observation") or {}
    observation_id = observation.get("observation_id")
    if not observation_id:
        raise RuntimeError(f"POST /observations returned no observation_id: {response}")
    return observation_id, elapsed


def find_event(state: dict[str, Any], observation_id: str) -> dict[str, Any] | None:
    for event in state.get("events", []):
        if observation_id in (event.get("trigger_observation_ids") or []):
            return event
    return None


def summarize_step_output(output: dict[str, Any]) -> dict[str, Any]:
    return {
        "fallback": bool(output.get("fallback")),
        "summary": output.get("summary", ""),
        "risk_level": output.get("risk_level"),
        "reviewed_risk_level": output.get("reviewed_risk_level"),
        "recommended_actions": output.get("recommended_actions", []),
        "recommended_followup": output.get("recommended_followup", []),
        "schema_repaired_fields": output.get("schema_repaired_fields", []),
        "error": output.get("error"),
    }


def wait_for_result(edge_url: str, elder_id: str, observation_id: str, scenario: Scenario, timeout: float, wait_llm: bool) -> dict[str, Any]:
    started = time.monotonic()
    last_state: dict[str, Any] = {}
    event: dict[str, Any] | None = None
    baseline_elapsed: float | None = None
    while time.monotonic() - started < timeout:
        last_state = request_json("GET", f"{edge_url}/api/v2/dashboard/state?elder_id={elder_id}", timeout=15.0)
        event = find_event(last_state, observation_id)
        if event:
            event_id = event.get("event_id")
            steps = [step for step in last_state.get("workflow_steps", []) if step.get("event_id") == event_id]
            names = {step.get("step_name") for step in steps}
            if baseline_elapsed is None and any(name in names for name in scenario.terminal_steps):
                baseline_elapsed = time.monotonic() - started
                if not wait_llm or event.get("risk_level") == "P0":
                    return build_result(scenario, started, event, steps, last_state, baseline_elapsed=baseline_elapsed)
            if baseline_elapsed is not None and wait_llm and any(name in names for name in LLM_TERMINAL_STEPS):
                return build_result(scenario, started, event, steps, last_state, baseline_elapsed=baseline_elapsed)
        time.sleep(2)
    if event:
        event_id = event.get("event_id")
        steps = [step for step in last_state.get("workflow_steps", []) if step.get("event_id") == event_id]
        return build_result(scenario, started, event, steps, last_state, timed_out=True, baseline_elapsed=baseline_elapsed)
    return {
        "scenario": scenario.name,
        "timed_out": True,
        "wait_elapsed_sec": round(time.monotonic() - started, 2),
        "error": "No event was created for observation.",
    }


def build_result(
    scenario: Scenario,
    started: float,
    event: dict[str, Any],
    steps: list[dict[str, Any]],
    state: dict[str, Any],
    timed_out: bool = False,
    baseline_elapsed: float | None = None,
) -> dict[str, Any]:
    event_id = event.get("event_id")
    workflows = [workflow for workflow in state.get("workflows", []) if workflow.get("event_id") == event_id]
    actions = [action for action in state.get("action_executions", []) if action.get("event_id") == event_id]
    alerts = [alert for alert in state.get("alerts", []) if alert.get("event_id") == event_id]
    hmi_prompts = [prompt for prompt in state.get("hmi_prompts", []) if prompt.get("event_id") == event_id]
    llm_steps = [
        step
        for step in steps
        if step.get("step_name") in LLM_STEP_NAMES or step.get("step_name") == "llm_chain_timeout"
    ]
    fallback_steps = [step.get("step_name") for step in llm_steps if (step.get("output") or {}).get("fallback")]
    llm_terminal_present = any(step.get("step_name") in LLM_TERMINAL_STEPS for step in steps)
    return {
        "scenario": scenario.name,
        "timed_out": timed_out,
        "wait_elapsed_sec": round(time.monotonic() - started, 2),
        "baseline_elapsed_sec": round(baseline_elapsed, 2) if baseline_elapsed is not None else None,
        "llm_chain_elapsed_sec": round(time.monotonic() - started, 2) if llm_terminal_present else None,
        "event_type": event.get("event_type"),
        "risk_level": event.get("risk_level"),
        "workflow_ids": [workflow.get("workflow_id") for workflow in workflows],
        "step_names": [step.get("step_name") for step in steps],
        "fallback_steps": fallback_steps,
        "llm_output_samples": [summarize_step_output(step.get("output") or {}) for step in llm_steps],
        "action_statuses": [
            {
                "status": action.get("status"),
                "command": action.get("command"),
                "reason": action.get("reason"),
                "mqtt_topic": action.get("mqtt_topic"),
            }
            for action in actions
        ],
        "hmi_prompt_statuses": [prompt.get("status") for prompt in hmi_prompts],
        "alert_statuses": [alert.get("status") for alert in alerts],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Qwen step quality on Elder Guardian v2 scenarios.")
    parser.add_argument("--edge-url", default="http://127.0.0.1:8010", help="edge MCP server base URL")
    parser.add_argument("--elder-id", default="elder-001")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for each scenario")
    parser.add_argument(
        "--case",
        action="append",
        choices=sorted(SCENARIOS),
        help="scenario to run; can be repeated. Defaults to representative scenarios.",
    )
    parser.add_argument("--all", action="store_true", help="run all scenarios")
    parser.add_argument("--wait-llm", action="store_true", help="wait for background LLM chain terminal step")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    names = sorted(SCENARIOS) if args.all else args.case or ["co2_high", "temperature_high", "heart_rate_abnormal", "gas_leak"]
    results: list[dict[str, Any]] = []
    for name in names:
        scenario = SCENARIOS[name]
        observation_id, ingest_elapsed = post_observation(args.edge_url.rstrip("/"), args.elder_id, scenario)
        result = wait_for_result(args.edge_url.rstrip("/"), args.elder_id, observation_id, scenario, args.timeout, args.wait_llm)
        result["ingest_elapsed_sec"] = round(ingest_elapsed, 2)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    print(json.dumps({"summary": summarize_results(results), "results": results}, ensure_ascii=False, indent=2))
    return 1 if any(result.get("timed_out") for result in results) else 0


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(results),
        "timed_out": [result["scenario"] for result in results if result.get("timed_out")],
        "fallback": {result["scenario"]: result.get("fallback_steps", []) for result in results if result.get("fallback_steps")},
        "denied_actions": {
            result["scenario"]: [
                action
                for action in result.get("action_statuses", [])
                if action.get("status") != "accepted"
            ]
            for result in results
            if any(action.get("status") != "accepted" for action in result.get("action_statuses", []))
        },
    }


if __name__ == "__main__":
    sys.exit(main())
