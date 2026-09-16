# RecruitOps 分享版部署说明

## 包内内容

包含完整运行源码、Edge 浏览器扩展、公开公司/岗位/JD/抓取状态，以及历史参考评分。数据明细与 SHA-256 在 `seed/manifest.json` 中。

不包含原使用者的简历、邮箱、投递记录、日程、聊天、浏览器登录状态、API 密钥、定时任务或测试产物。历史数字分数保留，个人优劣势、简历证据与评分解释已移除。所有改动只发生在导出的副本，原库不变。

**分数是原使用者偏好下的历史参考分，不是你个人的匹配度。** 上传新简历不会自动重算已有分数；未评分岗位及今后新岗位按新配置评分。包内也保留抓取失败/不完整的岗位和公司，并非每条都有完整 JD 或分数。页面会按当前标题筛选规则隐藏无关岗位，因此页面总数可能少于数据库总数。

## 环境

- 推荐 Windows 10/11 + Docker Desktop（Linux containers / WSL2 模式），浏览器使用 Edge。
- 不需要安装 Python、Node 或原来的“秋招系统”项目，也不需要登录 Codex 账号。
- 首次构建需要联网下载容器基础镜像、Chromium 和 Python/Node 依赖，需要数 GB 磁盘空间；本包不含巨大的离线镜像。
- 解压到独立目录，例如 `D:\RecruitOps-Share`。先启动 Docker Desktop，确保引擎正常。
- Linux/macOS 可以使用下面的 Compose 命令运行网页和抓取服务；Windows Edge 投递状态桥接未在这些平台验收。

## 首次启动

在解压目录双击 `Start.cmd`。它会检查 Docker、首次构建镜像、启动数据库/API、校验并导入数据，最后打开 `http://127.0.0.1:8012/`。不要直接在压缩包内运行。

也可以在终端执行：

```powershell
docker compose build api
docker compose up -d --wait --wait-timeout 180
docker compose exec -T api python scripts/share_catalog.py import --directory /app/seed --if-empty
```

导入只允许空岗位目录，检查文件校验和，所有表在一个事务中写入；失败会回滚。再次启动不会覆盖已有目录，更不会重置投递信息。

如果 8012 端口被占用，在 `.env` 中修改 `RECRUITOPS_API_PORT`，例如 8013，再重启。数据库不向宿主机开放端口。本包的 Compose 项目名是 `recruitops-share`，与开发项目隔离；多个分享版实例需要分别修改 Compose 项目名及端口。

## 首次配置

1. 打开“配置”，填写自己的 API 服务地址和密钥，启用模型调用并保存。模型统一使用 `deepseek-flash`（界面名称 V4.1 Flash），解析时关闭思考；服务商必须支持项目使用的 Anthropic Messages 兼容接口。
2. 上传自己的文字 PDF/TXT/MD 简历，核对提取结果，再确认替换并保存。上传会发送简历文字到你配置的模型服务。扫描版 PDF 暂不支持。
3. 设置求职方向及岗位标题包含/排除关键词，高级字段可以不填。没有自定义关键词时使用程序内置方向筛选；实习、博士岗位仍被排除。
4. 按需配置邮箱 IMAP 地址、账号和**邮箱授权码**，不是普通登录密码。投递记录可以用页面提供的 CSV 模板或 JSON 导入。
5. 开启求职助理/邮箱后，等待现有任务结束，执行 `docker compose restart api`。新配置才能完整应用到常驻进程。

默认没有定时任务，也不会启动模型评分或读取任何人的邮箱。需要时再在页面创建定时任务。

## Edge 扩展与投递状态

访问 `edge://extensions`，开启开发者模式，选择“加载解压缩的扩展”，选本包 `extension` 文件夹（版本 0.3.22）。按扩展连接设置填写自己的本机 API 地址和桥接令牌；连接步骤参见 `docs/EXTENSION.md`。

浏览器 Cookie 不包含在包中，需要自己登录招聘网站。验证码和身份验证必须本人完成。不装扩展仍可浏览公司、岗位、JD 和参考分，以及使用不依赖浏览器桥接的功能。

## 后续启动、停止与备份

- 再次双击 `Start.cmd` 即可启动；只在本地没有镜像时构建。更新源码后手动执行 `docker compose build api`。
- 停止：`docker compose stop`。重新启动：`docker compose up -d`。
- 查看故障：`docker compose ps`、`docker compose logs --tail 100 api`。
- 数据保存在 Docker 的 `postgres-data` 与 `agent-state` 项目卷中，不在压缩包里实时更新。
- **不要执行 `docker compose down -v` 或清理卷，否则会删除你后续的个人数据。**
- 初始 seed 只含公开目录，不是个人数据备份；完整备份需要备份 PostgreSQL 和 agent-state。
- 本地模型 API/邮箱不通时，可先关闭相关功能，数据库和页面仍可使用。构建失败时检查 Docker 的网络/镜像源；不要把 Windows 的 127.0.0.1 代理地址直接当作容器内代理。

## 分享与许可

公开招聘信息可能变更，申请前仍需查看官网。仅用于个人求职，不代表取得招聘网站或依赖软件的再分发许可；不要把招聘信息批量商业再分发。再次分享时请重新使用安全目录导出，不能直接打包自己的 `.env`、`.data` 或整个数据库。
