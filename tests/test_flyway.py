"""Phase S1 Spring Boot / Flyway support (docs/spring-boot-support.md): naming
conventions, ${placeholder} substitution, engine auto-detection, rollback-artifact
handling, and Java-migration flagging. All offline — no network, no subprocess."""

import os

import pytest

import sixta_review as sr


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# Naming / dispatch
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("src/main/java/com/acme/db/migration/V3__Backfill.java", True),
    ("db/migration/V1_2__Init.java", True),
    ("src/main/java/db/migration/tenant/R__Rebuild_views.java", True),
    ("src/main/java/com/acme/OrderService.java", False),
    ("db/migration/V3__add_index.sql", False),  # .sql goes through the plain-SQL path
    ("db/migrations/V3__X.java", False),  # not the Flyway directory name
])
def test_flyway_java_target(path, expected):
    assert sr.flyway_java_target(path) is expected


def test_is_migration_file_includes_flyway_java():
    assert sr.is_migration_file("src/main/java/com/acme/db/migration/V3__Backfill.java")


def test_extract_migration_java_is_flagged_not_analyzed():
    sql, manual = sr.extract_migration("src/main/java/com/acme/db/migration/V3__Backfill.java", _opts())
    assert sql == ""
    finding, section = manual
    assert finding.check_name == "flyway-java-manual-review"
    assert finding.severity == "Info"
    assert "human review" in section


# --------------------------------------------------------------------------
# Rollback artifacts are not forward changes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,forward", [
    ("U7__drop_index.sql", "V7__add_index.sql"),
    ("foo.down.sql", "foo.sql"),
    ("foo.rollback.sql", "foo.sql"),
])
def test_extract_migration_rollback_artifact_is_flagged_not_analyzed(tmp_path, name, forward):
    (tmp_path / forward).write_text("CREATE INDEX i ON t (c);")
    f = tmp_path / name
    f.write_text("DROP INDEX i;")
    sql, manual = sr.extract_migration(str(f), _opts())
    assert sql == ""
    finding, _section = manual
    assert finding.check_name == "rollback-artifact"
    assert finding.severity == "Info"


@pytest.mark.parametrize("name", ["Update_prices__2024.sql", "Users__seed.sql", "U7__drop_index.sql", "0001_x.down.sql"])
def test_unpaired_rollback_lookalikes_are_analyzed_normally(tmp_path, name):
    """A rollback-shaped NAME is not proof: without the forward companion the
    file is a normal change (Update_prices__2024.sql matches the U-pattern)."""
    f = tmp_path / name
    f.write_text("UPDATE prices SET amount = amount * 1.1;")
    sql, manual = sr.extract_migration(str(f), _opts())
    assert "UPDATE prices" in sql and manual is None


def test_run_v1_undo_only_changeset_sends_no_extractions(tmp_path):
    """A changed undo file alone must not POST anything (its content is only
    meaningful attached to a forward migration)."""
    (tmp_path / "V7__add_index.sql").write_text("CREATE INDEX i ON t (c);")
    f = tmp_path / "U7__drop_index.sql"
    f.write_text("DROP TABLE orders;")

    class _NoPost:
        def analyze_v1(self, request):
            raise AssertionError("no extraction should be sent for an undo-only changeset")

    reports, renders, context, worst, _badge, _outcomes = sr.run_v1([str(f)], _opts(), _NoPost(), hints={})
    assert renders is None and context is None and worst is None
    assert reports[0].findings[0].check_name == "rollback-artifact"


# --------------------------------------------------------------------------
# Repeatable migrations
# --------------------------------------------------------------------------

def test_sql_rollback_repeatable_is_unchecked(tmp_path):
    f = tmp_path / "R__order_totals_view.sql"
    f.write_text("CREATE OR REPLACE VIEW order_totals AS SELECT 1;")
    assert sr.sql_rollback(str(f)) is None


def test_extract_migration_repeatable_is_analyzed(tmp_path):
    f = tmp_path / "R__order_totals_view.sql"
    f.write_text("CREATE OR REPLACE VIEW order_totals AS SELECT 1;")
    sql, manual = sr.extract_migration(str(f), _opts())
    assert "order_totals" in sql and manual is None


# --------------------------------------------------------------------------
# ${placeholder} substitution
# --------------------------------------------------------------------------

def test_substitute_placeholders_configured_and_fallback():
    sql = "GRANT SELECT ON ${tenant_schema}.orders TO ${app_user}; -- ${flyway:defaultSchema}"
    out = sr.substitute_flyway_placeholders(sql, {"tenant_schema": "public"})
    assert "public.orders" in out
    assert "TO app_user;" in out  # unconfigured: the name itself
    assert "flyway_defaultSchema" in out  # non-identifier chars sanitized


def test_flyway_placeholders_from_properties(tmp_path, monkeypatch):
    (tmp_path / "src/main/resources").mkdir(parents=True)
    (tmp_path / "src/main/resources/application.properties").write_text(
        "spring.flyway.placeholders.app_user=app_rw\n"
        "spring.flyway.placeholders.tenant-schema: public\n"
        "spring.datasource.url=jdbc:postgresql://db/prod\n"
    )
    monkeypatch.chdir(tmp_path)
    assert sr.flyway_placeholders() == {"app_user": "app_rw", "tenant-schema": "public"}


def test_flyway_placeholders_from_yml(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    (tmp_path / "application.yml").write_text(
        "spring:\n  flyway:\n    placeholders:\n      app_user: app_rw\n"
    )
    monkeypatch.chdir(tmp_path)
    assert sr.flyway_placeholders() == {"app_user": "app_rw"}


def test_undo_companion_is_substituted_too(tmp_path, monkeypatch):
    (tmp_path / "application.properties").write_text("spring.flyway.placeholders.tenant_schema=public\n")
    (tmp_path / "V2__add_index.sql").write_text("CREATE INDEX i ON ${tenant_schema}.orders (status);")
    (tmp_path / "U2__drop_index.sql").write_text("DROP INDEX ${tenant_schema}.i;")
    monkeypatch.chdir(tmp_path)
    assert sr.sql_rollback(str(tmp_path / "V2__add_index.sql")) == {"sql": "DROP INDEX public.i;"}


def test_extract_migration_substitutes_in_flyway_files_only(tmp_path, monkeypatch):
    (tmp_path / "application.properties").write_text("spring.flyway.placeholders.app_user=app_rw\n")
    flyway = tmp_path / "V5__grants.sql"
    flyway.write_text("ALTER TABLE ${app_user}_audit ADD COLUMN note text;")
    plain = tmp_path / "changes.sql"
    plain.write_text("ALTER TABLE ${app_user}_audit ADD COLUMN note text;")
    monkeypatch.chdir(tmp_path)

    sql, _ = sr.extract_migration(str(flyway), _opts())
    assert "app_rw_audit" in sql
    sql, _ = sr.extract_migration(str(plain), _opts())
    assert "${app_user}" in sql  # non-Flyway-named .sql is untouched


# --------------------------------------------------------------------------
# Engine auto-detection
# --------------------------------------------------------------------------

def test_detect_engine_pom_flyway_module(tmp_path):
    (tmp_path / "pom.xml").write_text("<artifactId>flyway-database-postgresql</artifactId>")
    assert sr.detect_engine(str(tmp_path)) == "postgresql"


def test_detect_engine_gradle_mysql_connector(tmp_path):
    (tmp_path / "build.gradle.kts").write_text('runtimeOnly("com.mysql:mysql-connector-j")')
    assert sr.detect_engine(str(tmp_path)) == "mysql"


def test_detect_engine_application_yml_datasource_url(tmp_path):
    resources = tmp_path / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "application.yml").write_text(
        "spring:\n  datasource:\n    url: jdbc:postgresql://db:5432/prod\n"
    )
    assert sr.detect_engine(str(tmp_path)) == "postgresql"


def test_detect_engine_mariadb_maps_to_mysql(tmp_path):
    (tmp_path / "application.properties").write_text("spring.datasource.url=jdbc:mariadb://db/prod\n")
    assert sr.detect_engine(str(tmp_path)) == "mysql"


def test_detect_engine_ambiguous_abstains(tmp_path, capsys):
    (tmp_path / "pom.xml").write_text("flyway-database-postgresql mysql-connector-j")
    assert sr.detect_engine(str(tmp_path)) is None
    assert "set --engine" in capsys.readouterr().err


def test_detect_engine_nothing_to_detect(tmp_path):
    assert sr.detect_engine(str(tmp_path)) is None


def test_engine_flag_defaults_to_auto_and_env_overrides(monkeypatch):
    assert sr.build_parser().parse_args([]).engine == "auto"
    monkeypatch.setenv("SIXTA_ENGINE", "mysql")
    assert sr.build_parser().parse_args([]).engine == "mysql"


def test_resolve_engine_explicit_wins(monkeypatch):
    monkeypatch.setattr(sr, "detect_engine", lambda root=".": pytest.fail("must not detect"))
    assert sr.resolve_engine("mysql") == "mysql"


def test_resolve_engine_auto_detects_and_falls_back(monkeypatch):
    monkeypatch.setattr(sr, "detect_engine", lambda root=".": "mysql")
    assert sr.resolve_engine("auto") == "mysql"
    monkeypatch.setattr(sr, "detect_engine", lambda root=".": None)
    assert sr.resolve_engine("auto") == "postgresql"
    assert sr.resolve_engine(None) == "postgresql"
