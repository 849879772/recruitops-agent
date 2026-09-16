# 常见故障

## API 容器没有变成 healthy

先查看状态和日志：

```powershell
docker compose ps
docker compose logs --tail=100 api
```

`/ready` 检查 Agent 的 `config/companies.yaml`、
`config/candidate_profile.yaml`、独立 crawler import 和 PostgreSQL。它不检查
旧秋招系统目录。如果只需要确认进程存活，可访问 `/health`；如果就绪检查
失败，逐项确认 Agent 配置文件存在、迁移已应用且 PostgreSQL 健康。

## PostgreSQL 不健康或端口冲突

```powershell
docker compose logs --tail=100 postgres
docker compose ps postgres
docker compose config
```

宿主机端口是 `5433`，不是默认的 `5432`。如果 `5433` 已被占用，应在本机
配置中调整宿主机映射并同步宿主机客户端的连接地址，容器内部仍使用
`postgres:5432`。不要删除 `recruitops-postgres` 卷来解决端口问题。

## Agent 迁移未应用

迁移只针对 Agent-owned PostgreSQL，不要在旧 SQLite 上执行：

```powershell
python scripts/check_migrations.py
python scripts/apply_migrations.py
docker compose up -d --build
```

API 容器启动时也会按序应用未记录的迁移。迁移 ledger 会校验已应用文件的
名称和 checksum；出现 drift 时应恢复正确的迁移文件，不要删卷绕过检查。

## 独立 crawler 或每日任务失败

确认 Agent 配置中的公司行包含有效的 `crawler`、`careers_url` 和
`integration_status: connected`，然后先做无写入计划：

```powershell
python scripts/run_local_task.py --task daily_recruitment_intelligence --dry-run
python -m scripts.run_agent_crawler `
  --company "Example Company" `
  --crawler moka `
  --careers-url "https://jobs.example.test/campus"
```

每日流水线会因分页不完整、详情链接不在允许来源、JD 不完整、届别不是
`cohort=2027` 且 `cohort_status=confirmed` 或重复项而拒绝岗位。真实网站的
零岗位、页面改版、登录/CAPTCHA 和盲测指标属于待完成验收，不应通过放宽规则
来掩盖。

## 旧系统迁移失败

旧项目只允许作为一次性只读输入。先做 dry-run：

```powershell
python scripts/import_legacy_snapshot.py `
  --source-root D:/秋招系统 `
  --dry-run
```

导入器需要 `config.yaml`、`data/jobs.db` 和 `data/applications.json`，并要求
Agent 迁移已经建立 PostgreSQL snapshot tables。它用 SQLite `mode=ro` 打开源库，
并在读取前后校验三个文件未变化。源文件缺失、被修改或数据库 URL 指向旧库时，
应停止并修正路径，不能运行旧项目初始化或手工改源表。

apply 只写 Agent 数据库和 `config/companies.yaml`：

```powershell
python scripts/import_legacy_snapshot.py `
  --source-root D:/秋招系统 `
  --apply
```

`scripts/sync_sqlite_readonly.py` 仅为旧安装保留，不是独立项目的日常运行入口。
如需复现历史兼容命令，其形式为：

```powershell
docker compose run --rm api python scripts/sync_sqlite_readonly.py
```

默认 Compose 不挂载旧项目源目录，因此新的安装应使用
`import_legacy_snapshot.py`，不要安排重复 SQLite 同步。

## DeepSeek 或邮箱配置

模型默认关闭。确定性 crawler、筛选、迁移和离线验证不需要模型。只有在本机
`.env` 同时设置 `RECRUITOPS_LLM_ENABLED=true`、非空
`RECRUITOPS_LLM_API_KEY`、endpoint 和 model 后，才会调用 DeepSeek；密钥不要
写入命令行、文档或日志。

招聘邮箱默认关闭。启用 IMAP TLS 时检查 `.env` 中的授权码、host、port、账号和
mailbox；邮箱同步不会写旧系统，也不会自动回复、转发或删除邮件。

## CI 或本地验证失败

在项目根目录逐项运行：

```powershell
python -m pytest
python -m compileall -q apps packages evals scripts
python -m evals.mvp_runner
python -m evals.rag_runner
python scripts/check_migrations.py
python scripts/verify_runtime.py
```

这些离线检查不访问外部招聘网站或 DeepSeek。真实网站、真实 Edge 登录态、真实
邮箱关联和 PostgreSQL 恢复演练仍需单独验收。

## 密钥与日志安全

所有外部服务凭据只能通过本机未跟踪的环境变量、CI secret 或部署平台 secret
store 注入，不能写入源码、文档、前端、Dockerfile、Compose 配置或测试输出。
发现日志包含凭据时，先轮换凭据，再清理日志与缓存。
