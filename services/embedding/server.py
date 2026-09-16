"""Single-process, loopback-only GPU embeddings service. No business data access."""
import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import threading

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

MODEL = "Qwen/Qwen3-Embedding-0.6B"


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = MODEL
    input: list[str] = Field(min_length=1, max_length=16)


class Engine:
    def __init__(self, model_path):
        self.path = model_path
        self.model = None
        self.lock = threading.Lock()

    def load(self):
        import torch
        from sentence_transformers import SentenceTransformer
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; refusing an unnoticed CPU fallback")
        self.model = SentenceTransformer(
            self.path, device="cuda", local_files_only=True,
            model_kwargs={"torch_dtype": torch.float16, "attn_implementation": "sdpa"},
            tokenizer_kwargs={"padding_side": "left"},
        )
        self.model.max_seq_length = 2048
        self.encode(["GPU warmup"])

    def encode(self, texts):
        import torch
        # Only one GPU batch at a time; reject excessive backlog instead of growing it.
        if not self.lock.acquire(timeout=10):
            raise HTTPException(503, "Embedding service busy")
        try:
            tokens = self.model.tokenizer(texts, truncation=False, padding=False)
            lengths = [len(ids) for ids in tokens["input_ids"]]
            if max(lengths) > 2048:
                raise HTTPException(422, "Input exceeds 2048 tokens; split it without truncating")
            with torch.inference_mode():
                vectors = self.model.encode(texts, batch_size=4, normalize_embeddings=True, show_progress_bar=False)
            return vectors.tolist(), sum(lengths)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            raise HTTPException(503, "Insufficient GPU memory; close GPU-heavy applications") from None
        finally:
            self.lock.release()


def create_app(engine, api_key):
    if len(api_key) < 32:
        raise ValueError("An API key of at least 32 characters is required")

    @asynccontextmanager
    async def lifespan(app):
        engine.load()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str | None = Header(default=None)):
        if not secrets.compare_digest(authorization or "", "Bearer " + api_key):
            raise HTTPException(401, "Unauthorized")

    @app.middleware("http")
    async def bound_body(request, call_next):
        from starlette.responses import JSONResponse
        # The local client uses Content-Length; do not accept unbounded chunked uploads.
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 800_000:
                return JSONResponse({"detail": "Request too large or missing Content-Length"}, status_code=413)
        return await call_next(request)

    @app.get("/health", dependencies=[Depends(authorize)])
    def health():
        return {"ready": True, "model": MODEL, "dimension": 1024, "device": "cuda", "max_tokens": 2048}

    @app.post("/v1/embeddings", dependencies=[Depends(authorize)])
    def embeddings(body: EmbeddingRequest):
        if body.model != MODEL:
            raise HTTPException(422, "Unknown model")
        if any(not text.strip() or len(text) > 12_000 for text in body.input):
            raise HTTPException(422, "Empty or oversized input")
        vectors, tokens = engine.encode(body.input)
        return {"object": "list", "model": MODEL,
                "data": [{"object": "embedding", "index": i, "embedding": vector} for i, vector in enumerate(vectors)],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    import uvicorn
    uvicorn.run(create_app(Engine(config["model_path"]), config["api_key"]),
                host="127.0.0.1", port=config.get("port", 8015), workers=1, access_log=False)
