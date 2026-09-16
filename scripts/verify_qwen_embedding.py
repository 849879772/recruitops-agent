"""Local synthetic retrieval comparison, without paid chat calls or business writes."""
import json
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.personal_knowledge import KnowledgeService, LocalEmbedding
from packages.rag.embeddings import OpenAICompatibleEmbeddingProvider, QWEN_QUERY_PREFIX
from packages.storage import Storage

DOCUMENTS = {
    "vision": "外部参考：RealSense 深度相机通过手眼标定，将识别到的工件位置转换到机械臂基座坐标系，用于自动抓取。",
    "database": "学习笔记：数据库事务通过写前日志确保崩溃后的数据恢复，多版本并发控制减少读写互相阻塞。",
    "ros": "学习笔记：ROS2 节点通过话题发布和订阅消息，DDS 提供底层通信，QoS 设置可靠性和历史队列。",
    "cpp": "外部参考：C++ 多线程共享队列使用互斥锁和条件变量实现生产者消费者，避免忙等待和数据竞争。",
    "rag": "个人示例：检索增强生成先从文档中召回依据，再将片段和问题交给模型回答，用原文引用约束结论。",
    "frontend": "学习笔记：响应式界面使用 CSS Grid 和媒体查询适配窄屏，表格容器允许横向滚动，防止内容挤出视口。",
    "training": "外部参考：LoRA 冻结原模型参数，仅训练低秩适配矩阵，减少微调所需的显存和可训练参数。",
    "linux": "个人示例：Linux 服务使用 systemd 管理启动和自动重启，通过 journalctl 检查日志和失败原因。",
}
QUERIES = [
    ("机械臂怎样知道相机看到的零件应该去哪里抓？", "vision"),
    ("断电以后怎样保证已经提交的数据不会丢？", "database"),
    ("机器人不同程序间如何传递传感器消息？", "ros"),
    ("共享任务队列有多个读写线程时怎样避免抢占冲突？", "cpp"),
    ("怎样让回答有资料依据并能追溯原文？", "rag"),
    ("手机上表格太宽，怎样不让整个页面溢出？", "frontend"),
    ("显存有限，如何减少大模型微调时更新的参数？", "training"),
    ("后台进程异常退出后怎样拉起并查看原因？", "linux"),
]


def evaluate(provider):
    with TemporaryDirectory(prefix="recruitops-embedding-eval-") as directory:
        storage = Storage.from_url("sqlite:///" + str(Path(directory) / "kb.db"), initialize=True)
        try:
            service = KnowledgeService(storage, provider)
            started = perf_counter()
            for name, body in DOCUMENTS.items():
                doc = service.queue(name + ".md", body.encode(), "reference")
                service.ingest(doc["id"], doc["revision"])
            assert all(doc["status"] == "ready" for doc in service.list_documents())
            indexing = perf_counter() - started
            latencies, correct, top3, details = [], 0, 0, []
            for query, expected in QUERIES:
                started = perf_counter()
                hits = service.search(query)
                latencies.append((perf_counter() - started) * 1000)
                names = [hit["filename"][:-3] for hit in hits]
                correct += bool(names and names[0] == expected)
                top3 += expected in names[:3]
                details.append({"expected": expected, "results": names[:3]})
            return {"top1": correct, "top3": top3, "queries": len(QUERIES),
                    "index_seconds": round(indexing, 3), "median_query_ms": round(statistics.median(latencies), 1), "details": details}
        finally:
            storage.engine.dispose()


def main():
    config = json.loads((ROOT / ".data/embedding/service.json").read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{config['port']}"
    with httpx.Client(trust_env=False, timeout=20) as client:
        assert client.post(url + "/v1/embeddings", json={"input": ["not authorized"]}).status_code == 401
        health = client.get(url + "/health", headers={"Authorization": "Bearer " + config["api_key"]})
        health.raise_for_status()
        assert health.json()["device"] == "cuda"
    provider = OpenAICompatibleEmbeddingProvider(url + "/v1/embeddings", model="Qwen/Qwen3-Embedding-0.6B",
                                                 api_key=config["api_key"], dimension=1024, query_prefix=QWEN_QUERY_PREFIX)
    result = {"qwen": evaluate(provider), "bge": evaluate(LocalEmbedding(ROOT / ".data/knowledge/models"))}
    assert result["qwen"]["top3"] == len(QUERIES), result
    print(json.dumps(result, ensure_ascii=False))
    (ROOT / ".data/embedding/acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
