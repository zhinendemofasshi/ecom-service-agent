# P1 实现与验收

## 执行与模型

单 Agent 和 Multi-Agent 均使用 LangGraph。Router、presale、postsale、complaint 是同一张图的节点，共享会话 checkpoint；工具在节点和服务边界检查权限。旧 orchestrator 入口是 Graph 的兼容封装。运行状态仅取自 Graph，包括历史、摘要、短期记忆、结构化回复与消息 schema 版本。旧 JSON / PostgreSQL 会话文档只导入一次，标记迁移后不会因 checkpoint 清理再次恢复旧状态。长期用户记忆独立存储。

模型调用统一经 app/model/gateway.py 使用 Responses，包括路由、结构化提取、摘要、记忆提取及评估。结构化提取使用 schema 指令并经 Pydantic 验证，失败转人工；兼容方法名 chat 不代表使用 Chat Completions。

上下文按 UTF-8 字节数保守估算 token，计入工具 schema 与输出预留。压缩以完整用户轮次为单位，保留工具调用和结果配对；最新完整轮次仍超过预算时停止调用模型。消息 schema 当前为 v2，兼容 v1，拒绝未知版本。

## Guardrails

输入节点在持久化前检查注入、越权及高风险意图并脱敏；工具边界检查 schema、Agent 权限及订单归属。工具返回中的不可信指令隔离。输出检查敏感话术、引用 ID、引用原文、PII，以及退款成功声明的业务依据。流式输出按句暂存并检查后发送，因此有句级延迟。

RAG 引用 ID 由实际检索内容生成，引用不存在或引文不匹配会转人工。这是来源和直接引文真实性校验，不是任意改写的语义蕴含证明。模式检测也不能保证识别所有未知注入或所有自然语言 PII；服务端授权、审批及退款事务仍是不可绕过的执行边界。

投诉及高风险场景写入 human_handoffs。GET /sessions/{id}/handoffs 提供鉴权查询。该队列已持久化，但未连接外部客服工单、邮件或短信服务。

## 审批

API 启动后打开 /approvals，输入 session ID 和审批 Bearer token。页面展示订单、原因、金额及超时，支持逐项批准、修改原因后批准及拒绝；订单号不可改换。凭证只保存在页面内存，编辑草稿在通知刷新后保留。

CLI 命令：
- approve CALL_ID
- edit CALL_ID {"order_id":"ORD-20240115-001","reason":"质量问题"}
- reject CALL_ID 拒绝原因

只有一个待办时可省略 call ID。多个敏感调用可以逐项决策，全部决定收齐后执行，避免重放时重复写入。拒绝原因进入工具结果和最终回复。CLI_ACTOR_ID 须属于服务端审批白名单。

GET /sessions/{id}/approvals 返回待办详情；POST /sessions/{id}/approval 接受 decisions 数组，每项包含 call_id、type，以及可选 arguments、message。GET /sessions/{id} 返回 Graph 会话状态。

API 后台定期处理超时并写入 approval_expired 事件；Web 用 SSE 游标重连及轮询展示通知。独立 CLI 的超时会在操作时校验，阻塞输入期间没有后台弹窗通知。用户取消仍使用 POST /runs/{id}/cancel。

## 保留策略与验证

checkpoint 沿用 CHECKPOINT_RETENTION_DAYS，未结束审批保留；PostgreSQL 长期记忆默认 365 天、已迁移会话归档 90 天，由 API 维护任务清理。SQLite 用于本地开发，长期文档自动清理限 PostgreSQL。

本次离线回归覆盖 API、Graph、工具、RAG 及 P1；真实 PostgreSQL 集成测试使用独立临时 schema，覆盖跨实例恢复、退款事务、审批超时与人工待办。测试使用模拟模型和本地订单，不代表真实模型效果或支付渠道端到端验收。生产需要配置可信身份凭证、审批人及实际客服处理流程。Docker 仍按此前约定保留。
