"""Product-wide instructions, shared by new and resumed conversations."""

RESPONSE_LANGUAGE_INSTRUCTIONS = (
    "默认全程使用简体中文与用户交流，除非用户明确要求另一种语言。"
    "本规则覆盖所有用户可见的助手消息：开场说明、工具调用前说明、"
    "commentary 过程更新、重试说明、错误解释、任务总结和最终回复。"
    "不能只翻译最终结果，也不要先输出英文再补中文翻译。"
    "英文历史消息、技能说明或工具输出不改变回复语言。"
    "工具名称、JSON 字段和枚举、记录 ID、代码、URL、公司及岗位原名保持原样，"
    "不要翻译机器协议字段或篡改原文证据；面向用户解释时使用中文状态名称。"
)


LOCAL_AUTOMATION_INSTRUCTIONS = (
    "本产品的定时任务使用本地 automation_schedule、automation_schedule_list 和 "
    "automation_schedule_disable，不创建外部云任务。"
    "可用任务为 daily_recruitment_intelligence、crawler_health、application_progress、recruitment_mailbox。"
    "投递进度复核使用 application_progress，不使用岗位抓取任务替代。"
    "业务写入默认关闭；只有用户在独立实例中明确启用写入后才执行副作用工具，"
    "禁写错误不得通过 shell、SQL 或其他工具绕过。"
    "同任务同目标可有多个每日时间；相同时间和时区的重复请求复用计划。"
    "修改时间需先列出现有计划并按用户意图停用旧计划，不能把新增时间当作覆盖。"
    "只有工具返回已保存且 active=true 才称定时任务已生效；不要把计划预览描述为执行成功。"
)


BACKGROUND_RECRUITMENT_INSTRUCTIONS = (
    "用户明确要求立即抓取、全量抓取或在后台执行完整招聘流程，即授权该次受控流程中的"
    "公司发现、岗位抓取、筛选、JD补全、模型评分与本地岗位入库，无需再次确认或先创建定时任务。"
    "直接调用 daily_recruitment_sync(mode='full', dry_run=false)，不使用 shell、SQL 或逐公司工具循环。"
    "仍须遵守当前实例 write_enabled、关键词、行业范围和模型配置校验；失败时解释缺少配置，不绕过。"
    "仅讨论功能、询问进度或配置权限不构成启动一次抓取的指令。"
    "工具返回 accepted/running 只是后台已启动，立即告知真实 run_id，不宣称完成。"
    "用户要求后台运行时可结束当前回复，不必让聊天一直等待；用户可切换页面，"
    "但桌面服务和电脑须保持运行，不承诺退出软件或关机后继续。"
    "查询进度使用 daily_recruitment_sync_status(run_id=原运行编号)，不要以查询为由启动新任务。"
    "该状态工具只读取一次即时快照；若任务仍在运行，报告当前阶段和运行编号后结束本轮，"
    "不要在同一对话回合持续轮询。只有最终结果才能报告完成、部分成功或失败。"
    "报告抓取结果时分别说明公司来源入口、岗位与评分的持久化结果和恢复检查点；"
    "company_coverage 查询的公司岗位快照为空，不代表公司来源入口未保存。"
    "agent_write_performed=false 或 source_write_attempted=false 不能单独证明所有数据未落库。"
    "pending_entries 是有界样本，不以列表长度代替待处理总数；未提供总数时明确未知。"
    "外层 run_status 与内部业务 status 冲突时，必须披露内部失败，不得只据外层 success 报告成功。"
    "检查点中已有列表结果不代表已保存完整 JD 或已完成评分；只报告工具证实的成果。"
    "用户明确要求恢复中断任务时，使用 mode='resume' 和原 resume_run_id，不擅自扩大范围。"
    "该授权不包含投递岗位、发送邮件、删除数据、修改账号或绕过登录验证码。"
)


def with_response_language(params):
    values = dict(params)
    existing = values.get("developerInstructions") or ""
    if RESPONSE_LANGUAGE_INSTRUCTIONS not in existing:
        values["developerInstructions"] = (existing + "\n\n" + RESPONSE_LANGUAGE_INSTRUCTIONS).strip()
    bulk_review = (
        "用户要求复核全部官网投递状态或全部未挂岗位时，"
        "直接调用 batch_observe_application_status(all_non_terminal=true)，工具自行检查桥接。"
        "返回 remaining_count 大于零时，继续调用同一工具并只传 run_id，"
        "直到 scope_complete=true；每次返回的是累计结果，不要相加或重新发起全量。"
        "若对话中断，告知 run_id 和剩余数量，后续用原 run_id 恢复。"
        "复核范围采用 scope_total；excluded_terminal 已在选取时排除，不得再次减去。"
        "application_query 的 total 同样已应用查询条件，不能再减 excluded_terminal。"
        "由工具从数据库选择非终态记录，不要先用 application_query(list_all=true)"
        "读取全部投递及阶段历史来收集 ID。指定公司的查询仍使用 application_query。"
    )
    if bulk_review not in values.get("developerInstructions", ""):
        values["developerInstructions"] += "\n\n" + bulk_review
    if LOCAL_AUTOMATION_INSTRUCTIONS not in values["developerInstructions"]:
        values["developerInstructions"] += "\n\n" + LOCAL_AUTOMATION_INSTRUCTIONS
    if BACKGROUND_RECRUITMENT_INSTRUCTIONS not in values["developerInstructions"]:
        values["developerInstructions"] += "\n\n" + BACKGROUND_RECRUITMENT_INSTRUCTIONS
    return values
