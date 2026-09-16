from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from packages.tools.oc_candidates import SubprocessCandidateCrawlerProcess


def test_candidate_process_passes_reserved_crawl_budget(monkeypatch):
    monkeypatch.setenv("RECRUITOPS_CRAWL_TIMEOUT_SECONDS", "inherited-budget")
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"jobs": []}),
        stderr="",
    )

    with patch("packages.tools.oc_candidates.subprocess.run", return_value=completed) as run:
        result = SubprocessCandidateCrawlerProcess()(
            company="Example",
            crawler_key="render",
            source_url="https://example.com/jobs",
            timeout_seconds=120.0,
        )

    assert result == {"jobs": []}
    kwargs = run.call_args.kwargs
    assert kwargs["timeout"] == 120.0
    assert kwargs["env"]["RECRUITOPS_CRAWL_TIMEOUT_SECONDS"] == "115.000000"
    assert kwargs["env"] is not os.environ
    assert os.environ["RECRUITOPS_CRAWL_TIMEOUT_SECONDS"] == "inherited-budget"
