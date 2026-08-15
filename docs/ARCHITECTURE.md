# aquant 架构说明

## 1. Agent 与 Workflow 的边界

系统外层是确定性 workflow，确保输入必须经过解析确认、每轮存在预算且一定结束。内层是多 Agent：统筹模型根据报告内容和已启用的能力注册表安排职责；量化与风险汇总不可绕过，公司行业和外围市场可由项目配置启停。

```text
用户粘贴报告
    │
    ▼
确定性解析器 ── invalid → 阻止启动
    │ valid / 人工确认 partial
    ▼
统筹 Agent ── 安排专家并创建任务
    │
    ├── 量化信号 Agent（身份库 → 确定性计算 → 稳定性记忆 → 解释）
    ├── 公司与行业 Agent（Tushare、巨潮公告、东方财富新闻与研报）
    └── 外围市场 Agent（交易日口径 → 美韩指数 → 涨跌标准化 → 图表）
    │
    ▼
统筹 Agent 审阅 ── 发现矛盾/重要信息/证据缺口 → 再次调用现有 Agent（最多 max_rounds 轮）
    │
    ▼
风险 Agent（逐票检索报告日前的潜在利空）
    │
    ▼
统筹 Agent 二次审阅 ── 必要时再次调用现有 Agent
    │
    ▼
统筹 Agent 综合 → 研究解读与风险提示
```

配置真实模型后，统筹 Agent 自主选择允许的专家，并在子 Agent 返回后检查事实矛盾、重大信息、证据强度、未知项和查询失败。它只能重新调用当前已注册且启用的 Agent，不能创造新角色；Harness 会过滤无效任务、重复追问和越权 Agent，并用 `max_rounds` 保证运行结束。

## 2. 进程边界

- Electron Main：窗口生命周期、Python sidecar 生命周期、IPC 路由。
- Electron Preload：仅暴露 `request/onEvent/onCrash/platform`。
- React Renderer：纯 UI，不持有文件系统或 Node 权限。
- Python Sidecar：领域数据、编排、模型和工具的唯一执行面。

通信使用一行一个 JSON 的双向协议：

```json
{"type":"request","protocol_version":1,"request_id":"...","method":"parse_report","payload":{}}
{"type":"response","protocol_version":1,"request_id":"...","ok":true,"result":{}}
{"type":"event","protocol_version":1,"event":{"run_id":"...","seq":1,"kind":"agent.message"}}
```

`run_id + seq` 是界面重放和未来崩溃恢复的稳定游标。Python 日志只能写 stderr，避免破坏 stdout JSONL。

## 3. 当前运行状态

```text
draft
  → parsing
  → review_required
  → planning
  → specialists_running
  → risk_review
  → synthesizing
  → completed | failed | cancelled
```

当前 pause 是节点边界暂停；下一里程碑会用 LangGraph SQLite checkpointer 把状态升级为进程崩溃后可恢复。

每个专业 Agent 和风险 Agent 都会发出 `agent.lifecycle` 开始/完成/失败事件；模型调用发出 `model.usage` 或 `model.fallback`。运行记录页据此展示耗时、参与 Agent、证据、来源、风险、Token、回退和错误指标。

## 4. 数据真实性

- `null`、`false` 和 `0` 在 Pydantic 模型中保持不同值。
- `selected/near` 只代表上游来源池，不推断正式池或候选池。
- 量化正式观察只接受 `all_conditions_met`；near 标的按 P1/P2/P3 三档候选规则确定性归类，模型不能修改数字、排序和名单。
- 解析状态：`valid` 可运行；`partial` 需人工确认；`invalid` 禁止运行。
- 外部事实必须关联 Evidence ID；证据保存来源类型、标题、URL、摘要、发布日期、检索时间和关联标的。
- Prompt 只定义查询目标和归纳约束，不被视为联网能力；联网结果必须来自注册的只读客户端。
- Tushare 不同接口权限不同，股票基础、公司信息和每日指标允许部分成功；失败项进入 `unknowns`。
- Tavily 只用于公司与行业 Agent 的行业补充查询，控制额度并保留原始来源。
- 外围市场 Agent 固定展示五个核心指数并以 A 股报告日期为时间锚点：美股选择当地日期严格早于报告日的最近有效交易日，韩股选择不晚于报告日的最近有效交易日；周末或休市自动向前回退。每个指数保存自己的交易日和市场时区，延迟行情失败时只能展示醒目标注的 demo 数据。
- 当前无密钥行情适配器只用于产品 demo；生产环境必须切换到具有授权、SLA 和明确延迟口径的行情源，并对超过 12% 的指数单日涨跌执行第二来源复核。
- 巨潮与东方财富客户端不需要用户密钥，采用只读、限时、部分失败可继续的公共查询。
- 模型输出失败、越权或缺少必需字段时自动回退到确定性结果，并产生 `model.fallback` 事件。

## 5. 持久化

当前 SQLite 表：

- `reports`：原文、哈希、解析状态和完整结构化报告。
- `runs`：运行状态、报告关联和最终综合。
- `events`：按 `run_id + seq` 排序的完整运行轨迹。
- `settings`：非敏感应用配置，例如模型 Base URL 和模型名。
- `secrets`：仅存 Windows DPAPI 当前用户作用域加密后的密文，不存明文。
- `agent_configs`：Agent 启停、项目级附加要求、配置版本和更新时间。
- `prompt_templates/prompt_versions`：Prompt 身份、草稿、发布版本、历史版本和回滚审计。
- `security_master/security_name_history`：证券稳定身份、当前名称、别名和名称有效期。
- `signal_observations`：每个 Run 的信号等级、关键字段、缺失数和规则版本。
- `knowledge_documents/knowledge_chunks`：去重后的资料、切块、本地向量、模型名和证据元数据。

默认桌面数据目录由 Electron 通过 `QUANT_AGENT_DATA_DIR=app.getPath('userData')` 传给 sidecar。开发测试可把该变量指向工作区 `.data` 或临时目录。

运行历史不会覆盖旧 Run。重新运行会复用同一份已确认报告创建新的 `run_id`，保留可比较的审计轨迹。

## 6. Agent 治理

- 统筹、量化信号、风险是核心治理链，桌面协议拒绝停用。
- 公司行业与外围市场可按项目启停；Capability Registry 只向统筹模型暴露已启用能力。
- 专业结果和风险结果之后都设置统筹审阅检查点；统筹通过 `task.replan` 事件再次调用现有 Agent，追加任务必须包含具体问题、标的和原因。
- 相同 Agent、标的和问题不会重复派发；所有追问轮次共享 `RunPolicy.max_rounds` 上限。
- 附加要求进入结构化任务上下文，但不能扩大工具 allowlist，也不能覆盖确定性量化规则。
- 每次保存配置都会增加 `config_version`，新 Run 使用最新版本。
- 有效 Prompt 始终按“不可变平台策略 + 已发布角色 Prompt + 统筹任务”的顺序组装；外部网页和公告一律视为不可信资料，不能覆盖身份或权限。
- 草稿不影响运行；发布只影响之后创建的 Run；回滚通过创建一个新版本完成，不篡改旧版本。
- 量化和外围市场 Agent 已绑定独立子工作流定义，并通过 `workflow.plan/workflow.node` 事件暴露节点、状态和耗时。

## 7. 本地知识与向量化

市场资料的入库链路由代码自动完成：内容哈希去重、定长重叠切块、批量向量化、SQLite 持久化和增量召回。当前 `LocalHashingEmbedder` 是无需下载模型的架构 demo，支持中文 n-gram、字母数字 token、余弦分数、词项重叠和标的加权；它只能证明工程闭环，不等同于神经语义 embedding。

生产升级保持 `embed(text) -> vector` 契约不变：

1. 单机隐私优先：Ollama embedding 或 SentenceTransformers。
2. 小规模：SQLite 文档元数据配合本地向量索引。
3. 企业规模：Qdrant/pgvector，增加租户过滤、稀疏 + 稠密混合检索、重排和索引备份。
4. 采集侧增加来源白名单、robots/速率限制、增量游标、失败重试和文档保留策略。
5. 用人工维护的小型查询—证据评测集衡量 Recall@K、nDCG、时效性和错误引用率。

## 8. 后续兼容性约束

- Tool Registry 必须使用 Pydantic 输入输出、明确只读/写入级别、超时、缓存和证据转换器。
- Tushare、Tavily、模型 Key 由 Windows DPAPI 当前用户密钥加密；SQLite 仅保存不可直接使用的密文，不写日志、聊天事件或导出包。
- 配置只通过桌面设置协议读写；读取接口仅返回配置状态，不返回密钥明文。
- 旧服务同步通过受限用户和 forced-command RPC；不向桌面端授予 `ubuntu` shell。
- 自动交易不属于当前产品范围，任何未来写操作都必须另设人工批准边界。
