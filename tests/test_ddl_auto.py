"""Phase S4: detect Hibernate ddl-auto schema management with no migration
tool (docs/spring-boot-support.md) — the setup where schema changes never
appear in a PR and review is blind."""

import pytest

import sixta_review as sr


def test_detects_properties_ddl_auto(tmp_path):
    (tmp_path / "application.properties").write_text("spring.jpa.hibernate.ddl-auto=update\n")
    path, value = sr.jpa_ddl_auto_unmanaged(str(tmp_path))
    assert value == "update" and path.endswith("application.properties")


def test_detects_yml_ddl_auto_in_profile(tmp_path):
    resources = tmp_path / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "application-dev.yml").write_text(
        "spring:\n  jpa:\n    hibernate:\n      ddl-auto: create-drop\n"
    )
    assert sr.jpa_ddl_auto_unmanaged(str(tmp_path))[1] == "create-drop"


@pytest.mark.parametrize("value", ["validate", "none"])
def test_safe_ddl_auto_values_do_not_trigger(tmp_path, value):
    (tmp_path / "application.properties").write_text(f"spring.jpa.hibernate.ddl-auto={value}\n")
    assert sr.jpa_ddl_auto_unmanaged(str(tmp_path)) is None


def test_migration_tool_anywhere_suppresses(tmp_path):
    (tmp_path / "application.properties").write_text("spring.jpa.hibernate.ddl-auto=update\n")
    (tmp_path / "pom.xml").write_text("<artifactId>flyway-core</artifactId>")
    assert sr.jpa_ddl_auto_unmanaged(str(tmp_path)) is None


def test_liquibase_in_config_suppresses(tmp_path):
    (tmp_path / "application.yml").write_text(
        "spring:\n  jpa:\n    hibernate:\n      ddl-auto: update\n  liquibase:\n    change-log: x\n"
    )
    assert sr.jpa_ddl_auto_unmanaged(str(tmp_path)) is None


def test_no_spring_files_is_none(tmp_path):
    assert sr.jpa_ddl_auto_unmanaged(str(tmp_path)) is None


def test_note_appended_only_when_run_analyzed_something(tmp_path, monkeypatch):
    (tmp_path / "application.properties").write_text("spring.jpa.hibernate.ddl-auto=update\n")
    monkeypatch.chdir(tmp_path)

    empty: list = []
    sr.append_ddl_auto_note(empty)
    assert empty == []  # logged, but never the sole content of a comment

    reports = [sr.FileReport(path="changes.sql")]
    sr.append_ddl_auto_note(reports)
    assert len(reports) == 2
    note = reports[1]
    assert note.findings[0].check_name == "jpa-ddl-auto-unmanaged"
    assert note.findings[0].severity == "Info"
    assert "schema-generation" in note.sections[0]


def test_note_renders_in_markdown(tmp_path, monkeypatch):
    (tmp_path / "application.yml").write_text("spring:\n  jpa:\n    hibernate:\n      ddl-auto: create\n")
    monkeypatch.chdir(tmp_path)
    reports = [sr.FileReport(path="V1__init.sql")]
    sr.append_ddl_auto_note(reports)
    md = sr.render_markdown(reports, "high")
    assert "ddl-auto" in md and "application.yml" in md
