# P0 核查与补齐记录

核查范围：默认 LangGraph 单 Agent；PostgreSQL 共享存储、多 worker 协调和本机迁移已补齐，Docker 继续暂缓。保留本次核查前已有的工作区改动。

| 项目 | 核查结果 |
| --- | --- |
| Responses token streaming | 原来已有 runtime 流式调用；补上 CLI event sink，实时显示文本 delta。 |
| 运行事件 | model_started / token_delta / tool_started / tool_completed / approval_required / run_completed / run_failed 已有实现；补上 Graph 失败状态与失败事件、API run 事件关联。 |
| SSE、断线与游标 | 原有 SQLite 持久事件回放；补上 Last-Event-ID、游标错误/过期响应、缓存禁用、Python 自动重连客户端。POST 断线不释放仍在运行的工作槽位。 |
| checkpoint | 已有租户/用户/session 作用域、已完成线程保留期清理；补上禁止 touch 更换所有者。PostgreSQL 连接池、数据库锁、定期清理及真实连接/恢复/迁移测试已补齐，见 [迁移记录](postgres.md)。 |
| 身份与订单归属 | 默认使用服务端配置的有效期 Bearer 身份映射；客户端不能用身份头切换用户。旧网关身份头模式默认关闭。订单拒绝缺失或不匹配的 tenant/user。长期记忆目录增加租户作用域。 |
| 退款安全 | 在服务端新增审批表、pending 状态原子转换、审批人白名单、执行时审批核验及有效期检查。已有金额/每日次数限制、幂等键、订单重复保护及事务保留。申请、审批、拒绝、超时尝试、执行拒绝、成功与重放写入审计。 |
| 取消与审批超时 | 待审批取消直接持久化拒绝并结束 Graph，不调用模型；已过期审批不能决定或执行退款。执行中的取消是协作式，不能撤销已经提交的业务事务。 |
| 工具可靠性 | 原有 timeout/backoff/circuit breaker/输入输出大小限制；补上不可重试异常分类、只重试明确暂态错误和全局执行容量限制。超时任务真正退出前持有槽位；写操作始终不自动重试。 |
| API | 已有 sessions/messages/events/health/ready；补上按 run approve/reject，待审批 cancel，审批 run 状态和并发保护。 |

## 验证

最初离线回归结果：**39 passed**。PostgreSQL 补齐后的相关回归：**48 passed**（含 9 项真实 PostgreSQL 集成测试），命令见 [PostgreSQL 验证](postgres.md)。`git diff --check` 通过。

离线回归命令：

```bash
.venv/bin/python -m pytest tests/test_api.py tests/test_production_runtime.py tests/test_langgraph_runtime.py tests/test_tool_execution.py tests/test_product_search.py tests/test_rag_evaluation.py -q
```

覆盖 API 身份不可伪造/过期、run 审批权限与重复请求、取消、SSE 重启回放与 Last-Event-ID、过期游标、客户端自动重连、POST 断线后台执行、真实 Graph 到退款服务的批准/取消、直接调用服务绕过审批、审批过期、并发幂等提交、工具超时及容量、读写重试差异、永久错误不重试、checkpoint 所有权/清理。

核查开始时运行全量 pytest：43 passed / 37 failed / 4 errors。失败包含真实 OpenAI/MCP 连接不可用、旧脚本式测试断言；4 项 RAG 测试缺少 backend_name fixture。因此本次不宣称全量测试通过，也未调用真实支付渠道验收。

## 暂缓与部署边界

- PostgreSQL 已在本机 15.19 完成迁移与集成验证；Docker 部署仍暂缓。
- PostgreSQL 模式下，API session/run/event registry、退款/审批、会话/记忆文档已共享，执行锁和全局运行容量由数据库协调；支持多个 worker。SQLite 回退模式仍使用 `--workers 1`。
- 订单来自 `mock_data.ORDERS`，退款是本地事务提交申请；接入真实订单和支付服务后仍需支付幂等、回调对账与渠道验收。
- `AUTH_TOKENS` 提供服务端身份验证入口，不包含密码登录/第三方 IdP。部署时由可信身份系统或管理员配置高熵凭证及有效期；退款审批人还须配置 `REFUND_APPROVER_IDS`。
- 审批有效期在服务端决定和执行时强制核验；未触碰的过期 pending 记录不会自动推送超时通知。checkpoint 清理由管理接口及 PostgreSQL API 的定期维护任务触发。
- 进程崩溃不自动重跑正在执行的 run；定期维护会将失去执行锁且超时的 run 标为失败。已持久化事件和待审批 checkpoint 可跨 worker 恢复，写操作仍按幂等键核对业务结果。
