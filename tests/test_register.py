"""Tests for `sixta_review.py register` against a local stand-in for the
backend's register route (POST /api/v1/changes). No network, no real backend."""

import io
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler

import pytest

import sixta_review as sr
from conftest import REGISTER_FIELDS, REGISTER_TOKEN, StubRegisterHandler, run_register_stub, run_stub_server

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sixta_review.py")


@pytest.fixture
def backend(monkeypatch):
    """The stand-in, configured the way a pipeline would configure the kit."""
    for url in run_register_stub():
        monkeypatch.setenv("SIXTA_CHANGE_URL", url)
        monkeypatch.setenv("SIXTA_CHANGE_TOKEN", REGISTER_TOKEN)
        yield url


def _run(capsys, *args):
    """Run the command in-process; returns (exit code, stdout, stderr)."""
    code = sr.main(["register", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def _one_line(err):
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    return lines[0]


BASE = ("--database", "orders-main", "--tool", "flyway")


# --------------------------------------------------------------------------
# The request: the route's field names, required fields and bearer
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://sixta.example.com", "https://sixta.example.com/api/v1/changes"),
        ("https://sixta.example.com/", "https://sixta.example.com/api/v1/changes"),
        ("https://sixta.example.com/api/v1/changes", "https://sixta.example.com/api/v1/changes"),
        ("http://127.0.0.1:8100", "http://127.0.0.1:8100/api/v1/changes"),
    ],
)
def test_register_endpoint(url, expected):
    assert sr.register_endpoint(url) == expected


def test_register_posts_the_routes_fields_with_the_bearer(backend, capsys):
    code, _, err = _run(
        capsys, *BASE, "--statement", "CREATE INDEX idx_orders_email ON orders (email)",
        "--revision", "7", "--actor", "dba@example.com", "--pull-request", "acme/app#12", "--change-id", "3",
        "--started-at", "2026-11-17T10:00:00Z", "--finished-at", "2026-11-17T10:00:42Z",
    )
    assert code == 0
    assert len(StubRegisterHandler.calls) == 1
    call = StubRegisterHandler.calls[0]
    assert call["path"] == "/api/v1/changes"
    assert call["auth"] == "Bearer " + REGISTER_TOKEN
    body = call["body"]
    assert set(body) <= REGISTER_FIELDS  # the route refuses a field it does not have
    assert body == {
        "idempotency_key": body["idempotency_key"],
        "database": "orders-main",
        "plane": "schema",
        "statements": ["CREATE INDEX idx_orders_email ON orders (email)"],
        "tool": "flyway",
        "revision": "7",
        "actor": "dba@example.com",
        "pull_request": "acme/app#12",
        "change_ids": [3],
        "started_at": "2026-11-17T10:00:00Z",
        "finished_at": "2026-11-17T10:00:42Z",
    }
    assert body["idempotency_key"].startswith("sixta-ci-")
    line = _one_line(err)
    assert "SIXTA recorded the change on orders-main" in line and "WARNING" not in line


def test_register_minimal_request_carries_the_required_fields(backend, capsys):
    # The route requires an idempotency key, a database, a plane, and either
    # statements or a subject; the stand-in refuses a request missing any.
    code, _, err = _run(capsys, "--database", "orders-main", "--subject", "V7__add_orders_index.sql")
    assert code == 0
    body = StubRegisterHandler.calls[0]["body"]
    assert set(body) == {"idempotency_key", "database", "plane", "subject"}
    assert "by its identity, no statement text" in _one_line(err)


def test_register_database_from_the_environment(backend, capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_DATABASE", "billing")
    _run(capsys, "--statement", "ALTER TABLE invoices ADD COLUMN paid_at timestamp")
    assert StubRegisterHandler.calls[0]["body"]["database"] == "billing"


def test_register_replay_is_recorded_once(backend, capsys):
    args = (*BASE, "--statement", "CREATE INDEX i ON orders (status)", "--at", "2026-11-17T10:00:00Z")
    _run(capsys, *args)
    code, _, err = _run(capsys, *args)
    assert code == 0
    first, second = (c["body"]["idempotency_key"] for c in StubRegisterHandler.calls)
    assert first == second  # the derived key is the change's content and stated time
    assert "already recorded under the same key" in _one_line(err)
    # The same SQL at another stated time is its own change.
    _run(capsys, *BASE, "--statement", "CREATE INDEX i ON orders (status)", "--at", "2026-11-17T11:00:00Z")
    assert StubRegisterHandler.calls[2]["body"]["idempotency_key"] != first


def test_register_an_explicit_key_is_sent_as_given(backend, capsys):
    _run(capsys, *BASE, "--subject", "x", "--idempotency-key", "deploy-77")
    assert StubRegisterHandler.calls[0]["body"]["idempotency_key"] == "deploy-77"


# --------------------------------------------------------------------------
# Statements: files, stdin, what is left out, and the route's bounds
# --------------------------------------------------------------------------

def test_register_reads_files_and_stdin(backend, capsys, tmp_path, monkeypatch):
    f = tmp_path / "V2__add_index.sql"
    f.write_text("-- add the index\nCREATE INDEX i ON orders (status);\nUPDATE orders SET status = 'new' WHERE status IS NULL;\n")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"ALTER TABLE orders ADD COLUMN note text;")})())
    _run(capsys, *BASE, "--file", str(f), "--file", "-")
    assert StubRegisterHandler.calls[0]["body"]["statements"] == [
        "-- add the index\nCREATE INDEX i ON orders (status)",
        "UPDATE orders SET status = 'new' WHERE status IS NULL",
        "ALTER TABLE orders ADD COLUMN note text",
    ]


def test_register_leaves_out_transaction_control_and_tool_bookkeeping(backend, capsys):
    sql = """BEGIN;
    CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
    CREATE TABLE orders (id int);
    INSERT INTO DATABASECHANGELOG (ID) VALUES ('1');
    UPDATE flyway_schema_history SET success = 1;
    INSERT INTO django_migrations (app, name) VALUES ('shop', '0001');
    -- only a comment
    COMMIT;"""
    _, _, err = _run(capsys, *BASE, "--statement", sql)
    assert StubRegisterHandler.calls[0]["body"]["statements"] == ["CREATE TABLE orders (id int)"]
    assert "6 transaction-control or tool-bookkeeping statement(s) not sent" in _one_line(err)


@pytest.mark.parametrize("stmt,sent", [
    ("BEGIN", False), ("begin transaction isolation level serializable", False), ("COMMIT", False),
    ("ROLLBACK TO SAVEPOINT s1", False), ("START TRANSACTION READ ONLY", False), ("RELEASE SAVEPOINT s1", False),
    ("/* wrap */ END", False),
    ("START REPLICA", True),                       # starts with a transaction word, is not one
    ("SET GLOBAL max_connections = 500", True),    # a setting is a change
    ("LOCK TABLES orders WRITE", True),
])
def test_register_only_transaction_control_is_left_out(stmt, sent):
    statements, not_a_change, _ = sr._register_statements([stmt])
    assert (statements == [stmt]) is sent and not_a_change == (0 if sent else 1)


def test_register_splits_a_long_migration_across_requests(backend, capsys):
    sql = ";\n".join(f"CREATE TABLE t{i} (id int)" for i in range(45))
    code, _, err = _run(capsys, *BASE, "--statement", sql, "--idempotency-key", "mig-9")
    assert code == 0
    bodies = [c["body"] for c in StubRegisterHandler.calls]
    assert [len(b["statements"]) for b in bodies] == [20, 20, 5]  # the route admits 20 a request
    assert [b["idempotency_key"] for b in bodies] == ["mig-9/1", "mig-9/2", "mig-9/3"]
    assert "45 statement(s) in 3 request(s)" in _one_line(err)


def test_register_a_very_long_migration_is_registered_by_identity(backend, capsys):
    many = sr.REGISTER_MAX_STATEMENTS * sr.REGISTER_MAX_REQUESTS + 1
    sql = ";\n".join(f"INSERT INTO t (id) VALUES ({i})" for i in range(many))
    code, _, err = _run(capsys, *BASE, "--statement", sql, "--revision", "12")
    assert code == 0
    assert len(StubRegisterHandler.calls) == 1
    body = StubRegisterHandler.calls[0]["body"]
    assert "statements" not in body and body["subject"] == "12"
    assert f"{many} statements left out as too many to send" in _one_line(err)


def test_register_a_statement_over_the_bound_is_not_sent(backend, capsys):
    big = "INSERT INTO t (v) VALUES ('" + "x" * (sr.REGISTER_MAX_STATEMENT_BYTES + 1) + "')"
    _, _, err = _run(capsys, *BASE, "--statement", big, "--statement", "CREATE TABLE t (v text)")
    assert StubRegisterHandler.calls[0]["body"]["statements"] == ["CREATE TABLE t (v text)"]
    assert "1 statement(s) over 64 KiB not sent" in _one_line(err)


def test_register_bodies_stay_under_the_body_bound():
    stmts = ["INSERT INTO t (v) VALUES ('" + "y" * 60_000 + "')" for _ in range(12)]
    bodies, withheld = sr.register_bodies({"database": "d", "plane": "schema"}, stmts)
    assert not withheld and len(bodies) > 1
    assert all(len(json.dumps(b).encode()) <= sr.REGISTER_MAX_BODY_BYTES for b in bodies)
    assert sum(len(b["statements"]) for b in bodies) == 12


def test_register_statements_belong_on_the_schema_or_statement_plane(backend, capsys):
    code, _, err = _run(capsys, *BASE, "--plane", "instance", "--statement", "CREATE TABLE t (id int)")
    assert code == 0 and StubRegisterHandler.calls == []
    assert "schema or statement plane" in _one_line(err)


def test_register_nothing_to_say_sends_nothing(backend, capsys):
    code, _, err = _run(capsys, *BASE, "--statement", "BEGIN; COMMIT;")
    assert code == 0 and StubRegisterHandler.calls == []
    line = _one_line(err)
    assert "nothing to register" in line and "WARNING" not in line  # a run that applied nothing is not a failure


# --------------------------------------------------------------------------
# Times and names
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "given,expected",
    [
        ("2026-11-17T10:00:00Z", "2026-11-17T10:00:00Z"),
        ("2026-11-17T11:00:00.123456789+01:00", "2026-11-17T10:00:00Z"),  # Java's Instant, another zone
        ("2026-11-17 05:00:00-0500", "2026-11-17T10:00:00Z"),
        ("1794909600", "2026-11-17T10:00:00Z"),                           # date +%s
    ],
)
def test_register_instant_reads_rfc3339_and_epoch(given, expected):
    assert sr._register_instant(given, "--at") == expected


@pytest.mark.parametrize("bad", ["yesterday", "2026-11-17T10:00:00", "2026-13-40T10:00:00Z", "12:00"])
def test_register_a_time_that_is_not_one_sends_nothing(backend, capsys, bad):
    code, _, err = _run(capsys, *BASE, "--subject", "x", "--at", bad)
    assert code == 0 and StubRegisterHandler.calls == []
    assert "not an RFC 3339 time" in _one_line(err)


def test_register_a_finish_with_no_start_is_an_instant(backend, capsys):
    # The route refuses finished_at without started_at; an end alone is `at`.
    _run(capsys, *BASE, "--subject", "x", "--finished-at", "2026-11-17T10:00:42Z")
    body = StubRegisterHandler.calls[0]["body"]
    assert body["at"] == "2026-11-17T10:00:42Z" and "finished_at" not in body


def test_register_at_and_a_span_together_send_nothing(backend, capsys):
    code, _, err = _run(capsys, *BASE, "--subject", "x", "--at", "2026-11-17T10:00:00Z", "--started-at", "2026-11-17T09:00:00Z")
    assert code == 0 and StubRegisterHandler.calls == []
    assert "not both" in _one_line(err)


def test_register_no_stated_time_sends_none(backend, capsys):
    # With no time stated the backend dates the change at the post's arrival.
    _run(capsys, *BASE, "--subject", "x")
    assert not {"at", "started_at", "finished_at"} & set(StubRegisterHandler.calls[0]["body"])


def test_register_an_empty_time_is_no_stated_time(backend, capsys):
    # A shell variable that expanded to nothing costs the time, not the registration.
    code, _, err = _run(capsys, *BASE, "--subject", "x", "--started-at", "", "--finished-at", "")
    assert code == 0 and "WARNING" not in err
    assert not {"at", "started_at", "finished_at"} & set(StubRegisterHandler.calls[0]["body"])


@pytest.mark.parametrize("value,ok", [
    ("V7__add index.sql", True),
    ("bob's laptop", False),
    ('say "hi"', False),
    ("two\nlines", False),
    ("tab\there", False),
    ("x" * 201, False),
    ("x" * 200, True),
    ("  ", False),
])
def test_register_ident_is_the_routes_rule(value, ok):
    assert (sr._register_ident(value) is not None) is ok


def test_register_a_name_the_route_would_refuse_is_left_out(backend, capsys):
    # One stray quote in an optional field must not cost the registration.
    code, _, err = _run(capsys, *BASE, "--subject", "x", "--actor", "bob's laptop")
    assert code == 0
    assert "actor" not in StubRegisterHandler.calls[0]["body"]
    assert "actor left out as not a plain name" in _one_line(err)


def test_register_change_ids_the_route_would_refuse_are_left_out(backend, capsys):
    _run(capsys, *BASE, "--subject", "x", "--change-id", "0")
    assert "change_ids" not in StubRegisterHandler.calls[0]["body"]
    capsys.readouterr()
    _run(capsys, *BASE, "--subject", "y", "--change-id", "4", "--change-id", "4", "--change-id", "9")
    assert StubRegisterHandler.calls[1]["body"]["change_ids"] == [4, 9]


# --------------------------------------------------------------------------
# It never fails the caller: one line, exit zero, no secret
# --------------------------------------------------------------------------

def test_register_unconfigured_is_silent(capsys):
    code, out, err = _run(capsys, *BASE, "--subject", "x")
    assert (code, out, err) == (0, "", "")
    # Even a bad flag is not worth a line where nothing is configured.
    assert _run(capsys, "--no-such-flag") == (0, "", "")


def test_register_half_configured_says_so(capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", REGISTER_TOKEN)  # a token with no URL
    code, _, err = _run(capsys, *BASE, "--subject", "x")
    assert code == 0
    assert "must both be set" in _one_line(err)
    assert REGISTER_TOKEN not in err


def test_register_unreachable_backend_logs_one_line_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_URL", "http://127.0.0.1:9")  # nothing listens on discard
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", REGISTER_TOKEN)
    code, out, err = _run(capsys, *BASE, "--statement", "CREATE TABLE t (id int)")
    assert code == 0 and out == ""
    line = _one_line(err)
    assert "not registered" in line and "unreachable" in line and "http://127.0.0.1:9/api/v1/changes" in line
    assert REGISTER_TOKEN not in err


@pytest.mark.parametrize(
    "args,behavior,token,expect",
    [
        (("--database", "orders-main", "--subject", "x"), "ok", "a-wrong-token-0123456789", "HTTP 401"),
        (("--database", "nowhere", "--subject", "x"), "ok", REGISTER_TOKEN, "HTTP 400"),
        (("--database", "hidden-db", "--subject", "x"), "ok", REGISTER_TOKEN, "HTTP 409"),
        (("--database", "orders-main", "--subject", "x"), "disabled", REGISTER_TOKEN, "HTTP 404"),
        (("--database", "orders-main", "--subject", "x"), "boom", REGISTER_TOKEN, "HTTP 500"),
    ],
)
def test_register_a_refusal_logs_one_line_and_exits_zero(backend, capsys, monkeypatch, args, behavior, token, expect):
    StubRegisterHandler.behavior = behavior
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", token)
    code, out, err = _run(capsys, *args)
    assert code == 0 and out == ""
    line = _one_line(err)
    assert expect in line and "not registered" in line
    assert token not in err


def test_register_a_400_names_the_field_the_route_refused(backend, capsys):
    _, _, err = _run(capsys, "--database", "nowhere", "--subject", "x")
    assert "invalid_registration: database: not a database the estate holds" in _one_line(err)


def test_register_a_401_points_at_the_token_setting_without_showing_it(backend, capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", "a-wrong-token-0123456789")
    _, _, err = _run(capsys, *BASE, "--subject", "x")
    assert "check SIXTA_CHANGE_TOKEN" in err and "a-wrong-token-0123456789" not in err


def test_register_a_timeout_logs_one_line_and_exits_zero(backend, capsys):
    StubRegisterHandler.behavior = "slow"
    code, out, err = _run(capsys, *BASE, "--subject", "x", "--timeout", "0.2")
    assert code == 0 and out == ""
    line = _one_line(err)
    assert "no answer within 0.2s" in line and "not registered" in line


def test_register_an_echoed_token_never_reaches_the_log(backend, capsys):
    # A proxy that quotes the request back must not put the bearer in a CI log.
    StubRegisterHandler.behavior = "echo"
    code, out, err = _run(capsys, *BASE, "--subject", "x")
    assert code == 0
    assert "HTTP 502" in _one_line(err)
    assert REGISTER_TOKEN not in err and REGISTER_TOKEN not in out


def test_register_credentials_in_the_url_never_reach_the_log(capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_URL", "http://deploy:url-password-123@127.0.0.1:9/?key=query-secret-456")
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", REGISTER_TOKEN)
    code, out, err = _run(capsys, *BASE, "--subject", "x")
    assert code == 0
    for secret in ("url-password-123", "query-secret-456", REGISTER_TOKEN):
        assert secret not in err and secret not in out


class _Elsewhere(BaseHTTPRequestHandler):
    """Where a redirect points: it must never hear from the kit."""
    calls: list = []
    behavior: str = "ok"

    def do_POST(self):
        _Elsewhere.calls.append(self.headers.get("authorization"))
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST

    def log_message(self, *args):
        pass


def test_register_does_not_follow_a_redirect_with_the_token(backend, capsys):
    for other in run_stub_server(_Elsewhere):
        StubRegisterHandler.behavior = "redirect"
        StubRegisterHandler.redirect_to = other.rsplit("/mcp", 1)[0] + "/api/v1/changes"
        code, _, err = _run(capsys, *BASE, "--subject", "x")
        assert code == 0
        assert _Elsewhere.calls == []  # urllib would have replayed the bearer there
        line = _one_line(err)
        assert "HTTP 307" in line and "redirects are not followed" in line


def test_register_an_answer_that_is_not_the_routes_is_not_called_recorded(backend, capsys):
    StubRegisterHandler.behavior = "html"  # a sign-in page answering 200
    code, _, err = _run(capsys, *BASE, "--subject", "x")
    assert code == 0
    assert "not as the register route does" in _one_line(err)


def test_register_plain_http_beyond_loopback_is_said_on_the_same_line(capsys, monkeypatch):
    monkeypatch.setenv("SIXTA_CHANGE_URL", "http://sixta.invalid:8100")
    monkeypatch.setenv("SIXTA_CHANGE_TOKEN", REGISTER_TOKEN)
    code, _, err = _run(capsys, *BASE, "--subject", "x", "--timeout", "2")
    assert code == 0
    assert "travel unencrypted" in _one_line(err)


@pytest.mark.parametrize("argv", [
    ("--no-such-flag",),
    ("--database",),                       # a flag whose value expanded to nothing
    ("--subject", "x"),                    # no database
    ("--database", "orders-main", "--plane", "weather", "--subject", "x"),
    ("--database", "orders-main", "--change-id", "seven", "--subject", "x"),
    ("--database", "orders-main", "--file", "/no/such/file.sql"),
    ("--database", "orders-main", "--subject", "x", "--idempotency-key", "it's"),
])
def test_register_a_mistake_in_the_call_logs_one_line_and_exits_zero(backend, capsys, argv):
    code, out, err = _run(capsys, *argv)
    assert code == 0 and out == ""
    assert "not registered" in _one_line(err)
    assert StubRegisterHandler.calls == []


def test_register_help_exits_zero(capsys):
    assert sr.main(["register", "--help"]) == 0
    assert "usage: sixta-review register" in capsys.readouterr().out


def test_register_as_a_process_exits_zero_and_keeps_the_token_out(tmp_path):
    # The real exit code a pipeline step sees, with the backend unreachable.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIXTA_")}
    env.update(SIXTA_CHANGE_URL="http://127.0.0.1:9", SIXTA_CHANGE_TOKEN=REGISTER_TOKEN)
    proc = subprocess.run(
        [sys.executable, SCRIPT, "register", "--database", "orders-main", "--statement", "CREATE TABLE t (id int)"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "not registered" in _one_line(proc.stderr)
    assert REGISTER_TOKEN not in proc.stderr


# --------------------------------------------------------------------------
# --dry-run: exactly what would leave the pipeline
# --------------------------------------------------------------------------

def test_register_dry_run_prints_the_bodies_and_sends_nothing(backend, capsys):
    code, out, err = _run(capsys, *BASE, "--statement", "CREATE INDEX i ON orders (email)", "--dry-run")
    assert code == 0 and StubRegisterHandler.calls == []
    printed = json.loads(out)
    assert printed[0]["statements"] == ["CREATE INDEX i ON orders (email)"]
    assert set(printed[0]) <= REGISTER_FIELDS
    assert REGISTER_TOKEN not in out and REGISTER_TOKEN not in err
    assert "dry run, nothing sent" in _one_line(err)


def test_register_dry_run_needs_no_configuration(capsys):
    code, out, _ = _run(capsys, "--database", "orders-main", "--subject", "x", "--dry-run")
    assert code == 0 and json.loads(out)[0]["subject"] == "x"


# --------------------------------------------------------------------------
# --flyway-json: one change per migration a Flyway run applied
# --------------------------------------------------------------------------

def _flyway_result(tmp_path, migrations):
    path = tmp_path / "flyway-migrate.json"
    path.write_text(json.dumps({
        "initialSchemaVersion": "1", "targetSchemaVersion": "3", "schemaName": "public",
        "migrations": migrations, "migrationsExecuted": len(migrations), "flywayVersion": "11.8.2",
        "database": "orders", "warnings": [], "operation": "migrate",
    }))
    return str(path)


def test_register_flyway_json_registers_each_migration(backend, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    v2 = tmp_path / "V2__add_status_index.sql"
    v2.write_text("CREATE INDEX idx_orders_status ON ${schema}.orders (status);\n")
    v3 = tmp_path / "V3__Backfill_totals.java"  # a Java migration: no SQL file to read
    result = _flyway_result(tmp_path, [
        {"category": "Versioned", "version": "2", "description": "add status index", "type": "SQL",
         "filepath": str(v2), "executionTime": 14},
        {"category": "Versioned", "version": "3", "description": "Backfill totals", "type": "JDBC",
         "filepath": str(v3), "executionTime": 3},
    ])
    code, _, err = _run(capsys, *BASE, "--flyway-json", result,
                        "--started-at", "2026-11-17T10:00:00Z", "--finished-at", "2026-11-17T10:00:05Z")
    assert code == 0
    first, second = (c["body"] for c in StubRegisterHandler.calls)
    assert first["subject"] == "V2__add_status_index.sql" and first["revision"] == "2"
    assert first["statements"] == ["CREATE INDEX idx_orders_status ON schema.orders (status)"]
    assert second["subject"] == "V3__Backfill_totals.java" and second["revision"] == "3"
    assert "statements" not in second
    assert first["idempotency_key"] != second["idempotency_key"]
    for body in (first, second):
        assert (body["started_at"], body["finished_at"]) == ("2026-11-17T10:00:00Z", "2026-11-17T10:00:05Z")
    assert "SIXTA recorded 2 change(s) on orders-main" in _one_line(err)


def test_register_flyway_json_with_nothing_applied_sends_nothing(backend, capsys, tmp_path):
    code, _, err = _run(capsys, *BASE, "--flyway-json", _flyway_result(tmp_path, []))
    assert code == 0 and StubRegisterHandler.calls == []
    assert "applied no migration" in _one_line(err)


def test_register_flyway_json_that_is_not_a_result_sends_nothing(backend, capsys, tmp_path):
    bad = tmp_path / "out.json"
    bad.write_text("ERROR: Unable to connect to the database")
    code, _, err = _run(capsys, *BASE, "--flyway-json", str(bad))
    assert code == 0 and StubRegisterHandler.calls == []
    assert "not registered" in _one_line(err)


def test_register_stops_at_the_first_failed_request(backend, capsys, tmp_path, monkeypatch):
    # An unreachable backend costs one wait, not one per migration.
    monkeypatch.chdir(tmp_path)
    StubRegisterHandler.behavior = "boom"
    result = _flyway_result(tmp_path, [
        {"version": str(i), "description": f"m{i}", "type": "SQL", "filepath": f"/gone/V{i}__m.sql"} for i in range(1, 6)
    ])
    code, _, err = _run(capsys, *BASE, "--flyway-json", result)
    assert code == 0
    assert len(StubRegisterHandler.calls) == 1
    assert "HTTP 500" in _one_line(err)
