"""Phase S2 Liquibase support (docs/spring-boot-support.md): formatted-SQL
changelog parsing (changesets, --rollback, --property), new-changeset diffing
against the base, offline CLI rendering of XML/YAML changelogs, and the rollback
audit for both families. All offline — subprocess is monkeypatched."""

import types

import pytest

import sixta_review as sr


def _proc(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1", "--engine", "postgresql"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


FORMATTED = """--liquibase formatted sql
--property name=schema value=public

--changeset ewen:1
CREATE TABLE ${schema}.orders (id bigint PRIMARY KEY);
--rollback DROP TABLE ${schema}.orders;

--changeset ewen:2 labels:perf
--comment adds the hot-path index
CREATE INDEX idx_orders_status ON ${schema}.orders (status);
--rollback DROP INDEX ${schema}.idx_orders_status;
"""

FORMATTED_NO_ROLLBACK = """--liquibase formatted sql

--changeset ewen:3
ALTER TABLE orders ADD COLUMN note text;
"""

FORMATTED_EMPTY_ROLLBACK = """--liquibase formatted sql

--changeset ewen:4
CREATE INDEX i ON t (c);
--rollback empty
"""


# --------------------------------------------------------------------------
# Formatted-SQL parsing
# --------------------------------------------------------------------------

def test_parse_formatted_changesets_properties_and_rollback():
    cs = sr.parse_formatted_changelog(FORMATTED)
    assert [(c.author, c.cid) for c in cs] == [("ewen", "1"), ("ewen", "2")]
    assert "public.orders" in cs[0].sql  # ${schema} substituted from --property
    assert cs[0].has_rollback and "DROP TABLE public.orders;" == cs[0].rollback_sql
    assert "--comment" not in cs[1].sql  # meta comment stripped
    assert "idx_orders_status" in cs[1].sql


def test_parse_formatted_empty_rollback_counts_as_present():
    cs = sr.parse_formatted_changelog(FORMATTED_EMPTY_ROLLBACK)
    assert cs[0].has_rollback and cs[0].rollback_sql == ""


def test_is_liquibase_formatted():
    assert sr.is_liquibase_formatted(FORMATTED)
    assert sr.is_liquibase_formatted("-- liquibase formatted sql\nSELECT 1;")
    assert not sr.is_liquibase_formatted("CREATE TABLE t (id int);")


# --------------------------------------------------------------------------
# New-changeset detection vs the diff base
# --------------------------------------------------------------------------

def test_new_changesets_without_base_is_everything(monkeypatch):
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: None)
    assert len(sr.new_formatted_changesets("db/changelog/c.sql", FORMATTED, _opts())) == 2


def test_new_changesets_appended_only(monkeypatch):
    base = FORMATTED.split("--changeset ewen:2")[0]  # base had only changeset 1
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: base)
    new = sr.new_formatted_changesets("db/changelog/c.sql", FORMATTED, _opts())
    assert [(c.author, c.cid) for c in new] == [("ewen", "2")]


def test_new_changesets_edited_body_counts(monkeypatch):
    edited = FORMATTED.replace("(status)", "(status, id)")
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: FORMATTED)
    new = sr.new_formatted_changesets("db/changelog/c.sql", edited, _opts())
    assert [(c.author, c.cid) for c in new] == [("ewen", "2")]


def test_extract_migration_formatted_changelog_only_new(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED)
    base = FORMATTED.split("--changeset ewen:2")[0]
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: base)
    sql, manual = sr.extract_migration(str(f), _opts())
    assert manual is None
    assert "idx_orders_status" in sql and "CREATE TABLE" not in sql


def test_extract_migration_formatted_no_new_changesets_skips(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED)
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: FORMATTED)
    assert sr.extract_migration(str(f), _opts()) is None


# --------------------------------------------------------------------------
# Formatted-SQL rollback audit
# --------------------------------------------------------------------------

def test_formatted_rollback_reversed_order(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED)
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: None)
    rb = sr.liquibase_formatted_rollback(str(f), _opts())
    assert rb["sql"].index("DROP INDEX") < rb["sql"].index("DROP TABLE")


def test_formatted_rollback_missing_when_any_changeset_lacks_it(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED + FORMATTED_NO_ROLLBACK.split("sql\n", 1)[1])
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: None)
    assert sr.liquibase_formatted_rollback(str(f), _opts()) == {"status": "missing"}


def test_formatted_rollback_all_empty_is_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED_EMPTY_ROLLBACK)
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: None)
    assert sr.liquibase_formatted_rollback(str(f), _opts()) is None


# --------------------------------------------------------------------------
# XML/YAML/JSON changelogs: detection + offline render
# --------------------------------------------------------------------------

XML_LEAF = '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">\n<changeSet id="1" author="e"><createTable tableName="t"/></changeSet>\n</databaseChangeLog>'
XML_MASTER = '<databaseChangeLog>\n  <includeAll path="db/changelog/releases/"/>\n</databaseChangeLog>'
YAML_LEAF = "databaseChangeLog:\n  - changeSet:\n      id: 1\n      author: e\n"


def test_structured_target_detection(tmp_path):
    (tmp_path / "leaf.xml").write_text(XML_LEAF)
    (tmp_path / "leaf.yaml").write_text(YAML_LEAF)
    (tmp_path / "pom.xml").write_text("<project><artifactId>x</artifactId></project>")
    (tmp_path / "notes.txt").write_text("databaseChangeLog:")
    assert sr.liquibase_structured_target(str(tmp_path / "leaf.xml"))
    assert sr.liquibase_structured_target(str(tmp_path / "leaf.yaml"))
    assert not sr.liquibase_structured_target(str(tmp_path / "pom.xml"))
    assert not sr.liquibase_structured_target(str(tmp_path / "notes.txt"))
    assert sr.is_migration_file(str(tmp_path / "leaf.xml"))


def test_extract_migration_renders_leaf_with_cli(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, "CREATE TABLE t (id int);\nINSERT INTO DATABASECHANGELOG (ID) VALUES ('1');\n")

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    sql, manual = sr.extract_migration(str(f), _opts())
    assert manual is None
    assert "CREATE TABLE t" in sql
    assert "DATABASECHANGELOG" not in sql  # bookkeeping stripped
    assert seen["cmd"][0] == "liquibase" and "update-sql" in seen["cmd"]
    assert f"--changelog-file={f}" in seen["cmd"]
    # stateless offline URL: a throwaway state CSV, never ./databasechangelog.csv
    url = next(a for a in seen["cmd"] if a.startswith("--url="))
    assert url.startswith("--url=offline:postgresql?changeLogFile=")


def test_extract_migration_master_index_is_flagged_not_rendered(tmp_path, monkeypatch):
    f = tmp_path / "db.changelog-master.xml"
    f.write_text(XML_MASTER)

    def no_subprocess(cmd, **kw):
        raise AssertionError(f"the index must not be rendered: {cmd}")

    monkeypatch.setattr(sr.subprocess, "run", no_subprocess)
    sql, manual = sr.extract_migration(str(f), _opts())
    assert sql == ""
    assert manual[0].check_name == "changelog-index"


def test_render_liquibase_cli_missing_raises_helpful_error(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)

    def boom(cmd, **kw):
        raise OSError("No such file or directory: 'liquibase'")

    monkeypatch.setattr(sr.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="liquibase CLI not available"):
        sr.extract_migration(str(f), _opts())


def test_render_liquibase_respects_engine_and_cmd(tmp_path, monkeypatch):
    f = tmp_path / "leaf.yaml"
    f.write_text(YAML_LEAF)
    seen = {}
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd) or _proc(0, "SELECT 1;"))
    sr.render_liquibase(str(f), _opts(engine="mysql", liquibase_cmd="/opt/liquibase/liquibase"))
    assert seen["cmd"][0] == "/opt/liquibase/liquibase"
    url = next(a for a in seen["cmd"] if a.startswith("--url="))
    assert url.startswith("--url=offline:mysql?changeLogFile=")


# --------------------------------------------------------------------------
# Structured rollback audit: changelog-sync into a temp state CSV, then
# rollback-count-sql with an overshoot count (the real CLI's working recipe;
# future-rollback-sql renders nothing offline).
# --------------------------------------------------------------------------

def _rollback_runner(seen, sync=_proc(0), rollback=_proc(0, "DROP TABLE t;")):
    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[-1] == "changelog-sync":
            return sync
        assert cmd[-2:] == ["rollback-count-sql", "999999"]
        return rollback
    return run


def test_structured_rollback_syncs_then_renders(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    seen: list = []
    monkeypatch.setattr(sr.subprocess, "run", _rollback_runner(seen))
    assert sr.extract_rollback(str(f), _opts()) == {"sql": "DROP TABLE t;"}
    assert [c[-1] for c in seen] == ["changelog-sync", "999999"]
    # both invocations share the same throwaway state CSV
    urls = {next(a for a in c if a.startswith("--url=")) for c in seen}
    assert len(urls) == 1 and "changeLogFile=" in urls.pop()


def test_structured_rollback_impossible_is_missing(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    err = "Unexpected error running Liquibase: liquibase.exception.RollbackImpossibleException: No inverse to liquibase.change.core.RawSQLChange created"
    monkeypatch.setattr(sr.subprocess, "run", _rollback_runner([], rollback=_proc(1, "", err)))
    assert sr.extract_rollback(str(f), _opts()) == {"status": "missing"}


def test_structured_rollback_sync_failure_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    monkeypatch.setattr(sr.subprocess, "run", _rollback_runner([], sync=_proc(1, "", "locked")))
    assert sr.extract_rollback(str(f), _opts()) is None


def test_structured_rollback_comment_only_output_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    banner = "-- ****************\n-- SQL to roll back\n-- ****************\n"
    monkeypatch.setattr(sr.subprocess, "run", _rollback_runner([], rollback=_proc(0, banner)))
    assert sr.extract_rollback(str(f), _opts()) is None


def test_structured_rollback_other_failure_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "leaf.xml"
    f.write_text(XML_LEAF)
    monkeypatch.setattr(sr.subprocess, "run", _rollback_runner([], rollback=_proc(1, "", "boom")))
    assert sr.extract_rollback(str(f), _opts()) is None


def test_structured_rollback_index_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "master.xml"
    f.write_text(XML_MASTER)

    def no_subprocess(cmd, **kw):
        raise AssertionError("the index must not be rendered")

    monkeypatch.setattr(sr.subprocess, "run", no_subprocess)
    assert sr.extract_rollback(str(f), _opts()) is None


def test_extract_rollback_routes_formatted_changelog(tmp_path, monkeypatch):
    f = tmp_path / "changelog.sql"
    f.write_text(FORMATTED)
    monkeypatch.setattr(sr, "_file_at_diff_base", lambda path, opts: None)

    def no_subprocess(cmd, **kw):
        raise AssertionError("formatted changelogs are parsed, not rendered")

    monkeypatch.setattr(sr.subprocess, "run", no_subprocess)
    rb = sr.extract_rollback(str(f), _opts())
    assert "DROP INDEX" in rb["sql"]


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------

def test_liquibase_cmd_flag_and_env(monkeypatch):
    assert sr.build_parser().parse_args([]).liquibase_cmd == "liquibase"
    monkeypatch.setenv("SIXTA_LIQUIBASE_CMD", "/usr/local/bin/liquibase")
    assert sr.build_parser().parse_args([]).liquibase_cmd == "/usr/local/bin/liquibase"
