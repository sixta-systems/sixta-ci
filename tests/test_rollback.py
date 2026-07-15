"""Tests for the rollback audit (client side of sixta-connect#75): framework
reverse renders, companion undo files, option plumbing, and output mappers.
All offline — stub HTTP servers and monkeypatched subprocess."""

import json
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sixta_review as sr


def _proc(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# Django: sqlmigrate --backwards
# --------------------------------------------------------------------------

def test_django_rollback_success_renders_sql(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, "DROP INDEX idx_status;\n")

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    assert sr.django_rollback("manage.py", "shop", "0002_x") == {"sql": "DROP INDEX idx_status;\n"}
    assert seen["cmd"][1:] == ["manage.py", "sqlmigrate", "shop", "0002_x", "--backwards"]


def test_django_rollback_irreversible(monkeypatch):
    err = ("Traceback (most recent call last):\n  ...\n"
           "django.db.migrations.exceptions.IrreversibleError: Operation <RunSQL  sql_1> is not reversible")
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: _proc(1, "", err))
    assert sr.django_rollback("manage.py", "shop", "0002_x") == {"status": "irreversible"}


def test_django_rollback_other_failure_is_unchecked(monkeypatch):
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: _proc(1, "", "OperationalError: connection refused"))
    assert sr.django_rollback("manage.py", "shop", "0002_x") is None


def test_django_rollback_oserror_is_unchecked(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(sr.subprocess, "run", boom)
    assert sr.django_rollback("manage.py", "shop", "0002_x") is None


# --------------------------------------------------------------------------
# Alembic: offline downgrade render + static downgrade() body check
# --------------------------------------------------------------------------

_HEAD = 'revision = "abc123"\ndown_revision = "def456"\n\ndef upgrade():\n    op.drop_column("users", "email")\n\n'
REAL_DOWN = _HEAD + 'def downgrade():\n    op.add_column("users", sa.Column("email", sa.String()))\n'
PASS_DOWN = _HEAD + 'def downgrade():\n    pass\n'
DOC_PASS_DOWN = _HEAD + 'def downgrade():\n    """nothing to do"""\n    pass\n'
RAISE_DOWN = _HEAD + 'def downgrade():\n    raise NotImplementedError("no downgrade")\n'
NO_DOWN = _HEAD


def test_alembic_rollback_renders_real_downgrade(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text(REAL_DOWN)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _proc(0, "ALTER TABLE users ADD COLUMN email varchar;\n")

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    assert sr.alembic_rollback(str(f), _opts()) == {"sql": "ALTER TABLE users ADD COLUMN email varchar;\n"}
    assert seen["cmd"] == ["alembic", "-c", "alembic.ini", "downgrade", "abc123:def456", "--sql"]


@pytest.mark.parametrize("body", [PASS_DOWN, DOC_PASS_DOWN, RAISE_DOWN, NO_DOWN],
                         ids=["pass", "docstring-pass", "raise-notimplemented", "absent"])
def test_alembic_rollback_trivial_downgrade_is_missing(tmp_path, monkeypatch, body):
    f = tmp_path / "m.py"
    f.write_text(body)

    def no_subprocess(cmd, **kw):  # a trivial body must not even try to render
        raise AssertionError(f"subprocess should not run for a trivial downgrade: {cmd}")

    monkeypatch.setattr(sr.subprocess, "run", no_subprocess)
    assert sr.alembic_rollback(str(f), _opts()) == {"status": "missing"}


def test_alembic_rollback_render_failure_with_real_body_is_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text(REAL_DOWN)
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: _proc(1, "", "boom"))
    assert sr.alembic_rollback(str(f), _opts()) is None


def test_alembic_rollback_empty_render_with_real_body_is_unchecked(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text(REAL_DOWN)
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: _proc(0, "   \n"))
    assert sr.alembic_rollback(str(f), _opts()) is None


def test_alembic_rollback_merge_migration_is_unchecked(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('revision = "m1"\ndown_revision = ("a", "b")\n\ndef upgrade():\n    pass\n')
    assert sr.alembic_rollback(str(f), _opts()) is None


# --------------------------------------------------------------------------
# Plain .sql / Flyway: companion undo file
# --------------------------------------------------------------------------

def test_sql_rollback_flyway_undo_found(tmp_path):
    (tmp_path / "V1.2__add_index.sql").write_text("CREATE INDEX i ON t (x);")
    (tmp_path / "U1.2__drop_index.sql").write_text("DROP INDEX i;")
    assert sr.sql_rollback(str(tmp_path / "V1.2__add_index.sql")) == {"sql": "DROP INDEX i;"}


def test_sql_rollback_flyway_undo_version_must_match(tmp_path):
    (tmp_path / "V2__add.sql").write_text("CREATE TABLE t (id int);")
    (tmp_path / "U1__other.sql").write_text("DROP TABLE old;")
    assert sr.sql_rollback(str(tmp_path / "V2__add.sql")) == {"status": "missing"}


def test_sql_rollback_bare_down_companion(tmp_path):
    (tmp_path / "foo.sql").write_text("ALTER TABLE t ADD c int;")
    (tmp_path / "foo.down.sql").write_text("ALTER TABLE t DROP COLUMN c;")
    assert sr.sql_rollback(str(tmp_path / "foo.sql")) == {"sql": "ALTER TABLE t DROP COLUMN c;"}


def test_sql_rollback_bare_rollback_companion(tmp_path):
    (tmp_path / "foo.sql").write_text("ALTER TABLE t ADD c int;")
    (tmp_path / "foo.rollback.sql").write_text("ALTER TABLE t DROP COLUMN c;")
    assert sr.sql_rollback(str(tmp_path / "foo.sql")) == {"sql": "ALTER TABLE t DROP COLUMN c;"}


def test_sql_rollback_bare_missing(tmp_path):
    (tmp_path / "foo.sql").write_text("ALTER TABLE t ADD c int;")
    assert sr.sql_rollback(str(tmp_path / "foo.sql")) == {"status": "missing"}


@pytest.mark.parametrize("name", ["U1__undo.sql", "foo.down.sql", "foo.rollback.sql"])
def test_sql_rollback_rollback_files_themselves_are_unchecked(tmp_path, name):
    (tmp_path / name).write_text("DROP TABLE t;")
    assert sr.sql_rollback(str(tmp_path / name)) is None


# --------------------------------------------------------------------------
# Option plumbing: --require-rollback / SIXTA_REQUIRE_ROLLBACK
# --------------------------------------------------------------------------

def test_require_rollback_defaults_false():
    assert sr.build_parser().parse_args([]).require_rollback is False


def test_require_rollback_flag():
    assert sr.build_parser().parse_args(["--require-rollback"]).require_rollback is True


@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("false", False), ("", False)])
def test_require_rollback_env(monkeypatch, value, expected):
    monkeypatch.setenv("SIXTA_REQUIRE_ROLLBACK", value)
    assert sr.build_parser().parse_args([]).require_rollback is expected


# --------------------------------------------------------------------------
# Stub /v1 server: request shapes and NO_ROLLBACK response rendering
# --------------------------------------------------------------------------

class RollbackStubHandler(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["content-length"])))
        RollbackStubHandler.calls.append(request)
        results = []
        for i, ex in enumerate(request.get("extractions", [])):
            findings = []
            if ex["kind"] == "migration":
                findings = [
                    {"rule_id": "NO_ROLLBACK", "title": "No rollback prepared for this changeset",
                     "severity": "High" if (request.get("options") or {}).get("require_rollback") else "Low"},
                    {"rule_id": "rollback:CREATE_INDEX", "title": "rollback: create index blocks writes",
                     "severity": "High"},
                ]
            results.append({"index": i, "kind": ex["kind"], "source_file": ex.get("source_file"),
                            "findings": findings, "report_text": "**SIXTA rollback audit**"})
        payload = json.dumps({"engine": request.get("engine"), "results": results,
                              "worst_severity": "High"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_rollback_v1():
    RollbackStubHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), RollbackStubHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


def test_run_v1_sql_migration_carries_missing_rollback_and_option(stub_rollback_v1, tmp_path):
    mig = tmp_path / "V7__add_index.sql"
    mig.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    sr.run_v1([str(mig)], _opts(require_rollback=True), client, hints={})

    req = RollbackStubHandler.calls[0]
    assert req["options"]["require_rollback"] is True
    migration = req["extractions"][0]
    assert migration["kind"] == "migration"
    assert migration["rollback"] == {"status": "missing"}


def test_run_v1_omits_require_rollback_when_false(stub_rollback_v1, tmp_path):
    mig = tmp_path / "changes.sql"
    mig.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    sr.run_v1([str(mig)], _opts(), client, hints={})
    assert "require_rollback" not in RollbackStubHandler.calls[0]["options"]


def test_run_v1_sql_migration_carries_undo_file_sql(stub_rollback_v1, tmp_path):
    (tmp_path / "V7__add_index.sql").write_text("CREATE INDEX i ON shop_order (status);")
    (tmp_path / "U7__drop_index.sql").write_text("DROP INDEX i;")
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    sr.run_v1([str(tmp_path / "V7__add_index.sql")], _opts(), client, hints={})
    assert RollbackStubHandler.calls[0]["extractions"][0]["rollback"] == {"sql": "DROP INDEX i;"}


def test_run_v1_django_migration_carries_backwards_sql(stub_rollback_v1, monkeypatch):
    monkeypatch.setattr(sr, "render_migration", lambda mp, app, name: "CREATE INDEX i ON shop_order (status);")
    monkeypatch.setattr(sr, "django_rollback", lambda mp, app, name: {"sql": "DROP INDEX i;"})
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    sr.run_v1(["shop/migrations/0002_x.py"], _opts(), client, hints={})
    assert RollbackStubHandler.calls[0]["extractions"][0]["rollback"] == {"sql": "DROP INDEX i;"}


def test_run_v1_unchecked_rollback_omits_the_field(stub_rollback_v1, monkeypatch):
    monkeypatch.setattr(sr, "render_migration", lambda mp, app, name: "CREATE INDEX i ON shop_order (status);")
    monkeypatch.setattr(sr, "django_rollback", lambda mp, app, name: None)
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    sr.run_v1(["shop/migrations/0002_x.py"], _opts(), client, hints={})
    assert "rollback" not in RollbackStubHandler.calls[0]["extractions"][0]


def test_no_rollback_findings_render_in_markdown_cq_and_sarif(stub_rollback_v1, tmp_path):
    mig = tmp_path / "V7__add_index.sql"
    mig.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_rollback_v1, api_key=None)
    reports, _renders, _context, _worst = sr.run_v1([str(mig)], _opts(require_rollback=True), client, hints={})

    checks = sorted(f.check_name for r in reports for f in r.findings)
    assert checks == ["NO_ROLLBACK", "rollback:CREATE_INDEX"]

    # Markdown renders the server's report text and the gate summary.
    md = sr.render_markdown(reports, "high")
    assert "SIXTA rollback audit" in md
    assert "worst severity **High**" in md

    # Code-quality entries build, serialize, and fingerprint stably.
    entries = [f.code_quality() for r in reports for f in r.findings]
    again = [f.code_quality() for r in reports for f in r.findings]
    assert [e["fingerprint"] for e in entries] == [e["fingerprint"] for e in again]
    assert len({e["fingerprint"] for e in entries}) == 2  # distinct per finding
    cq_path = tmp_path / "cq.json"
    sr.write_code_quality(reports, str(cq_path))
    assert len(json.loads(cq_path.read_text())) == 2

    # SARIF builds with the colon-carrying rule id and serializes.
    sarif = sr.build_sarif(reports)
    rule_ids = [r["id"] for r in sarif["runs"][0]["tool"]["driver"]["rules"]]
    assert "sixta:NO_ROLLBACK" in rule_ids and "sixta:rollback:CREATE_INDEX" in rule_ids
    sarif_path = tmp_path / "out.sarif"
    sr.write_sarif(sarif, str(sarif_path))
    assert json.loads(sarif_path.read_text())["runs"][0]["results"]


# --------------------------------------------------------------------------
# MCP mode: rollback audit is skipped entirely
# --------------------------------------------------------------------------

class _FakeMcpClient:
    def __init__(self):
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {"content": [{"type": "text", "text": "clean"}],
                "structuredContent": {"verdict": "clean", "findings": []}}


def test_mcp_mode_sends_no_rollback(tmp_path, monkeypatch):
    (tmp_path / "V7__add_index.sql").write_text("CREATE INDEX i ON shop_order (status);")
    (tmp_path / "U7__drop_index.sql").write_text("DROP INDEX i;")

    def no_subprocess(cmd, **kw):  # MCP mode must not probe rollbacks at all
        raise AssertionError(f"subprocess should not run in mcp mode: {cmd}")

    monkeypatch.setattr(sr.subprocess, "run", no_subprocess)
    client = _FakeMcpClient()
    opts = sr.build_parser().parse_args(["--api", "mcp", "--require-rollback"])
    reports = sr.analyze_files([str(tmp_path / "V7__add_index.sql")], opts, client, hints={})

    assert client.calls, "the DDL should still be analyzed"
    for _tool, args in client.calls:
        assert "rollback" not in json.dumps(args).lower()
    assert reports[0].findings == []


def test_main_mcp_warns_that_require_rollback_is_v1_only(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = sr.main(["--api", "mcp", "--require-rollback", "--platform", "gitlab",
                  "--code-quality", str(tmp_path / "cq.json"), "not-a-migration.txt"])
    assert rc == 0
    assert "rollback audit is /v1 only" in capsys.readouterr().err
