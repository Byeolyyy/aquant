# Protocol package

跨进程协议当前以 `apps/desktop/src/shared/protocol.ts` 和 Python `ProtocolServer` 为实现来源。

下一阶段将在这里加入规范 JSON Schema，并由同一份 schema 生成 TypeScript 与 Pydantic 类型，避免两端手写类型漂移。协议变更必须提升 `protocol_version`，并保留不兼容版本的明确错误响应。

