# 安装与部署

本文档描述 RecruitOps Agent 的独立本地部署。正常运行只需要本项目、Agent
配置和 Agent-owned PostgreSQL；旧秋招系统只在一次性迁移时作为只读输入。API
是唯一的 HTTP 入口，Compose 不额外启动 frontend 容器，API 应用负责静态入口
和 `/api/*` 路由。

## 前置条件

- Docker Desktop，且 Docker Compose v2 可用。
- Python 3.11 或更高版本，用于本地脚本、测试和一次性迁移。
- 如需真实抓取，目标招聘网站可访问；离线验证不需要外网或模型密钥。

## 快速启动

在克隆后的项目根目录执行：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:8010/ready
python scripts/verify_runtime.py
```

Compose 启动 `pgvector/pgvector:pg16` 和 API。API 映射到
`http://127.0.0.1:8010`，PostgreSQL 只绑定宿主机回环地址的 `5433` 端口，
数据保存在命名卷 `recruitops-postgres`。API 启动前会执行
`python scripts/apply_migrations.py`。

也可以在本机虚拟环境直接启动 API：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python scripts/apply_migrations.py
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop_local.ps1
```

停止服务时使用 `docker compose stop` 或 `docker compose down`。不要使用
`docker compose down -v`，该命令会删除 PostgreSQL 数据卷。

## Agent 配置

Agent 的运行配置位于仓库内：

- `config/companies.yaml`：公司、招聘 URL、crawler key 和接入状态。它是独立
  crawler core 的正式目录，不再从旧 `config.yaml` 运行时读取。
- `config/candidate_profile.yaml`：结构化候选人画像，供确定性筛选和匹配使用。
- `config/rag_sources.yaml`：批准的 Crawler/ATS 知识来源清单。
- `.env`：本机数据库、API、邮件和可选模型配置；该文件不提交。

正常运行不需要 `RECRUITOPS_SOURCE_ROOT`。这个变量仅供一次性 legacy importer
以及保留的兼容脚本使用。容器只读绑定 Agent `config/`，不挂载旧秋招系统目录。

宿主机脚本使用 `127.0.0.1:5433`，容器内 API 使用服务名 `postgres:5432`。
不要把带宿主机 `localhost:5433` 的 URL 直接传给容器。不要把密码、模型密钥或
邮箱授权码写入 Dockerfile、Compose、文档、前端、镜像层或 CI 日志。

`RECRUITOPS_CHECKPOINT_MODE` 默认为 Compose 的 `postgres`；本地开发可设为
`memory`。只读 API、独立 crawler、每日确定性筛选和离线评测不要求模型密钥。

## Codex、MCP 与写边界

健康和就绪端点为 `GET /health` 与 `GET /ready`。`/ready` 检查 Agent 配置、
独立 crawler import 和 PostgreSQL，不检查旧源目录。

Codex App Server 固定为 `@openai/codex@0.149.0`，由本地 supervisor 以 stdio
JSONL 子进程启动。FastAPI 只提供本地 BFF：

- `GET /api/codex/health` 和 `GET /api/codex/traces`：运行状态与脱敏事件追踪。
- `POST /api/codex/threads`、`GET /api/codex/threads` 和
  `GET /api/codex/threads/{thread_id}`：创建、分页读取和读取 thread。
- `POST /api/codex/threads/{thread_id}/resume`：恢复 thread。
- `POST /api/codex/threads/{thread_id}/turns`：启动 turn；`/turns/stream` 以
  SSE 投影 App Server 事件。
- `POST /api/codex/threads/{thread_id}/interrupt`：请求中断；
  `GET /api/codex/threads/{thread_id}/events`：订阅事件。

MCP 工具协议版本为 `15`。完整诊断目录包含 34 个工具，其中 25 个只读；默认 Codex
会话只暴露 `MCP_AGENT_TOOL_NAMES` 中的 26 个业务级工具；完整目录及只读/副作用分类以
`packages/mcp/server.py` 的 `MCP_TOOL_NAMES`、`MCP_READ_ONLY_TOOL_NAMES` 和
`MCP_ACTION_TOOL_NAMES` 为准。模型通过 MCP 调用招聘能力，不通过旧任务或助手 HTTP
路由调用。

浏览器与审批边界：

- `POST /api/browser/observations`：接收用户授权的被动、脱敏浏览器证据；它不
  导航、不点击，也不写数据库。
- `POST /api/browser/application-captures`：对当前页面生成“记录已投递”的审批
  预览，不直接执行写入。
- `WebSocket /browser-bridge`：Edge 扩展主动建立经 challenge/HMAC 认证的持久连接，
  服务端主动推送操作，扩展返回 ACK、进度和脱敏结果。
- `GET /api/approvals`：读取本机审批队列；`POST /api/approvals`：创建审批预览。
- `POST /api/approvals/{token_id}/approve` 和
  `POST /api/approvals/{token_id}/reject`：批准或拒绝审批。
- `POST /api/approvals/{token_id}/execute`：在审批、Bearer 认证和写开关均满足
  时执行精确绑定的业务写操作。

浏览器状态复核由 MCP 的 `edge_connection_status`、
`observe_application_status_page`、`verify_application_status_evidence`、
`browser_operation_status` 和 `cancel_browser_operation` 负责。旧的任务领取、轮询和投递状态 review HTTP
路径不属于当前协议；仍保留被动 evidence 接口与通用 approval。

扩展先采集脱敏后的语义 DOM、ARIA 状态和必要视觉属性；批量复核和定时投递复核
都固定只做 DOM-only 观察。只有交互式助理已经针对单条申请完成 DOM-only 观察，明确判断
“页面无结构化证据、但截图文字可能有状态”时，才以 `include_vision=true` 和
`vision_fallback_reason=no_structured_evidence_visible_status_likely` 请求当前可见页截图。
图片只在必要的单页回退中发送到 DeepSeek 视觉接口，返回结构化结果；操作、页面、申请和
来源绑定仍须保留，状态写入仍须通过同一个证据校验器。空白页、超时、登录墙、CAPTCHA
或不明确目标不发送图片，也不解验证码。默认视觉型号是 `deepseek-flash`，复用
`RECRUITOPS_LLM_API_KEY`；官方视觉名称 `deepseek-v4-flash-vision-exp` 仅作为兼容选项，
不是本地模型。

2026-09-13 已对官方接口做两次无写入图片实测：`/models` 列出 `deepseek-flash` 和
`deepseek-v4-pro`，`deepseek-v4-flash-vision-exp` 调用成功但返回型号为
`deepseek-flash`；原型号 `deepseek-flash` 直接接收 `image_url` 并准确读出 nonce
`VX73-Q9`。两次测试合计 465 tokens，证据保存在
`.data/evals/vision-removal-20260913/flash.json` 和
`.data/evals/vision-removal-20260913/vision-exp.json`。更新代码后需在
`edge://extensions` 重新加载版本 `0.3.18` 的扩展。

除公开健康检查外，审批管理接口在配置 `RECRUITOPS_API_TOKEN` 后使用
`Authorization: Bearer <token>`。Compose 中
`RECRUITOPS_WRITE_ENABLED` 默认值为 `false`；只有本机 `.env` 显式设为 `true`
时才开启审批业务写入。
每日招聘流水线的正常写入目标是 Agent-owned PostgreSQL，不是旧系统数据库。
只有本机实验在配置非空 `RECRUITOPS_API_TOKEN`、人工审批和
`RECRUITOPS_WRITE_ENABLED=true` 后，才允许消费 Agent 业务写令牌。

需要让本地持久化计划在 API 进程内到期执行时，同时设置：

```dotenv
RECRUITOPS_AUTOMATION_ENABLED=true
```

API 会在启动时恢复被异常中断的执行记录，并由后台 worker 轮询 PostgreSQL 中
处于启用状态的计划。计划、执行历史和业务快照都保存在命名卷中，容器重建不会
清空这些数据。

## 每日流水线

先用 dry-run 查看任务计划：

```powershell
python scripts/run_local_task.py --task daily_recruitment_intelligence --dry-run
```

固定任务的实际标识和手动运行方式：

```powershell
python scripts/run_local_task.py --task daily_recruitment_intelligence
python scripts/run_local_task.py --task crawler_health
python scripts/run_local_task.py --task application_progress
python scripts/run_local_task.py --task recruitment_mailbox
```

每日招聘任务读取 `config/companies.yaml`，并行调用连接的
`packages/recruitment_core` crawler，校验分页、来源、具体岗位链接、完整 JD
和届别，再按内容指纹做新增/变化/复用比较，最后在一个 Agent PostgreSQL
事务中写入公司、岗位和分析结果。只有 `cohort=2027` 且
`cohort_status=confirmed` 的完整 JD 可进入分析；实习和博士限定岗位不会进入
匹配评分；DeepSeek 关闭时使用确定性筛选。任务运行器提供单实例锁、超时、重试
和错过任务补跑元数据。

Windows 任务计划程序安装前先 dry-run：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_tasks.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts/install_windows_tasks.ps1
powershell -ExecutionPolicy Bypass -File scripts/uninstall_windows_tasks.ps1
```

## 可选 DeepSeek

模型路由和模型匹配默认关闭。需要时只在本机 `.env` 设置：

```dotenv
RECRUITOPS_LLM_ENABLED=true
RECRUITOPS_LLM_ENDPOINT=https://api.deepseek.com/anthropic/v1/messages
RECRUITOPS_LLM_API_KEY=replace-locally
RECRUITOPS_LLM_MODEL=deepseek-v4-flash
RECRUITOPS_LLM_MAX_TOKENS=400
RECRUITOPS_LLM_MATCHING_MAX_TOKENS=2000
RECRUITOPS_LLM_MATCHING_THINKING_ENABLED=true
RECRUITOPS_LLM_MATCHING_REASONING_EFFORT=high
```

确定性筛选、crawler 验收、数据库事务、权限策略和安全停止不依赖 DeepSeek。
模型只能返回白名单中的结构化意图或匹配结果，不能获得任意 SQL、Shell、浏览器
点击或未经审批的写入能力。

意图路由、标题粗筛、普通查询等高频短任务关闭思考；只有基于完整 JD 与候选人
证据的匹配评分开启 `high` 思考。默认不使用 `max`，以控制延迟和 Token 消耗。

## 一次性旧系统迁移

迁移前先应用 Agent-owned PostgreSQL 迁移并做静态检查：

```powershell
docker compose up -d postgres
python scripts/check_migrations.py
python scripts/apply_migrations.py
```

迁移 runner 按数字顺序写入 `schema_migrations` ledger；正式审计记录使用
`write_audits` 表。迁移静态检查会确认没有破坏性 SQL，并要求声明
`CREATE EXTENSION IF NOT EXISTS vector`。

然后在宿主机从项目根目录执行 dry-run，再决定是否 apply：

```powershell
python scripts/import_legacy_snapshot.py `
  --source-root D:/秋招系统 `
  --dry-run

python scripts/import_legacy_snapshot.py `
  --source-root D:/秋招系统 `
  --apply
```

导入器读取旧 `config.yaml`、`data/jobs.db` 和 `data/applications.json`，SQLite
使用 `mode=ro`，并在读取前后校验三份文件 fingerprint 未变化。dry-run 不写
PostgreSQL 或 `companies.yaml`；apply 只写 Agent-owned snapshot tables 和
Agent `config/companies.yaml`。迁移完成后，不要把旧路径加入每日任务或 API 配置。

如需单独留存旧树不变的证据：

```powershell
python scripts/verify_legacy_unchanged.py snapshot --source-root D:/秋招系统
python scripts/verify_legacy_unchanged.py verify --source-root D:/秋招系统
```

`scripts/sync_sqlite_readonly.py` 仅为旧安装保留，不能替代新的独立运行配置。
其历史兼容命令形式仍为：

```powershell
docker compose run --rm api python scripts/sync_sqlite_readonly.py
```

默认 Compose 不挂载旧源目录；新部署使用 `import_legacy_snapshot.py`，不执行
重复 SQLite 同步。

## 招聘邮箱

邮箱默认关闭。当前连接器只读访问 IMAP TLS，通过 Message-ID 去重和持久化游标
增量同步；凭据只从本机环境变量读取。163 个人邮箱应使用客户端授权码，不要使用
网页登录密码。启用时在 `.env` 设置：

```dotenv
RECRUITOPS_MAIL_ENABLED=true
RECRUITOPS_MAIL_IMAP_HOST=imap.163.com
RECRUITOPS_MAIL_IMAP_PORT=993
RECRUITOPS_MAIL_IMAP_USERNAME=your-account@163.com
RECRUITOPS_MAIL_IMAP_PASSWORD=set-locally
RECRUITOPS_MAIL_IMAP_MAILBOX=INBOX
```

OAuth、安全存储、邮件与投递的完整关联及日程草稿仍是待验收能力。

## Edge 主动桥与被动证据

Edge 扩展启动后主动连接 `ws://127.0.0.1:8010/browser-bridge`，先完成服务端
challenge 和 HMAC 认证，再接收服务端推送的 `operation.dispatch`。扩展返回 ACK、
`CONNECTING`、`DISPATCHED`、导航/登录等待、证据提取、验证和终态事件；连接断开
后，服务端会保留未确认的 outbox 以便恢复。扩展不领取任务、不轮询任务，也不把
Cookie、原始 DOM 或任意 JavaScript 发送给 Agent。

被动证据流程只提交当前页面的用户授权脱敏 observation。登录、CAPTCHA、页面状态
不清晰或证据不足时必须暂停并等待用户动作；高风险、回退或破坏性数据库变更仍须
走通用 approval。真实 Edge 登录态联调仍是部署环境验收，不由离线 fixture 代替。

## 备份与恢复

```powershell
python scripts/backup_local_state.py
python scripts/restore_local_state.py path\to\backup.zip
python scripts/restore_local_state.py path\to\backup.zip --apply

python scripts/backup_postgres.py
python scripts/restore_postgres.py path\to\postgres.dump
python scripts/restore_postgres.py path\to\postgres.dump --apply
```

恢复默认只做清单、SHA256 和路径安全检查，`--apply` 才写本地状态。PostgreSQL
备份使用 custom format，需要 `pg_dump` 与 `pg_restore`。2026-08-30 已在 Docker 内
完成隔离恢复和 19 张核心表逐表核对；正式恢复仍必须先执行同等隔离验收。证据见
`docs/POSTGRES_RESTORE_DRILL_20260830.md`。

## RAG 导入与 pgvector

RAG 只读取显式批准清单，不扫描整个磁盘。默认命令生成预览，`--apply` 才写
Agent-owned PostgreSQL：

```powershell
python -m scripts.ingest_rag_sources --manifest config\rag_sources.yaml
python -m scripts.ingest_rag_sources --manifest config\rag_sources.yaml --apply
python -m scripts.ingest_rag_sources `
  --manifest config\rag_sources.yaml --apply --prune-managed
```

`pgvector/pgvector:pg16` 提供 `vector` 扩展。扩展状态可在数据库容器内检查，
密码不写入命令参数：

```powershell
docker compose exec -T postgres sh -lc 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;"'
docker compose exec -T postgres sh -lc 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extname FROM pg_extension;"'
```

个人画像由 Agent 配置直接读取，不进入候选人 RAG。真实 BGE-M3 可通过兼容
OpenAI Embeddings 的 endpoint 接入；本地默认使用确定性 embedding。

## 本地验证

```powershell
python -m pytest
python -m compileall -q apps packages evals scripts
python -m evals.mvp_runner
python -m evals.rag_runner
python scripts/check_migrations.py
python scripts/verify_runtime.py
```

这些检查不访问外部招聘网站、不调用 DeepSeek，也不写旧系统源文件。真实网站
盲测、Edge 登录态端到端、真实邮箱关联和 PostgreSQL 恢复演练必须另行验收。
