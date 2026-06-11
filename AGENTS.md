# Elder Guardian Agent Coding Guide

你是本项目的长期维护 Agent，不是一次性补丁脚本。你可以做同风格重构，也应该在必要时敢于跨模块清理设计债，但必须先保护安全闭环、协议稳定性和可复盘性。

本项目是“基于 RK3588 边缘计算的居家老人健康守护与环境协同 Agent 系统”。v2 架构目标是把硬件 IO、规则/小模型编排、显示界面拆开：传感器/执行器/MQTT 通过 `edge-mcp-server` 暴露 MCP/HTTP 语义工具；`guardian-orchestrator` 负责规则触发和多轮小模型 workflow；HMI/dashboard 只做交互和复盘展示。

## 首读顺序

每次修 bug、加功能或重构前，先读：

1. `README.md`：系统目标、启动方式、验收链路。
2. `packages/guardian-shared/guardian_shared/enums.py`、`schemas.py`、`topics.py`：全系统协议源头。
3. `apps/edge-mcp-server/app/main.py`、`apps/guardian-orchestrator/app/main.py`：v2 MCP 桥接和智能执行入口。
4. 如需对照旧 MVP，再读 `apps/guardian-core/app/main.py`。
5. 与任务相关的分层模块：MCP tools、rules、workflow、policy、db、api、前端 app。

不要在没理解事件流和安全边界前直接写补丁。

## 系统分层职责

- `packages/guardian-shared` 是协议层。所有跨服务流转的枚举、Pydantic schema、MQTT topic 都从这里定义，其他模块只复用，不私造平行字段。
- `configs` 是策略和环境配置层。风险阈值、设备策略、MQTT topic 示例、老人画像示例放这里；不要把可配置策略散落到业务代码里。
- `apps/edge-mcp-server` 是传感器、执行器、MQTT、SQLite 和 MCP 的桥接层。它可以执行设备控制和报警，但必须在工具内部做策略门控和审计。
- `apps/guardian-orchestrator` 是智能执行层。它负责规则触发、必要时拆分多轮小模型新对话、调用 MCP/HTTP 语义工具、记录 workflow；它不直接 publish MQTT。
- `rule_engine` / v2 rules 是安全底线层。P0/P1/P2/P3/P4 的硬规则先于 LLM 执行，并且要可解释、可追踪。
- `agent` / workflow 是上下文理解和建议层。小模型每轮只做一件事，fresh conversation；它不能绕过 MCP policy gate。
- `services` 是业务编排层。传感器、视觉、家居、HMI、告警、报表等流程在这里串起来；不要把一个 service 写成全能上帝对象。
- `db` 是持久化层。SQLAlchemy models 和 CRUD 要可复用；所有事件、决策、动作、响应都要能落库复盘。
- `api` 是 HTTP/WebSocket 边界层。API 只做输入校验、调用 service、返回结果，不承载核心业务决策。
- `apps/elder-hmi` 和 `apps/web-dashboard` 是前端体验层。HMI 面向老人，优先大字、清晰、少操作；dashboard 面向家属/开发，优先实时状态和复盘信息。

## 安全红线

- P0 紧急事件不能依赖 LLM 判断，也不允许被 LLM 降级。
- P1 不允许被 LLM 降级到 P3/P4。
- 燃气泄漏时禁止打开风扇、空调等可能带来电气风险的设备。
- 燃气泄漏时只允许安全联动：打开窗户、关闭燃气阀门、启动本地报警、通知家属。
- LLM 不能直接发布 MQTT 控制指令，也不能绕过 ActionPlanner、DevicePolicy、ActionExecutor。
- 非紧急风险优先通过 RK3588 本地 HMI/语音询问老人；老人确认需要帮助或超时无响应时再升级通知家属。
- 所有事件、规则结果、Agent 决策、设备动作、HMI 响应、告警都必须可持久化、可推送、可复盘。

## 开发风格

- Python 使用类型标注，模块顶部保留 `from __future__ import annotations`。
- 跨模块数据优先使用 Pydantic schema；内部轻量计划对象可用 dataclass。
- 枚举值、topic、schema 字段优先从 `guardian-shared` 复用，避免字符串散落。
- 日志覆盖关键流程：接收事件、规则分类、Agent 决策、动作执行、HMI 响应、告警升级、设备 ack/state。
- 保持函数和类职责窄而清晰。新增逻辑先判断它属于 rule、agent、action、service、gateway、db 还是 api。
- 不新增 LangChain、LangGraph、Kubernetes。MVP 先保持透明、可调试、可本地运行。
- 不把 mock 和真实实现混成一团。真实硬件/微信/ASR/TTS/RKLLM 接入应通过现有 gateway/service 接口替换或扩展。
- 前端保持清晰、稳定、可扫读。HMI 不做复杂 UI；dashboard 不做营销页。

## 修改与重构原则

- 可以做大规模同风格重构，但要保持公开 API、MQTT topic、DB 表语义、核心验收链路稳定。
- 发现重复逻辑时，优先抽到对应层：共享协议进 `guardian-shared`，安全规则进 `rule_engine`，处置编排进 `action`，业务流程进 `services`。
- 不要为一个 bug 在多个地方打临时补丁。先找源头：协议不一致、状态机不完整、策略缺失、CRUD 复用不足、还是前端消费格式不稳定。
- 改数据库语义时同时更新 models、CRUD、dashboard state、README/AGENTS 中的说明；如果未来加入迁移工具，再按迁移工具处理。
- 改事件状态时检查 `events/state_machine.py`、HMI timeout、ActionExecutor、dashboard 展示，避免状态只在局部正确。
- 改设备控制时检查 `configs/device_policy.yaml`、`action/device_policy.py`、`action/action_planner.py`、`action/action_executor.py`，不要从 service 或 API 直接 publish MQTT 控制设备。
- 改 Agent 输出时检查 `AgentDecision` schema、LLM prompt、OutputParser、Guardrails、ActionPlanner，确保安全等级和动作建议能闭环。

## 常见变更路线

新增事件类型：

1. 先改 `guardian-shared/enums.py`，必要时扩展 `schemas.py`。
2. 更新相关模拟脚本或 gateway 输入。
3. 在 `rule_engine` 增加可解释规则。
4. 如需自动处置，在 `home_control_rules.py` 和 ActionPlanner 中接入。
5. 更新 dashboard/HMI 展示和 README 验收说明。

新增设备或设备动作：

1. 先改 `DeviceType`、`DeviceAction` 和 `configs/device_policy.yaml`。
2. 更新 `home_control_rules.py` 生成建议命令。
3. 让 ActionExecutor 继续统一执行，不要新增旁路 MQTT publish。
4. 更新 `scripts/simulate_device.py` 的 ack/state 行为。

新增 dashboard/HMI 字段：

1. 先确认后端是否已有持久化数据和 `DashboardMessage`。
2. 更新 `/api/dashboard/state` 或 WebSocket broadcast 数据。
3. 更新 `packages/frontend-shared` 类型。
4. 再改 `apps/web-dashboard` 或 `apps/elder-hmi`。

新增外部真实接入：

1. 保留现有 mock 路径可用。
2. 在 gateway/service 边界新增适配，不破坏核心 schema。
3. 将真实服务失败降级为可记录、可告警的状态，不让核心闭环崩溃。

## Agent 工作流程

1. 先说明你理解的影响面：会碰哪些层、哪些协议和安全边界。
2. 再实施修改：优先改源头协议和核心链路，再改调用者和 UI。
3. 修改后做验证：至少跑语法/构建；涉及安全策略必须跑 CO2、long_static、gas_leak 链路。
4. 最后汇报：说明改了什么、验证了什么、哪些环境限制导致未验证。

## 验收命令

基础静态检查：

```bash
python3 -m compileall packages apps scripts
conda run -n elder-guardian-agent python -c "import fastapi, sqlalchemy, paho.mqtt.client, pydantic, yaml, httpx; print('runtime-deps-ok')"
pnpm -r build
```

后端启动：

```bash
cd apps/guardian-core
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
```

MQTT 端到端模拟需要 Mosquitto：

```bash
docker compose up mosquitto
python scripts/simulate_device.py
python scripts/simulate_sensor.py --event co2_high
python scripts/simulate_vision.py --event long_static
python scripts/simulate_sensor.py --event gas_leak
```

如果本机没有 Docker/Mosquitto，可用 HTTP API 先验证 guardian-core 逻辑，但最终涉及 MQTT 的改动必须在有 broker 的环境补测。

## 不要做的事

- 不要新增绕过 `guardian-shared` 的临时 JSON 字段。
- 不要把安全等级判断写进前端或 LLM prompt 后就算完成。
- 不要让 API、service 或前端直接决定底层设备控制。
- 不要为了局部 UI 展示改变后端协议语义。
- 不要提交 `data/guardian.db`、`dist/`、`node_modules/`、`__pycache__/`、egg-info 等生成物。
