"""Owner-uploaded documents: bounded ingestion, local embeddings and BM25/RRF retrieval."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import io
import logging
import re
import threading
from uuid import uuid4

import jieba
import numpy as np
from pgvector.sqlalchemy import Vector
from rank_bm25 import BM25Plus
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, delete, select, update

from packages.config import get_settings
from packages.rag.embeddings import OpenAICompatibleEmbeddingProvider, QWEN_QUERY_PREFIX
from packages.storage import Base, Storage

jieba.setLogLevel(logging.WARNING)
MODEL = "BAAI/bge-small-zh-v1.5"
MAX_BYTES = 5_000_000
KINDS = {"personal", "reference", "notes"}


class KnowledgeDocument(Base):
    __tablename__ = "personal_knowledge_documents"
    id = Column(String(32), primary_key=True)
    filename = Column(String(255), nullable=False)
    kind = Column(String(20), nullable=False)
    revision = Column(String(32), nullable=False)
    content_hash = Column(String(64), nullable=False)
    content = Column(LargeBinary, nullable=False)
    status = Column(String(20), nullable=False)
    error = Column(Text, nullable=False, default="")
    pages = Column(JSON, nullable=False, default=list)
    chunk_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class KnowledgeChunk(Base):
    __tablename__ = "personal_knowledge_chunks"
    id = Column(String(32), primary_key=True)
    document_id = Column(String(32), ForeignKey("personal_knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    page = Column(Integer, nullable=False)
    section = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector().with_variant(JSON, "sqlite"), nullable=False)
    model = Column(String(255), nullable=False)


class LocalEmbedding:
    dimension = 512
    version = MODEL

    def __init__(self, cache_dir):
        self.cache_dir = str(cache_dir)
        self._model = None
        self._lock = threading.Lock()

    def embed_many(self, texts):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(MODEL, cache_dir=self.cache_dir, threads=2)
            return [vector.tolist() for vector in self._model.embed(texts, batch_size=16)]

    def embed(self, text):
        return self.embed_many([text])[0]


def tokenize(text):
    stop = {"的", "了", "和", "是", "在", "我", "你", "有", "哪些", "什么", "这个", "一个", "如何", "请", "与", "中", "对", "及"}
    tokens = []
    for part in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_+#.-]*|[\u3400-\u9fff]+", text.casefold()):
        tokens.extend(word for word in jieba.cut(part) if word.strip() and word not in stop)
    return tokens


def extract_pages(filename, content):
    if filename.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or len(reader.pages) > 80:
            raise ValueError("PDF 加密或超过 80 页，请拆分后导入")
        pages = [page.extract_text() or "" for page in reader.pages]
        if any(not text.strip() for text in pages):
            raise ValueError("PDF 存在无法提取文字的页面，请使用文字版资料；未建立不完整索引")
    else:
        pages = [content.decode("utf-8-sig")]
    if not pages or not "".join(pages).strip() or sum(map(len, pages)) > 100_000:
        raise ValueError("正文为空或超过 10 万字符，请拆分后导入")
    if any("\x00" in text for text in pages):
        raise ValueError("文件不是有效的文本资料")
    return pages


def split_pages(pages):
    chunks = []
    for page, text in enumerate(pages, 1):
        section = "正文"
        for paragraph in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text, flags=re.M):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            heading = re.match(r"^#{1,6}\s+([^\n]+)", paragraph)
            if heading:
                section = heading[1][:180]
            # Keep chunks below the local model's token window, with a small overlap.
            start = 0
            while start < len(paragraph):
                end = min(start + 350, len(paragraph))
                chunks.append({"page": page, "section": section, "content": paragraph[start:end]})
                if end == len(paragraph):
                    break
                start = end - 40
    if len(chunks) > 500:
        raise ValueError("资料段落过多，请按章节拆分后导入")
    return chunks


def document_data(row):
    return {"id": row.id, "filename": row.filename, "kind": row.kind, "revision": row.revision,
            "status": row.status, "error": row.error, "chunk_count": row.chunk_count,
            "updated_at": row.updated_at.isoformat(), "page_count": len(row.pages)}


class KnowledgeService:
    def __init__(self, storage, provider):
        self.storage = storage
        self.provider = provider

    @property
    def model(self):
        return str(self.provider.version)

    def list_documents(self):
        with self.storage.session() as session:
            return [document_data(row) for row in session.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc()))]

    def queue(self, filename, content, kind="notes", document_id=None, revision=None):
        if not filename or len(filename) > 255 or re.search(r"[/\\]", filename):
            raise ValueError("文件名无效")
        if not filename.lower().endswith((".md", ".txt", ".pdf")):
            raise ValueError("仅支持 Markdown、TXT 和文字版 PDF")
        if not content or len(content) > MAX_BYTES or kind not in KINDS:
            raise ValueError("资料为空、超过 5 MB 或类型无效")
        digest = sha256(content).hexdigest()
        with self.storage.transaction(write=True) as session:
            if document_id:
                row = session.get(KnowledgeDocument, document_id, with_for_update=True)
                if row is None:
                    raise LookupError("资料不存在")
                if row.revision != revision:
                    raise ValueError("资料已更新，请刷新后再操作")
            else:
                row = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.content_hash == digest, KnowledgeDocument.kind == kind))
                if row and row.status == "ready":
                    return {**document_data(row), "reused": True}
                if row is None:
                    if len(session.scalars(select(KnowledgeDocument.id)).all()) >= 100:
                        raise ValueError("第一版最多保存 100 份资料")
                    row = KnowledgeDocument(id=uuid4().hex)
                    session.add(row)
            if row.status == "processing":
                raise ValueError("资料正在处理，请稍后重试")
            row.filename, row.kind, row.content_hash, row.content = filename, kind, digest, content
            row.revision, row.status, row.error = uuid4().hex, "processing", ""
            row.pages, row.chunk_count = [], 0
            row.updated_at = datetime.now(timezone.utc)
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == row.id))
            session.flush()
            return {**document_data(row), "reused": False}

    def ingest(self, document_id, revision):
        with self.storage.session() as session:
            row = session.get(KnowledgeDocument, document_id)
            if row is None or row.revision != revision or row.status != "processing":
                return
            filename, content = row.filename, row.content
        try:
            pages = extract_pages(filename, content)
            chunks = split_pages(pages)
            inputs = [chunk["content"] for chunk in chunks]
            many = getattr(self.provider, "embed_many", None)
            vectors = list(many(inputs)) if many else [self.provider.embed(text) for text in inputs]
            array = np.asarray(vectors, dtype=float)
            if array.shape != (len(chunks), self.provider.dimension) or not np.isfinite(array).all() or np.any(np.linalg.norm(array, axis=1) == 0):
                raise ValueError("向量服务返回无效数据")
            with self.storage.transaction(write=True) as session:
                row = session.get(KnowledgeDocument, document_id, with_for_update=True)
                if row is None or row.revision != revision or row.status != "processing":
                    return
                for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                    session.add(KnowledgeChunk(id=uuid4().hex, document_id=document_id, ordinal=ordinal,
                                              **chunk, embedding=vector, model=self.model))
                row.pages, row.chunk_count, row.status = pages, len(chunks), "ready"
        except Exception as exc:
            error = str(exc) if isinstance(exc, (ValueError, UnicodeError)) else "解析或向量服务失败，请检查依赖、模型下载和网络后重试"
            with self.storage.transaction(write=True) as session:
                session.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == document_id, KnowledgeDocument.revision == revision).values(status="failed", error=error[:300]))

    def delete_document(self, document_id, revision):
        with self.storage.transaction(write=True) as session:
            row = session.get(KnowledgeDocument, document_id, with_for_update=True)
            if row is None:
                raise LookupError("资料不存在")
            if row.revision != revision:
                raise ValueError("资料已更新，请刷新后再删除")
            session.delete(row)

    def retry(self, document_id, revision):
        with self.storage.session() as session:
            row = session.get(KnowledgeDocument, document_id)
            if row is None:
                raise LookupError("资料不存在")
            if row.status != "failed":
                raise ValueError("仅失败的资料可以重试")
            filename, content, kind = row.filename, row.content, row.kind
        return self.queue(filename, content, kind, document_id, revision)

    def recover_interrupted(self):
        with self.storage.transaction(write=True) as session:
            session.execute(update(KnowledgeDocument).where(KnowledgeDocument.status == "processing").values(
                status="failed", error="服务重启导致索引中断，可以重试"))

    def reindex(self, document_id):
        """Replace only vectors after success; preserve source revisions and old index on failure."""
        with self.storage.session() as session:
            doc = session.get(KnowledgeDocument, document_id)
            if doc is None or doc.status != "ready":
                raise ValueError("仅可重建已就绪资料的索引")
            revision = doc.revision
            old = session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id).order_by(KnowledgeChunk.ordinal)).all()
            if old and all(chunk.model == self.model for chunk in old):
                return False
            chunks = split_pages(doc.pages)
        many = getattr(self.provider, "embed_many", None)
        vectors = list(many([c["content"] for c in chunks])) if many else [self.provider.embed(c["content"]) for c in chunks]
        array = np.asarray(vectors, dtype=float)
        if array.shape != (len(chunks), self.provider.dimension) or not np.isfinite(array).all() or np.any(np.linalg.norm(array, axis=1) == 0):
            raise ValueError("向量服务返回无效数据，原索引未修改")
        with self.storage.transaction(write=True) as session:
            doc = session.get(KnowledgeDocument, document_id, with_for_update=True)
            if doc is None or doc.revision != revision or doc.status != "ready":
                raise ValueError("资料在重建过程中发生变更，未覆盖索引")
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))
            for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(KnowledgeChunk(id=uuid4().hex, document_id=document_id, ordinal=ordinal, **chunk, embedding=vector, model=self.model))
            doc.chunk_count = len(chunks)
        return True

    def read(self, document_id, *, page=1, revision=None):
        with self.storage.session() as session:
            row = session.get(KnowledgeDocument, document_id)
            if row is None:
                raise LookupError("资料已删除或不存在")
            if revision and revision != row.revision:
                raise ValueError("引用来自旧版本，原文已经更新，请重新检索")
            if row.status != "ready":
                raise ValueError("资料尚未完成索引")
            if page < 1 or page > len(row.pages):
                raise ValueError("页码无效")
            return {**document_data(row), "page": page, "text": row.pages[page - 1]}

    def search(self, query, *, document_id=None, top_k=5):
        with self.storage.session() as session:
            statement = select(KnowledgeChunk, KnowledgeDocument).join(KnowledgeDocument).where(KnowledgeDocument.status == "ready", KnowledgeChunk.model == self.model)
            if document_id:
                statement = statement.where(KnowledgeChunk.document_id == document_id)
            rows = session.execute(statement.order_by(KnowledgeChunk.document_id, KnowledgeChunk.ordinal)).all()
            if not rows:
                return []
            # Explicitly selected short sources bypass retrieval to preserve their full context.
            if document_id and len(rows[0][1].pages) == 1 and len(rows[0][1].pages[0]) <= 3000:
                chunk, doc = rows[0]
                return [self.hit(chunk, doc, doc.pages[0], mode="full_document")]
            corpus = [tokenize(chunk.content) for chunk, _ in rows]
            terms = tokenize(query)
            scores = BM25Plus(corpus).get_scores(terms) if any(corpus) and terms else np.zeros(len(rows))
            embed_query = getattr(self.provider, "embed_query", self.provider.embed)
            q = np.asarray(embed_query(query), dtype=float)
            if q.shape != (self.provider.dimension,) or not np.isfinite(q).all() or not np.linalg.norm(q):
                raise ValueError("查询向量无效")
            vectors = np.asarray([chunk.embedding for chunk, _ in rows], dtype=float)
            cosine = vectors @ q / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(q))
            lexical = sorted((i for i, tokens in enumerate(corpus) if set(terms).intersection(tokens)), key=lambda i: -scores[i])[:10]
            semantic = sorted((i for i, value in enumerate(cosine) if value >= 0.5), key=lambda i: -cosine[i])[:10]
            fused = {}
            for route in (lexical, semantic):
                for rank, i in enumerate(route, 1):
                    fused[i] = fused.get(i, 0) + 1 / (60 + rank)
            hits, seen = [], set()
            for i in sorted(fused, key=lambda i: (-fused[i], rows[i][0].id)):
                chunk, doc = rows[i]
                digest = sha256(chunk.content.encode()).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                adjacent = [other for other, _ in rows if other.document_id == doc.id and other.page == chunk.page and other.section == chunk.section and abs(other.ordinal - chunk.ordinal) <= 1]
                context = "\n\n".join(other.content for other in sorted(adjacent, key=lambda c: c.ordinal))
                hits.append({**self.hit(chunk, doc, context), "bm25": float(scores[i]), "cosine": float(cosine[i]), "rrf": fused[i]})
                if len(hits) >= top_k:
                    break
            return hits

    @staticmethod
    def hit(chunk, doc, content, mode="hybrid"):
        return {"chunk_id": chunk.id, "document_id": doc.id, "filename": doc.filename,
                "kind": doc.kind, "revision": doc.revision, "page": chunk.page,
                "section": chunk.section, "content": content, "mode": mode,
                "url": f"/?knowledge={doc.id}&page={chunk.page}&revision={doc.revision}"}


@lru_cache(maxsize=1)
def get_knowledge_service():
    settings = get_settings()
    prefix = "knowledge_embedding" if settings.knowledge_embedding_endpoint else "embedding"
    endpoint = getattr(settings, prefix + "_endpoint")
    model = getattr(settings, prefix + "_model")
    provider = (OpenAICompatibleEmbeddingProvider(endpoint, model=model,
                 api_key=getattr(settings, prefix + "_api_key") or None,
                 dimension=getattr(settings, prefix + "_dimension"),
                 query_prefix=QWEN_QUERY_PREFIX if model == "Qwen/Qwen3-Embedding-0.6B" else "")
                if endpoint else LocalEmbedding(settings.agent_root / ".data/knowledge/models"))
    return KnowledgeService(Storage.from_url(settings.database_url), provider)
