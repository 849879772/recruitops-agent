# RecruitOps v0.1.4（Windows 便携版热修复）

本版修复 v0.1.3 安装包遗漏 `scripts.run_agent_crawler`，导致公司入口校验返回 `crawler_failed`、`No module named scripts.run_agent_crawler`，并使复用该子进程的来源重试失败的问题。此前失败时显示的 `raw_job_count=0` 不表示招聘官网没有岗位；修复版需要重新执行失败的校验或抓取。

打包校验现在强制检查爬虫、MCP 及其他实际使用的 Python 子进程入口。发布前还会用安装包内置 Python 执行爬虫入口的无网络烟测，以阻止同类缺文件问题再次通过验收。

下载文件：`RecruitOps-v0.1.4.zip`（约 704 MB）。完整解压后运行 `RecruitOps-Desktop-Preview.exe`，不要只复制 EXE。

SHA-256：`F05D9DB4BBF6C49F7C309FBE5245C49178F4CB8C53250F16F6F4D6EB5D9EDA41`

## 旧版已有数据的用户

如果旧版已爬取岗位，或保存了投递记录、招聘邮件与配置，请先正常退出软件并完整备份原 `.data`。让新版程序回到旧版的**相同绝对路径**，再放回整个 `.data`，并用原 Windows 用户启动。不要只搬 `pgdata`，也不要覆盖正在运行的旧版。操作与回退步骤见[已有数据的桌面版升级步骤](UPGRADE_EXISTING_DATA.md)。发布包不含任何用户 `.data`。

## 验证范围

已验证资源清单缺少入口时失败、内置 Python 的爬虫模块启动、PostgreSQL 初始化及迁移、重启与备份恢复，以及打包桌面程序的匿名闪填、投递记录和重启持久化。未对所有真实招聘网站的岗位数量作保证。
