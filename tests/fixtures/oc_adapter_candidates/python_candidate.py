def parse_fixture(fixture):
    jobs = []
    for row in fixture.get("rows", []):
        jobs.append({
            "id": row["jobId"],
            "title": row["name"],
            "city": row.get("location"),
            "jd_url": f"https://careers.example.test/jobs/{row['jobId']}",
            "jd_raw": row.get("description", ""),
            "cohort": 2027,
            "cohort_status": "confirmed",
            "cohort_source": "frozen official fixture",
            "cohort_evidence": "2027 campus recruitment fixture",
            "recruitment_track": "formal",
        })
    return {
        "jobs": jobs,
        "pagination_complete": True,
        "pages_seen": 1,
        "total_pages": 1,
        "has_more": False,
        "advertised_total": len(jobs),
    }
