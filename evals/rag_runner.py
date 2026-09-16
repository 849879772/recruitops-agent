from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from packages.rag import DocumentChunk, EvidenceGrounder, LexicalCosineRetriever

from .rag_metrics import RagEvalCase, RagEvalSummary, evaluate_grounder


class RagFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentChunk]
    cases: list[RagEvalCase]


def run_frozen_rag(path: Path | None = None) -> RagEvalSummary:
    fixture_path = path or Path(__file__).parent / "fixtures" / "rag_cases.json"
    fixture = RagFixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    retriever = LexicalCosineRetriever(lexical_weight=1.0, cosine_weight=0.0)
    retriever.add(fixture.documents)
    return evaluate_grounder(EvidenceGrounder(retriever, minimum_score=0.4), fixture.cases)


if __name__ == "__main__":
    summary = run_frozen_rag()
    print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
    passed = (
        summary.mean_recall_at_k >= 0.8
        and summary.mean_citation_precision >= 0.8
        and summary.refusal_accuracy >= 0.8
    )
    raise SystemExit(0 if passed else 1)
