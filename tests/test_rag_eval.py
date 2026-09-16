from evals.rag_runner import run_frozen_rag


def test_frozen_rag_recall_citation_and_refusal_thresholds() -> None:
    summary = run_frozen_rag()

    assert summary.cases == 4
    assert summary.answerable_cases == 3
    assert summary.mean_recall_at_k >= 0.8
    assert summary.mean_citation_precision >= 0.8
    assert summary.refusal_accuracy >= 0.8
