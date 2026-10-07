RecruitOps Desktop
==================

Version: 0.1.7 (2026-10-07)

1. Extract the complete ZIP to a short writable path, for example D:\RecruitOps.
2. Double-click RecruitOps-Desktop-Preview.exe.
3. Keep all files and folders together. Do not run the EXE inside the ZIP.

The first launch creates a local .data folder next to the EXE and initializes the
database. If the first launch is interrupted, the application can retry an empty,
never-initialized database on the next launch. Existing databases and backups are
never deleted automatically.

Requirements: 64-bit Windows 10/11. Python, Node.js, PostgreSQL, Docker, and the
Visual C++ Redistributable do not need to be installed separately.

This package contains no developer resume, mailbox password, API key, application
record, or personal knowledge content. Back up .data before deleting or moving it.

本次更新
--------
1. 官网投递复核结合页面、岗位卡片与必要的截图识别，校验岗位和阶段证据。
   新增复核明细、待核对列表，以及登录后重新复核该公司本次岗位的入口。
2. 邮件同步明确显示新增、已有和失败；识别错误有限重试，失败邮件可选择后重试。
   未指定岗位的同公司测评/笔试邮件支持人工确认多个投递，共享一项日程。
3. 定时全量抓取先创建本地任务会话；需要模型的任务对临时启动错误有限重试。
   报告可在重启后查看和追问，完成、部分完成和失败分别报告，保留实际成果。
4. 内置招聘浏览器定期保存登录状态，正常退出再次保存；会话 Cookie 加密恢复
   最长 7 天，仍受官网注销和失效规则影响。
5. 空闲时分批清理或压缩超过 12 小时的复核诊断和工具追踪，保留业务历史、
   最新复核摘要与仍需恢复的任务证据。
6. 数据库结构已是最新时，启动不再重复生成完整备份；需要迁移时先备份检查，
   自动迁移备份保留最近 3 份。已保存岗位不会因关键词变化重新隐藏，投递看板
   分页、数量显示和刷新性能改善。

真实网站、网络、登录验证和模型输出仍可能导致局部失败；证据不足时保留已确认阶段。
软件和电脑需要保持运行，退出后任务不会继续。不保证所有网站兼容或模型始终识别正确。

旧版已有岗位、投递记录或邮件数据时
--------------------------------
先等待正在执行的任务结束，从软件和系统托盘正常退出，不要强杀数据库。
把新包完整解压到临时短路径文件夹，不要覆盖解压到旧程序的 resources 内；暂不启动。
完整备份旧程序旁边的 .data；旧程序和备份保留到新版本验证完成。
将旧程序目录改名保留，再让新版程序目录回到旧版相同的绝对路径。
在新版本第一次启动前，把旧版完整的 .data 复制到新程序旁边，不只复制数据库子目录。
使用同一台电脑、原来的 Windows 用户启动；更换路径、电脑或用户不能直接照搬旧实例。
不要把旧 resources、旧程序文件、单独的 instance.json 或 runtime.lock 混入新程序资源。
复制完成后启动新版，核对岗位、投递、邮件、配置、日程和登录状态。
仅有待应用的数据库结构迁移时，已有数据库才会先做自动迁移备份；它不能代替完整 .data 备份。
如 Windows 资源管理器提示路径过长，不要选择跳过，应先取消并用支持长路径的完整复制方式。
不要同时启动旧版与新版来操作同一份实例数据。新版启动失败时保留原始 .data 和日志，不要删除数据库重建。
详细升级与回退步骤：https://github.com/849879772/recruitops-agent/blob/main/docs/UPGRADE_EXISTING_DATA.md
