import json
import sqlite3
from pathlib import Path

import yaml

from packages.config import Settings
from packages.repositories.autumn_source import AutumnSourceRepository


def _source(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(data / "jobs.db")
    connection.executescript(
        """
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY, company TEXT, title TEXT, city TEXT,
          job_type TEXT, jd_url TEXT, jd_raw TEXT, published_at TEXT,
          source TEXT, crawled_at TEXT, last_seen_at TEXT, link_kind TEXT,
          is_new INTEGER, screening_tier TEXT, jd_status TEXT,
          jd_checked_at TEXT, link_status TEXT, link_checked_at TEXT,
          cohort INTEGER, cohort_status TEXT, cohort_source TEXT,
          cohort_evidence TEXT, cohort_checked_at TEXT, recruitment_track TEXT
        );
        CREATE TABLE job_analysis (
          job_id INTEGER UNIQUE, match_score INTEGER, advantages TEXT, gaps TEXT,
          summary TEXT, recommendation TEXT, score_breakdown TEXT, evidence TEXT,
          evidence_level TEXT, matched_directions TEXT, primary_match_direction TEXT,
          analysis_status TEXT, model TEXT, analyzed_at TEXT
        );
        INSERT INTO jobs VALUES (
          1, '示例公司', 'C++开发工程师', '上海', '校招',
          'https://example.com/jobs/1', '岗位职责 任职要求', '', 'fixture',
          '2026-08-19T01:00:00+00:00', '2026-08-19T01:00:00+00:00',
          'detail', 1, 'A', 'complete', '', 'ok', '', 2027, 'confirmed',
          'fixture', '2027届', '', 'formal'
        );
        INSERT INTO job_analysis VALUES (
          1, 80, '["C++"]', '["分布式"]', '匹配', '考虑', '{}', '[]',
          'verified', '["C++软件开发"]', 'C++软件开发', 'complete', 'fixture',
          '2026-08-19T01:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()
    (data / "applications.json").write_text(
        json.dumps(
            [
                {
                    "id": 7,
                    "job_id": 1,
                    "company": "示例公司",
                    "title": "C++开发工程师",
                    "current_stage": "written",
                    "events": [
                        {
                            "id": 3,
                            "event_type": "笔试",
                            "event_date": "2026-08-20",
                            "event_time": "19:30",
                            "note": "线上",
                        }
                    ],
                    "applied_at": "2026-08-18",
                    "updated_at": "2026-08-19T02:00:00+08:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "name": "示例公司",
                        "crawler": "fixture",
                        "careers_url": "https://example.com/campus",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return Settings(source_root=tmp_path)


def test_repository_queries_only_frozen_source(tmp_path: Path) -> None:
    repo = AutumnSourceRepository(_source(tmp_path))

    jobs = repo.search_jobs(cohort=2027, cohort_status="confirmed")
    detail = repo.get_job("1")
    applications = repo.list_applications()
    schedule = repo.list_schedule()

    assert jobs.total == 1
    assert jobs.items[0].title == "C++开发工程师"
    assert detail is not None and detail.analysis is not None
    assert detail.analysis.match_score == 80
    assert detail.analysis.advantages == ["C++"]
    assert applications[0].stage.value == "written"
    assert schedule[0].event_date.isoformat() == "2026-08-20"
    assert schedule[0].event_time.isoformat() == "19:30:00"
    assert schedule[0].note == "线上"
    assert repo.list_companies()[0].integration_status == "connected"
