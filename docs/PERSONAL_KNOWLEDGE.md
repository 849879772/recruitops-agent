# 个人知识库第一版

在左侧“个人知识库”上传 Markdown、UTF-8 TXT、文字版 PDF，或粘贴笔记。
资料类型为“我的经历”“参考资料”“学习笔记”；参考资料不会被作为个人经历的依据。
支持查看原文、替换资料、删除及失败重试。每份最多 5 MB、10 万字符，PDF 最多 80 页，最多 100 份。
扫描 PDF 暂不支持；遇到无法提取文字的页面会明确失败，不会悄悄建立缺页索引。
不会自动扫描本机文件夹，不支持代码仓库索引。

## 对话

从岗位详情进入求职助理，勾选个人知识库，可选全部资料或某一份资料，然后提问。
也可从资料列表点击“用于对话”。岗位数据仍由数据库工具读取，只有资料正文进入 RAG。
工具 knowledge_search 的 personal 域支持 list、search、read；read 单次最多 6000 字符，可通过 offset 补读。
回答的引用链接可打开文档原文。更新或删除后旧引用会提示失效，不会冒充旧版本。
资料内容只是证据，不授予模型执行命令或修改业务数据的权限。

## 检索与存储

按页、标题、段落切分为约 350 字符的块，长段落保留 40 字符重叠。
中文分词 BM25Plus 和语义向量各召回最多 10 块，用 RRF(k=60) 合并，最多返回 6 块并补充相邻段落。
明确选中的单页短文档直接完整读取。该实现面向小型个人资料库，向量使用精确余弦检索，无额外向量数据库。
默认 FastEmbed 本地 BAAI/bge-small-zh-v1.5，512 维；首次索引下载约 90 MB 模型，此后复用缓存。
缓存位于 .data/knowledge/models，Docker 使用已有状态卷持久化。索引和原始文件存入 PostgreSQL 新表，迁移 022。
已有 embedding_endpoint 配置时复用该提供方；模型改变后旧索引不混用，可运行 scripts/reindex_personal_knowledge.py 原子重建索引，失败时保留原向量，不需要重新上传资料。
Windows GPU 的 Qwen3-Embedding-0.6B 部署、1024 维索引和快捷启动配置见 [QWEN_EMBEDDING.md](QWEN_EMBEDDING.md)。

本地 embedding 不产生模型 API token 费用；助理回答仍使用现有聊天模型并按实际请求计费。
上传原文不会发给外部 embedding 服务（除非配置了外部服务），但检索到的片段会随问题发送给当前聊天模型。
分享数据库时必须排除 personal_knowledge_documents 和 personal_knowledge_chunks，避免泄露个人资料。
不自动重算岗位评分，也不修改简历画像、投递状态、邮箱和日程。

## 验证

运行 pytest tests/test_personal_knowledge.py tests/test_response_language.py。
覆盖索引、混合检索、版本失效、级联删除、失败重试、重启恢复、写入竞争、工具协议及同源访问边界。

手动端到端验收：python scripts/verify_personal_knowledge.py。需要已启动的本地服务和 Edge；只创建并清理临时资料。
加 --agent 时额外创建一轮真实模型对话，验证 job_detail、knowledge_search 和引用，再清理测试对话；该选项有聊天 API 费用。
2026-09-15 验收通过：41 项 Python、23 项前端测试，1 项因已安装 MCP SDK 而跳过；真实向量、正式 Agent、桌面和 390px 手机视口均通过。
