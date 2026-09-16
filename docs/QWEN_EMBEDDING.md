# Qwen 本机 GPU 向量服务

项目 API/PostgreSQL 继续使用 Docker；向量服务单独运行于 Windows，仅监听 127.0.0.1:8015。
Docker Desktop 通过 host.docker.internal 访问。接口需要随机 Bearer 密钥，不开放 CORS，不开放公网或局域网监听。
模型 Qwen/Qwen3-Embedding-0.6B，固定 revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3，1024 维。
使用 RTX GPU、FP16、SDPA；一个进程，推理批次 4，每请求最多 16 文本，单文本最多 2048 tokens。
超过长度明确拒绝，不静默截断。问题在客户端添加检索指令，文档不添加。向量归一化后继续 BM25 + 余弦 + RRF。

## 安装

1. Python 3.11 创建独立环境：python -m venv .data/embedding/venv。
2. 使用该环境安装 torch==2.8.0，官方 CUDA 12.8 wheel 索引 https://download.pytorch.org/whl/cu128。
3. 安装 services/embedding/requirements.txt 和 python-dotenv；不向主项目 Python 环境或 API 镜像安装 CUDA/PyTorch。
4. 用 huggingface_hub.snapshot_download 下载上述模型的固定 revision，保存到 .data/embedding/model；保留配置、tokenizer、1_Pooling 和 safetensors。
5. 若大文件下载不稳定，scripts/download_qwen_assets.py 支持 torch/model 两种官方大文件的断点分段下载与 SHA256 验证，--proxy 可指定本机可用代理。该脚本不代替第 4 步的小配置文件下载。
6. 运行 python scripts/configure_qwen_embedding.py 生成 .data/embedding/service.json（随机密钥，Windows ACL 限当前用户及 SYSTEM）。
7. 运行 pwsh -File scripts/start_embedding.ps1 -Force；首次加载会稍慢，日志位于 .data/logs/embedding.stderr.log。
8. 核对健康检查及 Docker 到本机连通后，运行 python scripts/configure_qwen_embedding.py --activate。
9. 更新 API 源码镜像并执行 docker compose up -d --no-deps --no-build --wait api。
10. docker compose exec api python scripts/reindex_personal_knowledge.py，仅原子替换个人文档向量；来源正文、引用版本和业务记录不变。

## 运行与恢复

现有桌面快捷方式调用 start_desktop.ps1，会优先确保 GPU 向量服务健康，然后检查现有 API。
重复点击不会加载多个模型。API 已运行但模型进程退出时，也会重新启动模型服务。
模型目录和虚拟环境在 .data/embedding，下载只需一次，运行时采用 local_files_only。
个人知识库使用独立的 RECRUITOPS_KNOWLEDGE_EMBEDDING_* 配置，不改变原有证据检索使用的 RECRUITOPS_EMBEDDING_* 配置。
旧 BGE 未删除；回退时将四个 RECRUITOPS_KNOWLEDGE_EMBEDDING_* 配置恢复为 previous-embedding-config.json 中的原值，
把 service.json 的 enabled 改为 false，再更新 API 容器并重新索引个人资料。不要恢复整份旧 .env，以免覆盖后来的邮箱或模型配置。
切换失败时不自动伪装为 CPU/BGE，不混用不同模型的索引。重建失败保留原向量，恢复原配置后仍可使用。

## 隐私与边界

service.json、密钥、模型文件、下载缓存和虚拟环境均位于忽略目录 .data，不进入源码或分享包。
向量服务不访问业务数据库，文本仅在本机计算，不记录请求正文。助理生成回答仍使用原聊天 API。
不修改岗位评分或抓取流程，不扫描个人目录。当前分块策略不因模型支持 32K 而自动扩大。

## 本机验收（2026-09-15）

- RTX 4060 Laptop 8 GB；PyTorch 2.8.0+cu128、Sentence Transformers 5.7.0、Transformers 4.57.6，pip check 通过。
- 模型和 CUDA wheel 均通过官方 SHA256；真实 CUDA 推理返回归一化 1024 维向量；未授权请求返回 401。
- 容器通过 host.docker.internal:8015 访问成功，服务仍仅监听本机回环地址；Docker NO_PROXY 包含该主机名。
- 8 个合成中文检索问题，Qwen Top1=7/8、Top3=8/8，混合检索中位耗时 67.9 ms；旧 BGE 同为 7/8、8/8，中位 6.4 ms。小样本不是通用质量结论，不能据此声称 Qwen 更准或更快。
- 验收后的整卡显存占用约 2480 MiB，启动前约 1186 MiB；包含桌面等其他进程，不代表模型独占或峰值。
- 正式页面完成上传、预览、桌面/手机检查、用于对话、更新、旧引用拒绝、删除；真实 Agent 使用 job_detail 和 knowledge_search，提供原文引用且区分参考资料与个人经历。
- 桌面快捷启动通过，已有健康服务被复用。临时资料、向量和测试对话已清理；正式知识库仍为 0 份。
- 岗位 18,135、评分 16,905，部署前后不变；未重建数据库容器。原有证据检索配置保持不变。
- 下载缓存删除被本机执行策略拦截，.data/embedding/downloads 与 CUDA wheel 等约 7.5 GB 临时下载仍保留；没有绕过策略。模型、环境和密钥不可当作缓存删除。
- 定向 Python 回归 39 通过、1 跳过；前端 Node 回归 23 通过。
