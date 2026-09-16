# RecruitOps Agent 本地启动

更新时间：2026-09-13。服务状态请以实时健康检查为准，不使用历史公司数量判断启动成功。

## 当前 Docker 部署

日常可双击桌面的“秋招工作台”快捷方式。它调用 `scripts/start_desktop.ps1`：
服务已就绪时直接打开 `http://127.0.0.1:8012/`；否则先调用现有 Docker 启动脚本，
确认健康后再打开默认浏览器。重复点击不会并发启动；失败时显示错误，启动日志位于
`.data/logs/desktop-start.log`。桌面入口不构建镜像、不删除业务数据。

在项目目录执行：

```powershell
.\scripts\start_docker.ps1
```

该脚本先检查引擎；Docker Desktop 未运行时才调用受限 socket 恢复脚本，保留旧运行目录后启动。若 Desktop 正在运行但不可用，只等待并报错，不强杀。随后启动现有 API/PostgreSQL 服务并等待健康，不重建镜像、不删除容器卷、不创建定时任务。

预览操作：

```powershell
.\scripts\start_docker.ps1 -DryRun
```

当前本机页面为 http://127.0.0.1:8012/，数据库映射端口为 5433。端口以 `.env` 和 `docker compose ps` 为准。

## 健康与日志

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8012/ready
docker compose logs --tail=100 api
```

`ready` 应为 `ready` 且 PostgreSQL 检查通过。前端能打开不等于后台任务全部成功。

## 更新代码

有正在运行的抓取/评分任务时先确认其状态，不直接重启。镜像内打包的代码改动通过稳定镜像部署：

```powershell
docker compose build api
docker compose up -d --no-deps api
```

仅运行配置需要重新加载且不涉及镜像代码时，使用 `docker compose restart api`。数据库不需要为 API 改动重建。不要例行使用 `down --volumes`、恢复出厂设置或清空历史岗位。

## 全量与恢复

向求职助理明确要求正式全量。其调用 `daily_recruitment_sync(mode="full", dry_run=false)`，不传公司 ID 清单；十公司限制仅针对显式抽测。

- `crawl_only`：刷新来源并抓取，不评分。
- `full`：刷新来源、抓取入库，再在模型配置启用时评分。
- `score_only`：处理已保存且待评分的 JD，不再抓详情。
- `resume`：恢复原任务的冻结范围与完成进度；缺少恢复信息时报错，不自动扩大范围。

## 数据保护与原生方案

正式数据包括 PostgreSQL 卷和 Agent `.data` 状态卷。宿主目录同名 `.data` 不保证是容器状态的完整副本。迁移前应分别备份并验证恢复，保留原卷作回滚。

当前模型使用远端 DeepSeek API，不需要 Docker Model Runner。此次已关闭其推理功能并恢复引擎，但这不代表所有 Docker Desktop 启动故障都能自动修复。

`start_local.ps1` 是原生 API 开发入口，不是完整的数据库和 Agent 状态迁移工具。去掉 Docker 的评估见 `docs/NATIVE_DEPLOYMENT_ASSESSMENT_20260913.md`；在完成原生依赖、数据恢复和隔离验收前，不卸载 Docker。
