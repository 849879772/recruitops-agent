"""Real parser + real pipeline, synthetic transport; no user data or network."""
import json
from collections import Counter
import pytest
from sqlalchemy import select

from packages.matching.client import DeepSeekClient
from packages.matching.service import MatchingService
from packages.pipeline import run_daily_pipeline
from packages.storage import JobSnapshot, JobAnalysisSnapshot
from tests.test_matching_output_contract import _valid_output
from tests.test_title_first_pipeline import _storage, _config, _company, _job, _crawl, _detail


@pytest.mark.parametrize("failure", ["token_limit", "missing_dimensions"])
def test_one_invalid_model_response_cannot_abort_other_company(tmp_path, failure):
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("a"), _company("b"))
    calls = Counter()
    def transport(endpoint, headers, payload, timeout):
        bad = "Broken" in payload["input"]
        calls["bad" if bad else "good"] += 1
        if bad and failure == "token_limit":
            return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        result = _valid_output()
        if bad:
            del result["score_breakdown"]["core_direction"]
            del result["score_breakdown"]["required_skills"]
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps({"result": result})}]}]}
    client = DeepSeekClient(api_key="synthetic", model="deepseek-flash",
                            transport=transport, max_attempts=1)
    run_daily_pipeline(
        companies_path=config, storage=storage,
        profile={"skills": ["Python"], "matching": {"title_keywords": ["Python"],
                 "project_evidence": ["使用 Python 实现过文档检索项目"]}},
        crawler=lambda company: _crawl(_job(company.id, "Python Broken Engineer" if company.id == "a"
                                                       else "Python Search Engineer")),
        jd_hydrator=_detail, matcher=MatchingService(client),
    )
    with storage.session() as session:
        jobs = {row.id: row for row in session.scalars(select(JobSnapshot))}
        analyses = {row.job_id: row for row in session.scalars(select(JobAnalysisSnapshot))}
        assert set(jobs) == {"a", "b"}
        assert jobs["a"].jd_raw and jobs["b"].jd_raw
        assert jobs["a"].match_score is None
        assert jobs["b"].match_score == 82
        assert analyses["a"].analysis_status == "failed"
        assert analyses["b"].analysis_status == "complete"
    assert calls == {"bad": 3, "good": 1}
    storage.engine.dispose()
