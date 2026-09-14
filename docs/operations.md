# 测试、追踪与部署

## 测试门禁

安装 requirements.txt 后，默认执行：

```sh
python -m pytest -q
```

默认只选 unit（含 fake client 和临时 SQLite），隔离 .env 与模型凭证，
禁止 socket 出站。它覆盖规则评估代码，但不执行真实模型质量评估。
分类按依赖而非文件名；未标记的新测试默认视为 unit，因此误用网络会失败。

```sh
python -m pytest --suite=integration -q
python -m pytest --suite=e2e -q
python -m pytest --suite=eval -q
```

integration 中 PostgreSQL 测试需要 TEST_DATABASE_URL，MCP 测试需要本地
Server。测试 PostgreSQL 使用独立 schema。e2e 和 eval 会读取运行配置并可能
产生模型、embedding、judge 成本；请使用独立测试配置和测试账户，勿连接真实退款系统。
可用 ECOM_ENV_FILE 指定测试环境文件。不要直接运行旧测试脚本来绕开门禁。

RAG 的 backend_name 已参数化为 numpy/chroma；两者的在线索引测试属于 e2e。
黄金集 CLI 仍在 app/scripts/run_eval.py 和 run_rag_eval.py，改模型、提示词或
检索配置时手动运行，不作为每次提交门禁。

## LangSmith

运行时配置：

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<运行时注入>
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_PROJECT=ecom-production
```

开发、测试与生产使用不同 project。代码使用显式 RunTree，不同时启用 Graph
自动上传完整状态：agent invoke/resume → Graph 节点 → Responses/工具/重试。
摘要、路由和结构化提取等经过 ModelGateway 的调用也会计入当前 trace。

**隐私策略是隐藏内容**：不上传对话、提示词、工具参数、工具返回正文或异常消息；
只上传结构、时长、布尔结果、异常类型、token 和配置中的模型名称。
客户端错误日志也不包含导出异常详情。若未来需要文本调试，应另行设计并验证脱敏策略。

关联标识：
- thread_id：现有会话所有者隔离哈希。
- request_id / business_run_id：业务 request_id 的 SHA-256。
- response_id：模型 response.id 的 SHA-256。
- call_id：工具 idempotency_key（thread_id:call_id）的 SHA-256。
- trace_id / span_id：LangSmith UUID，通过 Graph 事件进入 SSE。

同一业务 run 的审批恢复生成新的活动 trace，使用相同 business_run_id/thread_id
关联；不把几小时人工等待算成模型耗时。resume metadata 中
approval_wait_seconds 是自审批请求发出起的累计等待时间，每次恢复均记录，
不能对多次恢复的该字段直接求和。

LLM outputs.usage_metadata 提供 input_tokens/output_tokens/total_tokens 与缓存命中
token；LangSmith 根据 ls_provider/ls_model_name 识别定价并估算成本。
兼容网关或自定义模型名称必须在平台配置对应价格，缺失 usage 时不伪造零成本。

可重复运行以下命令配置专用看板；默认只打印图表定义，--apply 创建或更新本项目的
Ecom Service 看板，不修改其他看板：

```sh
python -m app.scripts.setup_dashboard
python -m app.scripts.setup_dashboard --apply
python -m app.scripts.check_tracing
```

check_tracing 发送一条不含客户内容的 deployment.smoke 轨迹并读回，不调用模型。
看板包含请求量、活动/模型/工具 P50/P95、模型错误率、工具成功率、token 和成本，
审批等待和重试详情通过对应 trace 的 metadata 查看。指标口径如下：

| 指标 | 过滤与口径 |
|---|---|
| 请求量、活动耗时 P50/P95 | 根 span agent.invoke，resume 单独看 |
| 模型延迟、模型错误率 | run_type=llm；带 error 的次数 / 总次数 |
| 工具成功率、耗时 | name=tool.execute；outputs.success=true 的次数 / 总次数 |
| 重试、超时 | name=tool.attempt；metadata.attempt>1；error=TimeoutError |
| 审批等待 | agent.resume 的 approval_wait_seconds |
| Token、估算成本 | LLM usage；确认模型价格识别正确 |

工具业务失败会设置 ToolBusinessError，不能仅看“有没有抛 Python 异常”。
追踪关闭、导出初始化或上传失败不改变业务结果；退出时最多等待 2 秒刷新缓冲。
实际模型 token 计价仍需通过显式在线测试核验；deployment.smoke 不冒充真实模型调用。

## 工具边界

当前不提供 Shell、Browser、任意文件读写或代码执行工具，不需要 SandboxToolExecutor。
服务内部读知识库/Skill、写 checkpoint/数据库属于固定业务实现。

MCP 默认关闭，启用时必须同时配置精确 URL 和工具名单：

```dotenv
MCP_ENABLED=true
MCP_TRUSTED_SERVER_URLS=["https://trusted.example/mcp"]
MCP_ALLOWED_TOOLS=["query_product"]
```

远端发现的新工具不会自动进入工具集；本地工具仍可用。allowed_tools 进一步限制整个
ToolManager，execute_tool 在特殊分支前也会校验。退款继续由本地授权、审批、幂等服务处理。
MCP 连接失败仍沿用现有本地回退行为；是否允许业务回退应随真实上游接入另行确认。

## 单实例 Docker

```sh
cp deploy/runtime.env.example deploy/runtime.env
chmod 600 deploy/runtime.env
# 编辑配置：填写 OPENAI_API_KEY、AUTH_TOKENS 和 REFUND_APPROVER_IDS
ECOM_IMAGE=ecom-service-agent:v1 docker compose --env-file /dev/null build
ECOM_IMAGE=ecom-service-agent:v1 docker compose --env-file /dev/null up -d
curl --fail http://127.0.0.1:8080/ready
```

AUTH_TOKENS 的结构见 .env.example：高熵 token 绑定 tenant_id/user_id/actor_id，
expires_at 必须是未来时间；审批人还需列入 REFUND_APPROVER_IDS。
不要使用示例凭证。配置通过 env_file 在运行时注入，不进入构建上下文。
生产可由 Secret 管理器生成权限为 600 的 runtime.env；Docker 管理员仍能读取容器环境。

Compose 默认只绑定本机 8080，公网部署需在前方提供 TLS。api 不直接发布端口。
内置 Nginx 按直接连接 IP 限流为 5 req/s、突发 20，超限 429，最大 body 64k。
若前面再加代理，需仅对可信代理配置 real_ip，否则多个用户会共享代理 IP 配额。
不信任客户端身份 headers，使用 Bearer token。SSE 禁用代理缓冲。
另有 MAX_CONCURRENT_RUNS、MAX_REACT_STEPS、CONTEXT_TOKEN_BUDGET；本轮无跨用户计费配额。

api 固定单 worker，UID/GID=10001，只读 root、无额外 capabilities、禁止提权；
仅 app/sessions 数据卷和临时目录可写，不挂载宿主根目录或 Docker socket。
新命名卷继承镜像数据目录所有权；导入既有数据时需确保 UID 10001 可读写。
SQLite 单实例数据放 state 卷。不要为这个配置随意增加 worker/replica。

/health 用于存活，/ready 检查配置和存储，不调用模型，因此不产生模型成本。
容器 HEALTHCHECK 使用 /health；发布成功必须另查 /ready，不能仅凭容器 running 判定。

Uvicorn 直接接收停止信号，停止接受连接并排空请求；lifespan 等待后台任务再关闭存储和
模型连接、刷新追踪。Compose 给 90 秒停止窗口，Uvicorn 请求排空窗口为 60 秒。
卡住的 Python 工具线程不能强制安全中断，超过外层窗口会被终止；写操作仍需依赖幂等和
重启核验，不把超时当作写操作没有发生。

## 数据库与升级

现有数据库初始化包含幂等 schema 创建与兼容列补齐。单实例启动时自动完成；
PostgreSQL 的 schema 初始化使用现有锁。不是新增另一套迁移框架。
升级前检查 schema 改动是否仍兼容上一镜像；破坏性迁移不能通过回退镜像撤销。

SQLite → PostgreSQL 使用现有迁移命令。停止所有写入者，挂载旧数据，
配置 CHECKPOINT_BACKEND=postgres 和 DATABASE_URL：

```sh
python -m app.scripts.migrate_postgres
python -m app.scripts.migrate_postgres --apply --backup-dir /secure/backup
```

第一条只盘点，第二条执行。源数据保留；不要把正在写入的 SQLite 文件简单复制当备份。
常规 SQLite 卷备份可在 docker compose stop api 后做冷备份，并验证恢复到独立卷；
PostgreSQL 使用数据库原生备份与恢复流程。不要用 docker compose down -v 升级。

## CI 和回滚

GitHub Actions 每次 push/PR 执行离线测试、校验 Compose、构建镜像并运行
deploy/smoke.py：检查 readiness、非 root、只读 root、正常停止、重建后的会话存在。
deploy/compose_smoke.py 还会用独立项目和随机本机端口验证 Nginx 网关、健康检查、
Bearer 鉴权、会话创建和 429 限流。两种 smoke 均自动清理各自测试容器和数据卷，
不调用真实模型。push 和手动运行保存带 commit SHA 的镜像 artifact，保留 7 天。
部署前下载并 docker load 该镜像；长期保存当前与上一版镜像到自己的镜像仓库/备份。
当前工作流不持有生产凭证，不自动推送到生产主机。

升级和回退均使用已经验证的镜像版本，避免重新构建旧依赖：

```sh
ECOM_IMAGE=ecom-service-agent:<new-sha> docker compose --env-file /dev/null up -d --no-build --force-recreate api gateway
curl --fail http://127.0.0.1:8080/ready
# 若失败，且数据库仍兼容上一版：
ECOM_IMAGE=ecom-service-agent:<previous-sha> docker compose --env-file /dev/null up -d --no-build --force-recreate api gateway
curl --fail http://127.0.0.1:8080/ready
```

重建 gateway 会刷新 api 的 DNS；数据卷保留。单实例更新存在短暂中断，无自动灰度。
镜像应保存到发行记录，记录对应配置版本与数据库备份；检查 readiness 后再恢复公网流量。

## 本次验证记录

- 默认离线测试：81 passed，58 个 integration/e2e/eval 用例未被选择。
- Compose 配置解析、Python 编译检查和 git diff --check 通过。
- 已配置 LangSmith 项目：deployment.smoke 上传和读回成功；8 张专用看板已保存并读回。
- 没有调用真实模型，实际 API 计费/在线业务效果未验收。
- Docker 28.5.1 本机验收通过：镜像 ecom-service-agent:verification 构建成功。
- deploy/smoke.py 实跑通过：readiness、UID 10001、只读 root、正常停止（exit 0）、
  重建容器后会话仍存在。
- 镜像内断网、只读执行离线测试：81 passed、58 deselected；上游 Starlette/AnyIO
  有一条弃用提醒，不影响通过。
- deploy/compose_smoke.py 实跑通过：网关 health/readiness、Bearer 鉴权、会话创建、
  超限 429。验收专用容器和数据卷已清理，构建镜像保留。
- CI 已包含两种 smoke，尚未触发远端运行。实际模型 API 和线上业务效果仍未验收。

本机可重复执行：

```sh
docker build -t ecom-service-agent:verification .
python deploy/smoke.py ecom-service-agent:verification
python deploy/compose_smoke.py ecom-service-agent:verification
```

Compose smoke 的随机端口覆盖使用 Compose 2.24.4+ 的 !override。
