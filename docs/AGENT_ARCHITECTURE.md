# aquant Agent 架构说明

> 配套图片：[agent-architecture.png](./agent-architecture.png)（由 `docs/agent_architecture_diagram.py` 生成）
> 更详细的演进边界见 `docs/ARCHITECTURE.md`。

aquant 是一个本地优先的 Windows 桌面应用：把粘贴进来的 PTrade 量化报告转成有证据、可审计的研究简报。系统外层是**确定性 workflow**（解析、权限、预算、审计、终止都由代码控制），内层是**动态专家 Agent**（统筹 Agent 按报告能力组队、并行派单、审阅、追问、综合）。

## 1. 总览 ASCII 图

```text
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 用户粘贴 PTrade 报告                                                             │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ① 桌面端 (Electron + React)                                                      │
│  ┌─────────────────────────┐ ┌────────────────┐ ┌──────────────────────────────┐ │
│  │ React Renderer（沙箱）    │ │ Preload         │ │ Electron Main               │ │
│  │ 三栏研房工作台            │◄┼►│ 仅暴露 4 个桥   │◄┼►│ 窗口/sidecar 生命周期 ·      │ │
│  │ 无 Node · CSP           │ │ request/onEvent │ │ IPC 路由(sender 校验) · 导出 │ │
│  └─────────────────────────┘ └────────────────┘ └──────────────────────────────┘ │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ② 双向 JSONL 进程协议（一行一个 JSON：request / response / event · run_id+seq）    │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ③ Python Sidecar (services/harness/src/quant_agent_harness)                     │
│  ┌───────────────────┐ ┌──────────────────┐ ┌──────────────┐ ┌────────────────┐ │
│  │ ProtocolServer    │ │ Parser           │ │ LLM 客户端    │ │ Agent Prompts  │ │
│  │ (server.py)       │ │ (parser.py)      │ │ (llm.py)     │ │ (版本化管理)    │ │
│  │ RPC 方法分发       │ │ 确定性解析         │ │ 严格 JSON     │ │ 平台策略+角色    │ │
│  └─────────┬─────────┘ └────────┬─────────┘ └──────┬───────┘ └───────┬────────┘ │
│            ▼                    ▼                  ▼                  ▼          │
│  ┌─────────────────────────────────────────────────────────────┐                 │
│  │ Harness 编排核心 (harness.py) —— 外层确定性 workflow           │                 │
│  │  ┌────────────────────────────────────────────────────────┐ │                 │
│  │  │ ① 统筹 Agent：planning 组队 → review 审阅 → synthesis 综合│ │                 │
│  │  └──────────┬──────────────────────────┬─────────────────┘ │                 │
│  │             │ 并行派单 (ThreadPoolExecutor)                  │                 │
│  │  ┌──────────▼─────────┐ ┌──────────────▼───────┐           │                 │
│  │  │ ② 量化信号 Agent    │ │ ② 公司与行业 Agent     │           │                 │
│  │  │ 身份库→正式池+P1/P2/P3│ │ Tushare·公告·新闻·研报 │           │                 │
│  │  │ →稳定性记忆→解释     │ │ ·Tavily 行业补充      │           │                 │
│  │  └────────────────────┘ └───────────────────────┘           │                 │
│  │  ② 外围市场 Agent（交易日口径→美韩日指数→图表）                  │                 │
│  │             ▼ 统筹审阅后                                      │                 │
│  │  ┌──────────────────────┐  ┌──────────────────────────────┐ │                 │
│  │  │ ③ 风险 Agent 逐票检索   │  │ ④ 状态机 + 事件审计              │ │                 │
│  │  │ 负面公告/新闻→利空总结   │  │ draft→…→completed|failed|cancelled│ │                 │
│  │  └──────────────────────┘  │ 追问受 max_rounds/白名单/去重约束 │ │                 │
│  │                            └──────────────────────────────┘ │                 │
│  └─────────────────────────────────────────────────────────────┘                 │
│  ┌─────────────────────────────────────────────────────────────┐                 │
│  │ 只读数据源客户端：GlobalMarketClient · PublicAStockClient     │                 │
│  │                     TushareClient · TavilyClient            │                 │
│  └─────────────────────────────────────────────────────────────┘                 │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ④ 持久化：Repository (SQLite) · secret_store (DPAPI) · local_knowledge (向量库)  │
└───────────────────────────────┬─────────────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ⑤ 外部只读依赖：OpenAI 兼容模型 · Tushare · Tavily · 巨潮/东财 · Yahoo(主)+镜像     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 2. 分层职责

| 层 | 位置 | 职责 |
|---|---|---|
| ① 桌面端 | `apps/desktop/src/renderer`、`src/main` | React 三栏 UI（报告输入/解析确认、运行工作台、历史/治理/提示词/设置）；Preload 只暴露 `request/onEvent/onCrash/platform`；Main 管窗口与 sidecar 生命周期、IPC 路由、Markdown 导出 |
| ② 进程协议 | `src/shared/protocol.ts` + `server.py` | 双向 JSONL：`request/response/event` 各一行，`protocol_version=1`，`run_id+seq` 是界面回放与未来崩溃恢复的稳定游标；Python 日志只写 stderr |
| ③ Python Sidecar | `services/harness/src/quant_agent_harness/` | 领域数据、编排、模型与工具的唯一执行面 |
| ④ 持久化 | `repository.py` 等 | SQLite（WAL）、DPAPI 密钥密文、本地向量知识库 |
| ⑤ 外部依赖 | 只读客户端 | 模型、Tushare、Tavily、巨潮/东财、Yahoo+镜像 |

## 3. Sidecar 内部结构

- **`server.py` ProtocolServer**：读 stdin 逐行分发 RPC，写 stdout 应答与事件；方法包括 `parse_report`、`start_run`、`pause_run/resume_run/cancel_run/steer_run`、`retry_run`、`get_run_snapshot`、`get_settings/save_settings/test_integration`、`get_agents/save_agent_config`、`get_prompt_workspace` 及草稿/发布/回滚、`get_workflows`。
- **`parser.py` Parser**：确定性解析 PTrade 文本 → `valid`（可运行）/ `partial`（需人工确认）/ `invalid`（禁止运行）；缺失字段、未知字段、诊断信息都结构化保留，模型不能改写数字、排序和名单。
- **`harness.py` Harness**：编排核心。
  - `CapabilityRegistry`：按报告结构与启用的 Agent 选“工作泳道”（quant_signal 必选、company_industry 有股票即选、global_market 可配置），模型只能解释/补充选择，不能删减必需证据泳道。
  - `RunControl`：暂停（节点边界）、取消、插话（steering）。
  - `RunPolicy`：`max_rounds`、`max_model_calls`、`max_tool_calls`、`max_external_symbols`、`timeout_seconds` 预算。
  - 执行流：规划 → 专业 Agent 并行（`ThreadPoolExecutor`）→ 统筹审阅（矛盾/重要信息/证据缺口 → `task.replan` 追问**已注册且启用**的现有 Agent，按“Agent+问题+标的”去重，共享 `max_rounds` 上限）→ 风险 Agent 逐票检索 → 统筹二次审阅 → 综合。
  - 事件：每节点发 `agent.lifecycle`、`workflow.plan/node`、`task.plan/replan`、`agent.message`、`model.usage/fallback`、`run.status/completed/error`，全部按 `run_id+seq` 落库并实时推 UI。
- **`llm.py`**：最小的 OpenAI 兼容 Chat Completions 适配器，强制 `json_object`、低温度；`response_format` 不被支持时自动降级重试；任何失败都产生 `model.fallback` 并回退到确定性结果。
- **`agent_prompts.py`**：不可变平台安全策略 + 角色提示词（统筹规划/审阅/综合、量化、公司行业、外围市场、风险）；经 `prompt_templates/prompt_versions` 表做草稿→发布→历史→回滚。
- **`workflows.py`**：三个子工作流定义（quant-signal-subgraph、global-market-subgraph、negative-news-risk-subgraph），节点有 `deterministic/tool/memory/visualization/model_optional` 等类型，运行版本随 Run 记录。
- **数据源客户端**：`integrations.py`（Tushare、Tavily）、`public_sources.py`（巨潮公告、东方财富新闻/研报，免密钥）、`global_markets.py`（Yahoo 主源 + 腾讯/东财/新浪镜像，延迟行情）。全部只读；失败/越权项进入 `unknowns`，绝不虚构。
- **持久化**：`repository.py`（SQLite 表：reports/runs/events/settings/agent_configs/prompt_templates/prompt_versions/security_master/security_name_history/signal_observations/knowledge_documents/knowledge_chunks）、`secret_store.py`（Windows DPAPI 当前用户加密，只存密文）、`local_knowledge.py`（检索→内容哈希去重→切块→向量化→混合召回，当前为 `local-hashing-v1` 架构 demo）。

## 4. 运行状态机

```text
draft → parsing → review_required → planning → specialists_running
      → risk_review → synthesizing → completed | failed | cancelled
```

- 暂停是节点边界暂停；下一里程碑用 LangGraph/SQLite checkpointer 升级为崩溃可恢复。
- 每次运行记录实际使用的 Agent 配置版本、提示词版本与子工作流版本，运行历史不覆盖旧 Run（重跑复用同一份已确认报告、生成新 run_id）。

## 5. 安全边界（关键设计）

- 渲染进程无 Node 权限，只能经 context-isolated 的 Preload 调白名单 IPC；权限请求默认拒绝，外链仅凭证无关 HTTPS。
- Python Harness 不向 Agent 暴露 Shell、文件写、任意 Python 或下单工具；模型只能引用已登记的 Evidence ID。
- 密钥 DPAPI 加密、不落日志/聊天/导出；读取接口只返回“是否已配置”。
- 输出是研究解读与风险提示，不构成买卖或仓位建议。

## 6. 本地生成图片

```powershell
python docs\agent_architecture_diagram.py
```

脚本内置排版自检：所有文本必须落在盒子内、盒子之间不允许意外重叠，否则打印问题清单。
