# PostgreSQL 运行与迁移

本机已在 Homebrew PostgreSQL 15.19 上完成数据库迁移。项目数据库为 `ecom_service_agent`，本地连接使用 `postgresql:///ecom_service_agent`，以当前系统用户通过 Unix socket 连接。`.env` 已切换，未改动原有模型凭证。

## PostgreSQL 模式的范围

`CHECKPOINT_BACKEND=postgres` 会统一启用以下共享存储，不再只切换 checkpointer：

| 状态 | PostgreSQL 表 |
| --- | --- |
| Graph checkpoint、pending writes | `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` |
| checkpoint 归属与保留期 | `agent_threads` |
| API session、run、SSE 游标 | `api_sessions`、`api_runs`、`api_events` |
| 退款申请、审批与审计 | `refund_requests`、`refund_approvals`、`refund_audit` |
| 对话历史、摘要、短期记忆及长期记忆 | `agent_documents` |
| 数据迁移记录 | `storage_migrations` |

API 的用户执行锁、Graph 线程锁以及运行槽位使用 PostgreSQL advisory lock。相同用户的会话在不同 worker 间互斥，避免长期记忆并发覆盖；忙时返回 409，不无限排队。运行容量达到上限返回 429。各 worker 的 `MAX_CONCURRENT_RUNS` 必须一致；本机配置为 8，该值是整个服务的上限。工具线程池仍按进程限制为 32。

每次开始执行前从共享存储刷新会话和记忆，worker 关闭时不把陈旧缓存覆盖回数据库。SSE 从共享事件表轮询，在另一 worker 上产生的事件通常在 1 秒内可见；`Last-Event-ID` 仍可跨 worker/重启回放。取消标记也保存在共享 run 表。

退款事务按租户和用户加事务级锁，金额/次数检查、幂等检查、审批消费和成功审计在同一事务中。连接失效或事务失败会回滚；不自动重试写操作。

## 启动

```dotenv
CHECKPOINT_BACKEND=postgres
DATABASE_URL=postgresql:///ecom_service_agent
MAX_CONCURRENT_RUNS=8
CHECKPOINT_RETENTION_DAYS=30
CHECKPOINT_CLEANUP_INTERVAL_SECONDS=3600
ABANDONED_RUN_SECONDS=300
```

配置登录 token 和审批白名单后启动 API：

```bash
.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 2
```

不同机器的实例使用同一个 PostgreSQL 地址和相同配置。无需共享会话 JSON 目录，但仍须各自部署模型配置、知识库索引及技能定义。生产远程数据库连接按部署环境配置 TLS、凭证和连接数。checkpointer 使用连接池，连接失败有有限超时；每个活跃 run 还会持有若干锁连接，应结合 PostgreSQL `max_connections` 调整 worker 和并发数。

`/health` 检查进程；`/ready` 检查鉴权配置、模型凭证和 checkpoint/API/退款数据库连接。安装数据库不会自动创建登录 token 或审批人，缺少这些配置时 `/ready` 仍返回 503。

## 旧数据迁移

迁移前停止所有 API/CLI 写入方。迁移不会覆盖非空目标，也不会删除源 SQLite 或 JSON 文件。

```bash
# 先查看源数据清单；不写目标数据库
.venv/bin/python -m app.scripts.migrate_postgres

# DATABASE_URL 指向已创建的专用空数据库
.venv/bin/python -m app.scripts.migrate_postgres --apply
```

脚本会：

1. 使用 SQLite backup API 生成一致的备份，复制关联会话和记忆 JSON，并保存源路径清单。
2. 在 PostgreSQL 初始化所需表及 LangGraph schema。
3. 在一个事务内复制 API、审批、审计、归属记录、会话/记忆文档；通过 checkpointer API 转换 checkpoint、父链及 pending writes。
4. 恢复审计表 sequence，记录源指纹。相同源重复执行返回 `already_applied`；源已变化时拒绝覆盖 PostgreSQL 新状态。

本机已导入 **14 个线程、98 个 checkpoint**。源 API 会话/事件与退款数据为空。备份位于 `app/sessions/backups/postgres-20260913T142042726400/`，包含 SQLite 快照和 `manifest.json`，目录已被 Git 忽略。

迁移中的 `running` run 会标为失败，不自动重放可能已产生业务副作用的调用。待审批 checkpoint 保留，可在另一 worker 恢复。切回 SQLite 是连接原始旧数据，并不是反向同步 PostgreSQL 后续产生的新数据。

## 生命周期维护

PostgreSQL API 启动时、以及每个清理周期执行维护，也保留 `POST /admin/checkpoints/cleanup`。

- 清理超过保留期且状态为 `completed` 的线程。与运行使用同一线程锁，锁内重新核验状态和时间，再在一个事务中删除 checkpoint、blob、write 和归属元数据。活跃/待审批线程不会被清理。
- 超过 `ABANDONED_RUN_SECONDS` 的 `running` run，只有拿到其用户执行锁后才标记失败并记录 `run_failed` 事件。仍持锁的长任务不会被误杀；进程崩溃不会触发退款自动重试。
- 定时维护异常写入服务日志，下一周期再执行；服务停止时先等待维护与在途任务退出，再关闭数据库资源。

## 验证

```bash
CHECKPOINT_BACKEND=sqlite TEST_DATABASE_URL=postgresql:///ecom_service_agent \
.venv/bin/python -m pytest tests/test_api.py tests/test_production_runtime.py \
  tests/test_langgraph_runtime.py tests/test_tool_execution.py \
  tests/test_product_search.py tests/test_rag_evaluation.py tests/test_postgres.py -q
```

**48 项相关测试通过，其中 9 项为真实 PostgreSQL 集成测试**，包含独立进程的审批交接。`git diff --check` 与 Python 编译检查通过。

`CHECKPOINT_BACKEND=sqlite` 在测试进程中隔离默认模块配置；PostgreSQL 专项测试显式使用 `TEST_DATABASE_URL`，每项创建自己的随机 schema，结束时只删除该 schema。测试覆盖重启恢复、跨 worker 审批、并发事件游标、退款并发幂等/每日限额/回滚、连接锁、保留期清理及旧数据迁移。

Docker 和真实订单/支付系统接入仍单独保留；PostgreSQL 迁移不代表已连接支付渠道。
