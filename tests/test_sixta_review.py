"""Unit + integration tests for sixta_review (stub MCP server, no network)."""

import http.client
import io
import json
import urllib.error
from http.server import BaseHTTPRequestHandler

import pytest

import sixta_review as sr
from conftest import json_reply, run_stub_server


# --------------------------------------------------------------------------
# SQL splitting / classification (pure)
# --------------------------------------------------------------------------

def test_split_basic():
    assert sr.split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_split_trailing_without_semicolon():
    assert sr.split_statements("SELECT 1") == ["SELECT 1"]


def test_split_semicolon_in_string_literal():
    stmts = sr.split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
    assert stmts == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_split_escaped_quote():
    stmts = sr.split_statements("INSERT INTO t VALUES ('it''s;fine'); SELECT 1;")
    assert len(stmts) == 2


def test_split_dollar_quoted_function_body():
    sql = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 1;"
    stmts = sr.split_statements(sql)
    assert len(stmts) == 2
    assert "END;" in stmts[0]


def test_split_line_and_block_comments():
    sql = "-- comment; with semicolon\nSELECT 1; /* block; comment */ SELECT 2;"
    stmts = sr.split_statements(sql)
    assert stmts[0].endswith("SELECT 1")
    assert len(stmts) == 2


def test_split_mysql_backticks():
    stmts = sr.split_statements("ALTER TABLE `a;b` ADD COLUMN c int; SELECT 1;")
    assert len(stmts) == 2


@pytest.mark.parametrize(
    "stmt,expected",
    [
        ("CREATE INDEX i ON t (c)", "ddl"),
        ("ALTER TABLE t ADD COLUMN c int", "ddl"),
        ("DROP TABLE t", "ddl"),
        ("UPDATE t SET c = 1", "dml"),
        ("SELECT * FROM t", "dml"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "dml"),
        ("BEGIN", "skip"),
        ("COMMIT", "skip"),
        ("SET search_path TO public", "skip"),
        ("-- comment\nCREATE TABLE t (id int)", "ddl"),
        ("GRANT ALL ON t TO PUBLIC", "other"),
    ],
)
def test_classify(stmt, expected):
    assert sr.classify_statement(stmt) == expected


@pytest.mark.parametrize(
    "stmt,table",
    [
        ("ALTER TABLE shop_order ADD COLUMN x int", "shop_order"),
        ('ALTER TABLE "public"."shop_order" ADD COLUMN x int', "shop_order"),
        ("CREATE INDEX idx ON shop_order (status)", "shop_order"),
        ("CREATE UNIQUE INDEX CONCURRENTLY idx ON public.shop_order (status)", "shop_order"),
        ("DROP TABLE IF EXISTS old_stuff", "old_stuff"),
        ("CREATE TABLE new_t (id int)", "new_t"),
        ("SELECT 1", None),
    ],
)
def test_extract_table(stmt, table):
    assert sr.extract_table(stmt) == table


# --------------------------------------------------------------------------
# Migration path parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path,expected",
    [
        ("shop/migrations/0002_add_index.py", ("shop", "0002_add_index")),
        ("src/apps/billing/migrations/0042_x.py", ("billing", "0042_x")),
        ("shop/migrations/__init__.py", None),
        ("shop/models.py", None),
        ("changes.sql", None),
    ],
)
def test_migration_target(path, expected):
    assert sr.migration_target(path) == expected


# --------------------------------------------------------------------------
# Verdict extraction
# --------------------------------------------------------------------------

SCHEMA_STRUCT = {
    "overall_risk": "HIGH",
    "overall_severity": "High",
    "statements": [
        {
            "operation": "CREATE_INDEX",
            "table": "shop_order",
            "risk": "HIGH",
            "severity": "High",
            "lock_type": "ShareLock",
            "blocks_reads": False,
            "blocks_writes": True,
            "has_safe_alternative": True,
        }
    ],
    "severity_histogram": {"High": 1},
}

QUERY_STRUCT = {
    "verdict": "findings",
    "overall_severity": "Critical",
    "findings": [
        {"rule_id": "NULL-EQUALS", "title": "Equality comparison with NULL", "severity": "Critical", "confidence": "SMELL"}
    ],
    "severity_histogram": {"Critical": 1},
}


def test_findings_from_structured_schema():
    result = {"content": [{"type": "text", "text": "..."}], "structuredContent": SCHEMA_STRUCT}
    findings = sr.findings_from_result(result, "shop/migrations/0002_x.py")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "High"
    assert "shop_order" in f.description and "ShareLock" in f.description and "blocks writes" in f.description


def test_findings_from_structured_query():
    result = {"content": [{"type": "text", "text": "..."}], "structuredContent": QUERY_STRUCT}
    findings = sr.findings_from_result(result, "x.sql")
    assert len(findings) == 1
    assert findings[0].severity == "Critical"
    assert findings[0].check_name == "NULL-EQUALS"


def test_findings_from_structured_clean():
    result = {"content": [{"type": "text", "text": "clean"}], "structuredContent": {"verdict": "clean", "findings": []}}
    assert sr.findings_from_result(result, "x.sql") == []


def test_findings_text_fallback():
    text = (
        "**SIXTA query analysis (PostgreSQL)** — overall severity: Medium\n\n"
        "1. **NOT IN (subquery)** — Medium\n   detail\n\n"
        "2. **SELECT *** — Info\n   detail\n"
    )
    findings = sr.findings_from_result({"content": [{"type": "text", "text": text}]}, "x.sql")
    sevs = sorted(f.severity for f in findings)
    assert sevs == ["Info", "Medium"]


def test_code_quality_entry_shape_and_fingerprint_stability():
    f = sr.Finding(path="a/migrations/0001_x.py", severity="High", description="SIXTA: CREATE_INDEX on t")
    e1, e2 = f.code_quality(), f.code_quality()
    assert e1 == e2  # stable fingerprint
    assert e1["severity"] == "critical"  # High -> critical in GitLab's scale
    assert e1["location"] == {"path": "a/migrations/0001_x.py", "lines": {"begin": 1}}
    assert sr.Finding(path="b.py", severity="High", description="SIXTA: CREATE_INDEX on t").code_quality()[
        "fingerprint"
    ] != e1["fingerprint"]


@pytest.mark.parametrize(
    "sev,cq",
    [("Critical", "blocker"), ("High", "critical"), ("Medium", "major"), ("Low", "minor"), ("Info", "info")],
)
def test_cq_severity_map(sev, cq):
    assert sr.CQ_SEVERITY[sev] == cq


# --------------------------------------------------------------------------
# DDL hint grouping
# --------------------------------------------------------------------------

def test_ddl_groups_no_hints_single_batch():
    groups = sr._ddl_groups(["CREATE INDEX i ON a (x)", "ALTER TABLE b ADD c int"], {})
    assert len(groups) == 1
    assert groups[0][1] == {}


def test_ddl_groups_hinted_table_gets_own_call():
    hints = {"a": {"size_bytes": 5}}
    groups = sr._ddl_groups(["CREATE INDEX i ON a (x)", "ALTER TABLE b ADD c int"], hints)
    assert len(groups) == 2
    hinted = [g for g in groups if g[1]]
    assert len(hinted) == 1 and hinted[0][1]["size_bytes"] == 5
    assert "ON a" in hinted[0][0]


# --------------------------------------------------------------------------
# Stub MCP server integration
# --------------------------------------------------------------------------

class StubHandler(BaseHTTPRequestHandler):
    calls: list = []
    # ok | rate_limit_once | tool_error | tool_error_auth | http_401 | http_401_string_error
    # | http_503 | http_429_once | rpc_auth_error | rpc_null_message | rpc_rate_limit_once
    behavior: str = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        StubHandler.calls.append({"body": body, "auth": self.headers.get("authorization")})
        tool = body["params"]["name"]
        if StubHandler.behavior == "http_401":
            return self._json(401, {"error": {"code": "unauthorized", "message": "invalid API key"}})
        if StubHandler.behavior == "http_401_string_error":
            return self._json(401, {"error": "unauthorized"})  # REST shape some proxies use
        if StubHandler.behavior == "http_503":
            return self._json(503, {"error": {"code": "unavailable", "message": "deploy in progress"}})
        if StubHandler.behavior == "http_429_once" and len(StubHandler.calls) == 1:
            return self._json(429, {"error": {"code": "rate_limited", "message": "slow down"}},
                              headers={"retry-after": "0"})
        if StubHandler.behavior == "rpc_auth_error":
            return self._json(200, {"jsonrpc": "2.0", "id": body["id"],
                                    "error": {"code": -32001, "message": "Unauthorized: invalid API key"}})
        if StubHandler.behavior == "rpc_null_message":
            return self._json(200, {"jsonrpc": "2.0", "id": body["id"],
                                    "error": {"code": -32600, "message": None}})
        if StubHandler.behavior == "rpc_rate_limit_once" and len(StubHandler.calls) == 1:
            return self._json(200, {"jsonrpc": "2.0", "id": body["id"],
                                    "error": {"code": -32000, "message": "Rate limit reached. wait about 0s and try again."}})
        if StubHandler.behavior == "rate_limit_once" and len(StubHandler.calls) == 1:
            result = {
                "content": [{"type": "text", "text": "Rate limit reached. wait about 1s and try again."}],
                "isError": True,
            }
        elif StubHandler.behavior == "tool_error":
            result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        elif StubHandler.behavior == "tool_error_auth":
            result = {"content": [{"type": "text", "text": "Unauthorized: invalid API key"}], "isError": True}
        elif tool == "sixta_analyze_schema_change":
            result = {"content": [{"type": "text", "text": "**SIXTA schema-change analysis**"}], "structuredContent": SCHEMA_STRUCT}
        else:
            result = {"content": [{"type": "text", "text": "**SIXTA query analysis**"}], "structuredContent": QUERY_STRUCT}
        self._json(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    _json = json_reply

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def stub_server():
    yield from run_stub_server(StubHandler)


def test_client_sends_bearer_and_bare_tools_call(stub_server):
    client = sr.SixtaClient(stub_server, api_key="sk-test")
    result = client.call("sixta_analyze_schema_change", {"sql": "CREATE INDEX i ON t(c);", "engine": "postgresql"})
    assert result["structuredContent"]["overall_risk"] == "HIGH"
    call = StubHandler.calls[0]
    assert call["auth"] == "Bearer sk-test"
    assert call["body"]["method"] == "tools/call"  # bare call, no initialize


def test_client_retries_rate_limit(stub_server):
    StubHandler.behavior = "rate_limit_once"
    client = sr.SixtaClient(stub_server, api_key=None)
    result = client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert len(StubHandler.calls) == 2
    assert result["structuredContent"]["verdict"] == "findings"


def test_client_tool_error_raises(stub_server):
    StubHandler.behavior = "tool_error"
    with pytest.raises(sr.SixtaToolError):
        sr.SixtaClient(stub_server, api_key=None).call("sixta_analyze_query", {"query": "SELECT 1"})


def test_client_http_401_names_auth_not_unreachable(stub_server):
    StubHandler.behavior = "http_401"
    client = sr.SixtaClient(stub_server, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    msg = str(exc.value)
    assert "HTTP 401" in msg
    assert "SIXTA_API_KEY" in msg
    assert "invalid API key" in msg
    assert "unreachable" not in msg


def test_client_http_401_with_string_error_body(stub_server):
    # {"error": "unauthorized"} (a shape some proxies use) must not crash the handler
    StubHandler.behavior = "http_401_string_error"
    client = sr.SixtaClient(stub_server, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    msg = str(exc.value)
    assert "HTTP 401" in msg and "unauthorized" in msg and "SIXTA_API_KEY" in msg


def test_client_http_503_is_connectivity(stub_server):
    StubHandler.behavior = "http_503"
    client = sr.SixtaClient(stub_server, api_key=None)
    with pytest.raises(sr.SixtaConnectivityError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert "HTTP 503" in str(exc.value)


def test_client_http_429_retries(stub_server):
    StubHandler.behavior = "http_429_once"
    client = sr.SixtaClient(stub_server, api_key=None)
    result = client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert len(StubHandler.calls) == 2
    assert result["structuredContent"]["verdict"] == "findings"


def test_client_http_401_anonymous_is_tool_error_not_auth(stub_server):
    # No key configured (fork PRs, anonymous callers): fail-open must still apply.
    StubHandler.behavior = "http_401"
    client = sr.SixtaClient(stub_server, api_key=None)
    with pytest.raises(sr.SixtaToolError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert not isinstance(exc.value, sr.SixtaAuthError)
    assert "set SIXTA_API_KEY" in str(exc.value)


def test_client_rpc_auth_rejection_with_key_is_auth_error(stub_server):
    # An unambiguous rejection ("Unauthorized: invalid API key") of a
    # configured key gates even when it arrives as a 200 JSON-RPC error.
    StubHandler.behavior = "rpc_auth_error"
    client = sr.SixtaClient(stub_server, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert "SIXTA_API_KEY" in str(exc.value)


def test_client_rpc_auth_flavored_anonymous_stays_tool_error(stub_server):
    # Without a configured key there is nothing to rotate: fail-open applies.
    StubHandler.behavior = "rpc_auth_error"
    client = sr.SixtaClient(stub_server, api_key=None)
    with pytest.raises(sr.SixtaToolError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert not isinstance(exc.value, sr.SixtaAuthError)
    assert "SIXTA_API_KEY" in str(exc.value)


def test_client_iserror_auth_rejection_with_key_is_auth_error(stub_server):
    StubHandler.behavior = "tool_error_auth"
    client = sr.SixtaClient(stub_server, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError):
        client.call("sixta_analyze_query", {"query": "SELECT 1"})


def test_hint_decoration_is_tentative_and_gate_matcher_is_strict():
    # Broad matcher decorates with a conditional hint, never a definitive claim
    decorated = sr._hint_if_auth("Statement type forbidden by policy: DROP DATABASE")
    assert "if this is an authentication problem" in decorated
    assert "authentication failed" not in decorated
    # The gate matcher must not fire on policy verdicts or quota nudges
    assert not sr._is_auth_rejection("Statement type forbidden by policy: DROP DATABASE")
    assert not sr._is_auth_rejection("your API key has exceeded its monthly quota")
    assert sr._is_auth_rejection("Unauthorized: invalid API key")


def test_client_rpc_error_null_message_does_not_crash(stub_server):
    StubHandler.behavior = "rpc_null_message"
    client = sr.SixtaClient(stub_server, api_key=None)
    with pytest.raises(sr.SixtaToolError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert "-32600" in str(exc.value)  # falls back to the whole error object


def test_client_rpc_rate_limit_retries(stub_server):
    StubHandler.behavior = "rpc_rate_limit_once"
    client = sr.SixtaClient(stub_server, api_key=None)
    result = client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert len(StubHandler.calls) == 2
    assert result["structuredContent"]["verdict"] == "findings"


def test_http_error_message_survives_truncated_body():
    class TruncatedBody(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"")

    exc = urllib.error.HTTPError("http://x/mcp", 502, "Bad Gateway", {}, TruncatedBody())
    assert sr._http_error_message(exc, "http://x/mcp") == "HTTP 502 from http://x/mcp"


def test_retry_after_header_parses_seconds_dates_and_garbage():
    assert sr._retry_after_header_seconds("42") == 42
    assert sr._retry_after_header_seconds("999") == sr.RETRY_AFTER_CAP_S
    assert sr._retry_after_header_seconds(None) == sr.RETRY_AFTER_DEFAULT_S
    assert sr._retry_after_header_seconds("not-a-date") == sr.RETRY_AFTER_DEFAULT_S
    # HTTP-date in the past clamps to 0 instead of falling back to the default
    assert sr._retry_after_header_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_redact_url_strips_credentials_and_query():
    assert sr._redact_url("https://user:secret@sixta.corp:8443/mcp?token=abc#f") == \
        "https://sixta.corp:8443/mcp"
    assert sr._redact_url("http://[::1]:8080/mcp") == "http://[::1]:8080/mcp"


def test_connectivity_error_redacts_url_credentials():
    client = sr.SixtaClient("http://user:secret@127.0.0.1:1/mcp", api_key=None, timeout=1)
    with pytest.raises(sr.SixtaConnectivityError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert "secret" not in str(exc.value)
    with pytest.raises(sr.SixtaConnectivityError) as exc:
        client.analyze_v1({"engine": "postgresql", "extractions": []})
    assert "secret" not in str(exc.value)


def test_client_http_429_exhausted_says_rate_limited(stub_server):
    StubHandler.behavior = "http_429_once"
    client = sr.SixtaClient(stub_server, api_key=None, max_retries=0)
    with pytest.raises(sr.SixtaConnectivityError) as exc:
        client.call("sixta_analyze_query", {"query": "SELECT 1"})
    assert "rate limited" in str(exc.value) and "HTTP 429" in str(exc.value)


def test_auth_error_propagates_for_main_to_decide(stub_server):
    # No mid-level catch may swallow or exit on SixtaAuthError; main() owns the policy.
    StubHandler.behavior = "http_401"
    client = sr.SixtaClient(stub_server, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError):
        sr.analyze_sql(client, "x.py", "CREATE INDEX i ON t (c);", "postgresql", None, {}, "open")


def test_client_connectivity_error():
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    with pytest.raises(sr.SixtaConnectivityError):
        client.call("sixta_analyze_query", {"query": "SELECT 1"})


def test_analyze_sql_routes_ddl_and_dml(stub_server):
    client = sr.SixtaClient(stub_server, api_key=None)
    sql = "BEGIN;\nCREATE INDEX i ON shop_order (status);\nUPDATE shop_order SET status = 'new' WHERE status = NULL;\nCOMMIT;\n"
    report = sr.analyze_sql(client, "shop/migrations/0002_x.py", sql, "postgresql", "16", {}, "open")
    tools = [c["body"]["params"]["name"] for c in StubHandler.calls]
    assert tools == ["sixta_analyze_schema_change", "sixta_analyze_query"]
    assert StubHandler.calls[0]["body"]["params"]["arguments"]["version"] == "16"
    sevs = sorted(f.severity for f in report.findings)
    assert sevs == ["Critical", "High"]
    assert not report.skipped


def test_analyze_sql_hints_reach_the_call(stub_server):
    client = sr.SixtaClient(stub_server, api_key=None)
    hints = {"shop_order": {"size_bytes": 12_000_000_000, "has_foreign_keys": True}}
    sr.analyze_sql(client, "x.py", "CREATE INDEX i ON shop_order (status);", "postgresql", None, hints, "open")
    args = StubHandler.calls[0]["body"]["params"]["arguments"]
    assert args["table_size_bytes"] == 12_000_000_000
    assert args["table_has_foreign_keys"] is True


def test_fail_open_records_skip_and_does_not_gate():
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    report = sr.analyze_sql(client, "x.py", "CREATE INDEX i ON t (c);", "postgresql", None, {}, "open")
    assert report.findings == []
    assert len(report.skipped) == 1
    assert sr.worst_rank([report]) == -1


def test_fail_closed_exits():
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    with pytest.raises(SystemExit) as exc:
        sr.analyze_sql(client, "x.py", "CREATE INDEX i ON t (c);", "postgresql", None, {}, "closed")
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# Gate + markdown
# --------------------------------------------------------------------------

def _report_with(sev):
    return sr.FileReport(path="x.py", findings=[sr.Finding(path="x.py", severity=sev, description="d")])


@pytest.mark.parametrize(
    "gate,sev,fails",
    [
        ("high", "High", True),
        ("high", "Critical", True),
        ("high", "Medium", False),
        ("medium", "Medium", True),
        ("critical", "High", False),
        ("none", "Critical", False),
    ],
)
def test_gate_logic(gate, sev, fails):
    gate_rank = sr.GATE_RANK[gate]
    worst = sr.worst_rank([_report_with(sev)])
    assert ((gate_rank is not None) and worst >= gate_rank) == fails


def test_markdown_contains_marker_summary_and_gate():
    md = sr.render_markdown([_report_with("High")], gate="high")
    assert md.startswith(sr.NOTE_MARKER)
    assert "1 file(s) analyzed, 1 finding(s), worst severity **High**" in md
    assert "gate (high) failed" in md
    assert "### `x.py`" in md


def test_hints_loader_json_content_in_yml(tmp_path, monkeypatch):
    (tmp_path / ".sixta.yml").write_text('{"tables": {"Orders": {"size_bytes": 7}}}')
    hints = sr.load_table_hints(str(tmp_path))
    assert hints == {"orders": {"size_bytes": 7}}  # keys lowercased
