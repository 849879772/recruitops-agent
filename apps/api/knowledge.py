"""Same-origin owner UI for personal documents; the agent only gets read tools."""
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.api.configuration import Upload, decode_upload, require_owner
from packages.personal_knowledge import get_knowledge_service
from packages.config import get_settings

router = APIRouter(prefix="/api/local-ui/knowledge", tags=["knowledge"])


class ImportDocument(Upload):
    kind: str = "notes"
    document_id: str | None = Field(default=None, max_length=32)
    revision: str | None = Field(default=None, max_length=32)


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=32, max_length=32)
    revision: str | None = Field(default=None, max_length=32)
    page: int = Field(default=1, ge=1, le=80)


def call(operation):
    require_owner()
    try:
        return operation(get_knowledge_service())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/list")
def list_documents():
    return call(lambda service: {"documents": service.list_documents(), "embedding_model": service.model,
                                 "local_embedding": not bool(get_settings().embedding_endpoint)})


@router.post("/upload")
def upload_document(body: ImportDocument, background: BackgroundTasks):
    def run(service):
        data = service.queue(body.filename, decode_upload(body), body.kind, body.document_id, body.revision)
        if not data["reused"]:
            background.add_task(service.ingest, data["id"], data["revision"])
        return data
    return call(run)


@router.post("/read")
def read_document(body: DocumentRequest):
    return call(lambda service: service.read(body.document_id, page=body.page, revision=body.revision))


@router.post("/retry")
def retry_document(body: DocumentRequest, background: BackgroundTasks):
    def run(service):
        data = service.retry(body.document_id, body.revision)
        background.add_task(service.ingest, data["id"], data["revision"])
        return data
    return call(run)


@router.post("/delete")
def delete_document(body: DocumentRequest):
    def run(service):
        service.delete_document(body.document_id, body.revision)
        return {"deleted": True}
    return call(run)
