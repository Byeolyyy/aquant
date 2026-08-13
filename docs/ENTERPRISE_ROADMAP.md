# QuantAgent 企业级演进路线

## 1. 当前已经可以真实演示的能力

本项目已经不是“多个 Prompt 依次调用”的简单 Workflow。当前可演示的工程闭环包括：

- 确定性 Harness 掌控流程、权限、数据校验、风险复核与终止；模型只能在允许范围内规划和归纳。
- 量化、公司行业、外围市场并行执行，统筹 Agent 负责派单与综合，风险 Agent 只汇总风险和待核验项。
- PTrade 正式池及 P1/P2/P3 候选规则由代码确定性计算，模型不能修改名单、排序和数字。
- Tushare、Tavily、巨潮公告、东方财富新闻/研报转换为带 Evidence ID 的统一证据模型。
- SQLite 事件溯源保存 Run、任务、Agent 消息、证据、模型用量、失败回退和最终结果。
- 桌面端可查看运行记录、执行指标、完整消息，支持相同报告重跑和 Markdown 导出。
- Agent 治理页面支持可选角色启停、附加要求和配置版本；核心治理角色不可绕过。
- API Key 使用 Windows DPAPI 加密；Electron 采用 sandbox、contextIsolation、CSP、IPC 白名单和 HTTPS 外链限制。

## 2. 与真正企业级平台仍有的差距

### P0：可靠执行与恢复

当前运行线程仍在单个 Python sidecar 内。应用进程被终止后，事件虽然存在，但运行不能从中间节点继续。

应补充：

- 将每个工作节点变成可重放的状态机步骤，使用 LangGraph checkpointer 或自研 checkpoint 表。
- 为工具调用增加 `idempotency_key`、超时、指数退避、最大重试、熔断与降级策略。
- 将 API、编排 Worker、工具 Worker 分离；生产部署使用 PostgreSQL 保存状态，Redis/NATS 只承担队列与事件分发。
- 增加 dead-letter queue、卡死 Run 扫描器和人工恢复入口。

验收标准：在任意 Agent 完成后强制杀掉 Worker，重启后不重复外部调用，并从最后 checkpoint 继续。

### P0：评测与质量门禁

企业 Agent 的核心不是“能回答”，而是“版本升级后质量不会悄悄下降”。

应补充：

- 建立脱敏 PTrade 黄金数据集，覆盖正常、缺字段、异常结构、无外部资料和风险公告等场景。
- 对量化名单正确率、Evidence 引用有效率、幻觉率、风险召回率、输出完整度设置自动评分。
- Prompt、模型、工具和解析器都要有独立版本；支持基线与候选版本离线对比。
- CI 中设置质量门禁，低于阈值禁止发布。

验收标准：每次 Prompt 或模型变更都能输出可比较的评测报告，并定位退化样本。

### P1：完整可观测性

当前已有本地 Run 指标与生命周期事件，下一步应映射到标准遥测体系：

- 一个 Run 对应一个 trace，Agent、模型与工具调用对应 span。
- 指标包括端到端延迟、各 Agent P50/P95、模型 Token/成本、工具成功率、证据覆盖率和回退率。
- 日志只记录结构化结论与错误，不记录 API Key、原始思维链和敏感报告全文。
- 通过 OpenTelemetry Collector 导出到 Grafana/Tempo/Loki 或企业现有 APM。

验收标准：可以从一次失败 Run 跳转到具体 Agent、模型调用和工具异常，并设置告警。

### P1：安全与组织治理

桌面单用户模式已有本地安全边界，但组织部署还需要：

- OIDC/企业 SSO、RBAC、项目空间、数据行级隔离和管理员审计。
- 密钥迁移到 Vault/KMS，短期凭据自动轮换，工具权限按角色和项目发放。
- 对网页内容做 Prompt Injection/恶意指令隔离：网页只能作为不可信数据，不能成为系统指令。
- 高风险工具使用人工审批，审批记录与输入摘要写入不可篡改审计日志。
- 数据保留、删除、脱敏、导出和跨境策略。

验收标准：不同项目用户不能互相读取 Run/证据；所有敏感操作都能追溯到人和策略版本。

### P1：模型与工具网关

- 统一多模型 Provider，支持路由、超时、Fallback、预算、速率限制和成本核算。
- 工具使用统一 Schema Registry，记录输入/输出哈希、权限、缓存、来源许可与 SLA。
- 公共站点查询增加缓存、去重、robots/条款检查和官方来源优先级。
- 证据内容建立可信度、发布日期、抓取时间、内容哈希和失效状态。

### P2：产品与交付

- 报告模板、PDF/Word 导出、团队评论、任务指派和审批。
- 签名 Windows 安装包、自动更新、灰度发布、回滚、崩溃收集。
- CI/CD、依赖锁定、SAST/DAST、依赖漏洞扫描、SBOM 和制品签名。
- 服务端模式支持多用户 Web 控制台，同时保留桌面端作为研究员客户端。

## 3. 推荐的生产架构

```text
Desktop / Web
    │ OIDC + HTTPS + SSE
API Gateway / RBAC / Audit
    │
Run Service ── PostgreSQL（Run、Checkpoint、配置、证据元数据）
    │
Durable Queue ── Orchestrator Workers ── Agent/Tool Workers
                                      ├── Model Gateway
                                      ├── Tushare / 公告 / 搜索
                                      └── Evidence Object Storage
    │
OpenTelemetry Collector ── Trace / Metric / Log / Alert
```

桌面版继续作为本地优先模式；企业服务端模式复用同一份领域模型、Prompt、工具 Schema 和评测集。

## 4. 推荐实施顺序

1. 先做 checkpoint、幂等工具调用和崩溃恢复。
2. 同时建立 50～100 份脱敏黄金报告及自动评测。
3. 接入 OpenTelemetry 和成本/质量面板。
4. 再拆分 API/Worker/队列并迁移 PostgreSQL。
5. 最后增加 SSO/RBAC、Vault、团队协作和签名发布。

## 5. 可写进简历的表述

以下内容已经在当前项目中实现，可以如实描述：

- 设计并实现本地优先的 A 股多 Agent 研究桌面平台，采用 Electron/React + Python sidecar + SQLite 事件溯源架构。
- 构建确定性 Harness 与多 Agent 协作机制，实现动态派单、并行专家分析、Evidence ID 证据约束、独立风险复核和模型失败回退。
- 将 PTrade 资金与盘口规则实现为确定性计算层，避免 LLM 修改关键数字和候选排序。
- 接入 Tushare、Tavily、巨潮及东方财富等只读数据源，支持部分失败、来源追踪、时效检查和风险关键词审查。
- 实现 Agent 配置治理、版本化附加指令、运行历史、重跑、Markdown 导出和耗时/证据/风险/Token 指标。
- 使用 Windows DPAPI、Electron sandbox/contextIsolation/CSP、IPC allowlist 等机制保护密钥和桌面权限边界。

完成下一阶段后才适合加入的表述：

- 基于 LangGraph/PostgreSQL/Redis 的分布式持久执行与崩溃恢复。
- 基于 OpenTelemetry 的全链路追踪与生产告警。
- 基于黄金数据集和 CI 门禁的 Agent 自动评测体系。
- 企业 SSO/RBAC、多租户隔离与 Vault/KMS 密钥治理。
