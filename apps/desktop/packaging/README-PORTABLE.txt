RecruitOps Desktop
==================

Version: 0.1.6 (2026-09-25)

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
1. 后台任务可查询中断记录、暂停、取消和续跑，无需用户复制内部运行编号。
2. 进度卡覆盖岗位抓取、官网投递复核与邮件处理，只展示当前活跃任务。
3. 岗位分页与加载优化；投递记录支持公司/岗位搜索，各阶段看板独立加载更多，不再提供重复的阶段筛选。
4. 邮件可经用户明确确认后绑定、更正或解除关联，不再显示无意义的默认 0% 置信度。
5. 包含此前已在源码完成的评分校验、桌面启动及简历闪填等修复；不代表所有网站或模型均已兼容。

v0.1.6 增补：公司来源刷新支持部分异常隔离，保留已确认的有效来源与断点；
岗位抓取重试和并发调度优化，DeepSeek 结构化输出校验与失败保护继续保留。
后台全量爬取完成、部分完成或失败后，软件运行且模型可用时由助理自动核对结果并汇报；
邮件处理和投递复核仍在当前助理回合等待结果。未执行真实网站端到端兼容保证。

2026-09-24 本地补丁：投递复核与邮件处理在当前助理回合等待并返回结果，仅全量爬取采用后台交互。
复核按有界批次连续执行，正常批次间隙不再误报暂停；已尝试、已完成及待重试数量分别统计。
全量爬取的公司进度按“已处理 / 总数”展示，不再在进度条单列抓取失败或待重试数量；处理过不代表均成功抓到岗位。
2026-09-24 第二版修复：全量投递复核的业务数量与任务步骤预算分离，修复 0 条或超过 50 条记录时启动失败。
历史中断的抓取任务可由助理标记取消，包括旧版没有控制回执的记录；取消不删除已入库结果，取消后不再续跑该任务。
投递看板修复：移除阶段下拉筛选，各列独立加载，避免全局前 50 条恰好属于同一阶段时其他列错误显示为空。
结构化解析修复：可识别的 DeepSeek OpenAI-compatible 请求明确传递关闭思考参数，保留字段校验与失败保护；第三方网关需支持对应参数，不能保证模型永远返回完整字段。
全量任务自动接续：每段 6 小时预算到期后，保存并验证断点，按原范围自动继续剩余抓取或评分；暂停、取消优先，断点异常或持续无进展时停止。需保持软件和电脑运行，退出软件后不会继续执行。

旧版已有岗位、投递记录或邮件数据时
--------------------------------
先等待正在执行的任务结束，从软件和系统托盘正常退出，不要强杀数据库。
把新包解压到一个全新的短路径文件夹，不要覆盖解压到旧程序的 resources 内。
完整备份旧程序旁边的 .data；旧程序和备份保留到新版本验证完成。
在新版本第一次启动前，把旧版完整的 .data 复制到新程序旁边，不只复制数据库子目录。
不要把旧 resources、旧程序文件、单独的 instance.json 或 runtime.lock 混入新程序资源。
复制完成后启动新版，核对岗位、投递、邮件、配置和日程。迁移前后的完整实例仍受当前账户权限保护。
如 Windows 资源管理器提示路径过长，不要选择跳过，应先取消并用支持长路径的完整复制方式。
不要同时启动旧版与新版来操作同一份实例数据。新版启动失败时保留原始 .data 和日志，不要删除数据库重建。
