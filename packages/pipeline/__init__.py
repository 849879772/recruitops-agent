"""Independent daily recruitment discovery pipeline for RecruitOps-Agent."""

from .daily import (
    DEFAULT_COMPANIES_PATH,
    PIPELINE_SOURCE,
    CompanyConfigError,
    CompanyRunResult,
    CrawlResult,
    CrawlerProtocol,
    DailyPipelineResult,
    DailyRecruitmentPipeline,
    MatcherProtocol,
    MatchingServiceAdapter,
    PipelineCompany,
    PipelineError,
    DeterministicMatcher,
    job_content_fingerprint,
    load_companies,
    run_daily_pipeline,
)

__all__ = [
    "DEFAULT_COMPANIES_PATH",
    "PIPELINE_SOURCE",
    "CompanyConfigError",
    "CompanyRunResult",
    "CrawlResult",
    "CrawlerProtocol",
    "DailyPipelineResult",
    "DailyRecruitmentPipeline",
    "DeterministicMatcher",
    "MatcherProtocol",
    "MatchingServiceAdapter",
    "PipelineCompany",
    "PipelineError",
    "job_content_fingerprint",
    "load_companies",
    "run_daily_pipeline",
]
