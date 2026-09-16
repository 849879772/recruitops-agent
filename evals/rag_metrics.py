from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from packages.rag import EvidenceGrounder


class RagEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RagEvalCase(RagEvalModel):
    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    relevant_source_refs: set[str] = Field(default_factory=set)
    should_answer: bool = True
    metadata_filter: dict[str, object] = Field(default_factory=dict)


class RagCaseResult(RagEvalModel):
    case_id: str
    answerable: bool
    cited_source_refs: list[str]
    recall_at_k: float = Field(ge=0.0, le=1.0)
    citation_precision: float = Field(ge=0.0, le=1.0)
    refusal_correct: bool


class RagEvalSummary(RagEvalModel):
    cases: int = Field(ge=0)
    answerable_cases: int = Field(ge=0)
    mean_recall_at_k: float = Field(ge=0.0, le=1.0)
    mean_citation_precision: float = Field(ge=0.0, le=1.0)
    refusal_accuracy: float = Field(ge=0.0, le=1.0)
    results: list[RagCaseResult]


def evaluate_grounder(
    grounder: EvidenceGrounder,
    cases: Iterable[RagEvalCase],
    *,
    top_k: int = 5,
) -> RagEvalSummary:
    case_list = list(cases)
    results: list[RagCaseResult] = []
    for case in case_list:
        grounded = grounder.collect(
            case.query,
            top_k=top_k,
            metadata_filter=case.metadata_filter or None,
        )
        cited = [citation.source_ref for citation in grounded.citations]
        relevant = set(case.relevant_source_refs)
        hits = len(relevant.intersection(cited))
        recall = hits / len(relevant) if relevant else (1.0 if not grounded.answerable else 0.0)
        precision = hits / len(cited) if cited else (1.0 if not case.should_answer else 0.0)
        results.append(
            RagCaseResult(
                case_id=case.case_id,
                answerable=grounded.answerable,
                cited_source_refs=cited,
                recall_at_k=recall,
                citation_precision=precision,
                refusal_correct=grounded.answerable is case.should_answer,
            )
        )

    answerable_results = [
        result
        for result, case in zip(results, case_list, strict=True)
        if case.should_answer
    ]
    count = len(results)
    return RagEvalSummary(
        cases=count,
        answerable_cases=len(answerable_results),
        mean_recall_at_k=(
            sum(item.recall_at_k for item in answerable_results) / len(answerable_results)
            if answerable_results
            else 0.0
        ),
        mean_citation_precision=(
            sum(item.citation_precision for item in answerable_results) / len(answerable_results)
            if answerable_results
            else 0.0
        ),
        refusal_accuracy=(
            sum(item.refusal_correct for item in results) / count if count else 0.0
        ),
        results=results,
    )


__all__ = [
    "RagCaseResult",
    "RagEvalCase",
    "RagEvalSummary",
    "evaluate_grounder",
]
