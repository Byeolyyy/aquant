# QuantAgent Research Room

QuantAgent 是一个面向 PTrade 报告的本地优先 Windows 多 Agent 研究桌面应用。它采用“确定性外层 Harness + 动态 Agent 内层”：Harness 负责解析、权限、预算、审计和终止；统筹 Agent 根据每份报告选择专家、派发任务并形成最终综合。

当前仓库已经完成一个可运行、可治理、可审计的桌面纵向切片：

- Electron + React 三栏桌面界面。
- Python JSONL sidecar 与版本化 IPC。
- PTrade 原始文本的确定性解析和确认预览。
- 5 个默认 Agent：统筹、量化信号、公司与行业、外围市场、风险。
- 根据报告动态选择专家，而不是固定轮询全员。
- 专家并行执行、风险清单汇总、最终统筹消息。
- 暂停、取消和节点边界插话协议。
- SQLite 报告、运行和有序事件持久化。
- 应用内运行记录：查看历史 Run、观测耗时/证据/风险/模型调用、一键重跑和 Markdown 导出。
- 应用内 Agent 管理：可选 Agent 启停、项目级附加要求、配置版本；核心治理角色不可停用。
- 应用内 Prompt 工作台：直接查看完整系统 Prompt，保存草稿、发布、查看历史版本和回滚；平台安全策略只读。
- 每个任务记录实际使用的 Agent 配置、Prompt 版本和子工作流版本。
- 量化信号 demo 子工作流：证券稳定 ID/名称历史、PTrade 确定性规则、逐 Run 信号观察和稳定性统计。
- 外围市场 demo 子工作流：以 A 股报告日为锚点，美股取严格早于报告日的最近交易日，韩股取不晚于报告日的最近交易日，再标准化涨跌并生成五日折线图。
- Agent 生命周期与模型用量事件：保存阶段耗时、Evidence、风险、Token、失败与回退指标。
- 应用内“连接与密钥”设置中心：模型、Tushare 与 Tavily 均可保存和测试连接。
- 公司与行业 Agent 已接入 Tushare、巨潮公告、东方财富新闻与研报，并可使用 Tavily 补充行业资料。
- 外部资料生成 Evidence ID、来源链接、摘要和检索时间，统筹模型只能引用已登记证据。
- API Key 使用 Windows DPAPI（当前用户作用域）加密；界面和协议只返回“是否已配置”。
- 可选 OpenAI-compatible 统筹模型；未配置时自动使用可测试的本地回退能力。

## 快速开始

环境要求：Windows、Python 3.11+、Node.js 22+。

```powershell
cd E:\quant-agent
npm.cmd install
npm.cmd run setup:electron
npm.cmd run build
npm.cmd run dev
```

首次安装时 `setup:electron` 会下载 Electron Windows 运行时。日常开发不需要重复运行。

## 测试

```powershell
npm.cmd run test:python
npm.cmd test
npm.cmd run build
npm.cmd audit --audit-level=high
```

## 应用内配置

启动桌面应用后，点击右上角“连接与密钥”。所有运行配置都从该界面完成，不需要设置环境变量或另开命令行：

- 统筹模型：填写 OpenAI-compatible Base URL、模型名和 API Key。
- Tushare：填写 token，供公司与行业 Agent 查询股票身份、行业、公司信息、估值、财务指标和业绩预告。
- Tavily：填写 API Key，供公司与行业 Agent 补充近期行业资料。
- 每项均可“保存并测试”；修改保存后无需重启 Harness。

配置完成后：

- 统筹模型会从当前报告允许的能力集合中选择需要运行的专家。
- 模型输出必须通过结构化校验；失败会自动回退到本地能力规则。
- 量化事实仍由确定性代码计算，模型不能覆盖解析结果。
- Tushare 与 Tavily 已作为只读工具开放给公司与行业 Agent；外围市场 Agent 当前通过隔离的数据适配器读取公开延迟指数行情。查询失败或权限不足时会逐项标为未知，不会补写事实。

## 本地向量知识库

向量库可以完全放在本机，材料处理也不需要逐份手动 embedding。当前代码保留了自动资料入库 demo，后续主要供公司与行业资料库使用：

```text
实时检索结果 → 内容哈希去重 → 自动切块 → 本地向量化 → SQLite 保存 → 下次运行混合检索
```

当前 demo 使用零额外依赖的 `local-hashing-v1` 特征向量，目的是先验证采集、去重、索引、召回和审计架构；它不是生产级语义模型。后续可以保持相同接口，切换为本机 Ollama embedding 或 SentenceTransformers，并在数据量增大时把向量存储切换到 Qdrant/pgvector。用户只需要在应用内选择允许的数据源、更新频率和模型；建议人工维护的是“可信来源白名单”和小型评测集，而不是手工生成每一条向量。

## 安全边界

- 首版只读取用户粘贴的报告，不连接旧服务器、邮箱或交易接口。
- Renderer 没有 Node 权限，只能通过 context-isolated preload 调用白名单 IPC。
- Renderer 启用 sandbox 与 CSP；IPC 校验发送方，权限请求默认拒绝，外链只允许无凭据 HTTPS。
- Python Harness 不向 Agent 开放 shell、文件写入、任意 Python 或下单工具。
- 密钥由 Windows DPAPI 当前用户密钥加密后保存；不会回显到界面、聊天记录或运行事件。
- 隐藏思维链不保存；只保存结论、结构化详情、任务、状态和错误。
- 输出仅用于研究解读和风险提示，不构成买卖或仓位建议。

## 目录

```text
apps/desktop/                 Electron 主进程、preload、React UI
services/harness/src/         Python 解析器、Harness、模型适配和持久化
services/harness/tests/       Python 单元与动态编排测试
packages/protocol/            跨进程协议说明和后续生成类型入口
docs/ARCHITECTURE.md          详细架构与演进边界
docs/ENTERPRISE_ROADMAP.md    企业级差距、生产化路线与简历表述
```

## 下一里程碑

1. LangGraph/PostgreSQL checkpoint、幂等工具调用与进程崩溃续跑。
2. OpenTelemetry trace/metric/log 导出与质量、成本告警。
3. 离线评测集、Prompt/模型版本对比和 CI 质量门禁。
4. 组织级 SSO/RBAC、审批流、Vault/KMS 与多项目隔离。
5. 签名 Windows 安装包、自动更新、SBOM 和供应链安全扫描。
