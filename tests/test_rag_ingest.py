import json

from packages.rag import (
    clean_source_text,
    document_from_profile_config,
    documents_from_json_records,
    documents_from_structured_json_file,
    documents_from_text_files,
    load_json_records,
)


def test_clean_source_text_redacts_credentials_and_normalizes() -> None:
    cleaned = clean_source_text("api_key=secret-value\n\n\n岗位  \t C++")
    assert "secret-value" not in cleaned
    assert "[REDACTED]" in cleaned
    assert "岗位 C++" in cleaned


def test_ingest_text_files_is_deterministic(tmp_path) -> None:
    (tmp_path / "b.md").write_text("第二份", encoding="utf-8")
    (tmp_path / "a.md").write_text("第一份", encoding="utf-8")
    docs = documents_from_text_files(tmp_path, source="knowledge", metadata={"domain": "crawler"})
    assert [doc.source_ref for doc in docs] == [
        (tmp_path / "a.md").as_posix(),
        (tmp_path / "b.md").as_posix(),
    ]
    assert docs[0].metadata["domain"] == "crawler"


def test_ingest_json_records_keeps_source_refs(tmp_path) -> None:
    path = tmp_path / "records.json"
    path.write_text(json.dumps([{"id": "job-1", "title": "C++", "jd_raw": "ROS"}]), encoding="utf-8")
    records = load_json_records(path)
    docs = documents_from_json_records(records, source="jobs", metadata={"domain": "job"})
    assert docs[0].source_ref == "job-1"
    assert docs[0].content == "C++\nROS"


def test_profile_ingest_reads_only_allowlisted_section(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "profile:\n  skills: [C++, ROS]\n  direction: 机器人\n"
        "deepseek:\n  api_key: never-import-this\ncompanies: []\n",
        encoding="utf-8",
    )

    document = document_from_profile_config(path)

    assert "C++" in document.content
    assert "机器人" in document.content
    assert "never-import-this" not in document.content
    assert "companies" not in document.content


def test_profile_ingest_excludes_learning_targets_and_unverified_skills(
    tmp_path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "profile:\n"
        "  skills: [C++]\n"
        "  matching:\n"
        "    project_evidence: [ROS2机械臂]\n"
        "    learning_targets: [尚未掌握的CUDA]\n"
        "    unverified_skills: [虚构技能]\n",
        encoding="utf-8",
    )

    document = document_from_profile_config(path)

    assert "ROS2机械臂" in document.content
    assert "尚未掌握的CUDA" not in document.content
    assert "虚构技能" not in document.content


def test_structured_json_ingest_supports_mapping_and_redacts_nested_secrets(
    tmp_path,
) -> None:
    path = tmp_path / "recipes.json"
    path.write_text(
        json.dumps(
            {
                "Moka": {
                    "type": "api",
                    "request": {
                        "url": "https://example.test/jobs",
                        "headers": {"Authorization": "Bearer private"},
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    documents = documents_from_structured_json_file(
        path,
        source="crawler_recipe",
        metadata={"domain": "crawler"},
    )

    assert len(documents) == 1
    assert documents[0].source_ref.endswith("recipes.json#Moka")
    assert "https://example.test/jobs" in documents[0].content
    assert "Bearer private" not in documents[0].content
    assert "[REDACTED]" in documents[0].content
