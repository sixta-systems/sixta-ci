"""Tests for the /v1/analyze batch mode (stub REST server, no network)."""

import json
import subprocess
from http.server import BaseHTTPRequestHandler

import pytest

import sixta_review as sr
from conftest import json_reply, run_stub_server


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://connect.sixta.ai/mcp", "https://connect.sixta.ai/v1/analyze"),
        ("http://127.0.0.1:8080/mcp", "http://127.0.0.1:8080/v1/analyze"),
        ("https://connect.sixta.ai/v1/analyze", "https://connect.sixta.ai/v1/analyze"),
        ("https://example.test/", "https://example.test/v1/analyze"),
    ],
)
def test_v1_endpoint(url, expected):
    assert sr.v1_endpoint(url) == expected


def test_v1_table_hints_projects_known_keys():
    hints = {"Orders": {"size_bytes": 5, "has_foreign_keys": True, "nonsense": 1}, "bad": "notadict"}
    assert sr._v1_table_hints(hints) == {"orders": {"size_bytes": 5, "has_foreign_keys": True}}


def test_capture_schema_runs_explicit_cmd(monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = "printf 'CREATE TABLE orders (id int);'"
    assert sr.capture_schema(opts) == "CREATE TABLE orders (id int);"


def test_capture_schema_none_without_db_or_cmd(monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    assert sr.capture_schema(opts) is None


# --------------------------------------------------------------------------
# Stub /v1/analyze server
# --------------------------------------------------------------------------

def _v1_response(request: dict) -> dict:
    results, cq = [], []
    for i, ex in enumerate(request.get("extractions", [])):
        src = ex.get("source_file")
        if ex["kind"] == "migration":
            findings = [{"rule_id": "ddl:CREATE_INDEX", "title": "create index on shop_order",
                         "severity": "High", "operation": "CREATE_INDEX", "table": "shop_order",
                         "source_file": src, "source_line": 1}]
            results.append({"index": i, "kind": "migration", "source_file": src, "overall_severity": "High",
                            "overall_risk": "HIGH", "findings": findings, "report_text": "**SIXTA schema-change analysis**"})
            cq.append({"description": "create index on shop_order", "check_name": "sixta:ddl:CREATE_INDEX",
                       "fingerprint": "a" * 40, "severity": "critical",
                       "location": {"path": src, "lines": {"begin": 1}}})
        elif ex["kind"] == "query":
            findings = [{"rule_id": "NULL-EQUALS", "title": "Equality comparison with NULL",
                         "severity": "Critical", "source_file": src, "source_line": 1}]
            results.append({"index": i, "kind": "query", "source_file": src, "overall_severity": "Critical",
                            "findings": findings, "report_text": "**SIXTA query analysis**"})
            cq.append({"description": "Equality comparison with NULL", "check_name": "sixta:NULL-EQUALS",
                       "fingerprint": "b" * 40, "severity": "blocker",
                       "location": {"path": src, "lines": {"begin": 1}}})
    return {"engine": request.get("engine"), "results": results, "worst_severity": "Critical",
            "renders": {"code_quality": cq, "markdown": "server markdown"}, "usage": {"calls_charged": len(results)}}


class StubV1Handler(BaseHTTPRequestHandler):
    calls: list = []
    behavior: str = "ok"  # ok | rate_limit | extraction_error | http_error | http_401 | http_503 | http_429_once

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        request = json.loads(raw)
        StubV1Handler.calls.append({"request": request, "auth": self.headers.get("authorization"), "path": self.path})

        if self.path != "/v1/analyze":
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        if StubV1Handler.behavior == "http_error":
            return self._json(413, {"error": {"code": "payload_too_large", "message": "body too big"}})
        if StubV1Handler.behavior == "http_401":
            return self._json(401, {"error": {"code": "unauthorized", "message": "invalid API key"}})
        if StubV1Handler.behavior == "http_503":
            return self._json(503, {"error": {"code": "unavailable", "message": "deploy in progress"}})
        if StubV1Handler.behavior == "http_429_once" and len(StubV1Handler.calls) == 1:
            return self._json(429, {"error": {"code": "rate_limited", "message": "slow down"}},
                              headers={"retry-after": "0"})

        resp = _v1_response(request)
        if StubV1Handler.behavior == "advisory_worst" and resp["results"]:
            # Server policy: advisory rollback:* findings are visible at their
            # own severity but excluded from worst_severity (sixta-connect #75).
            resp["results"][0]["findings"] = [{
                "rule_id": "rollback:ddl:DROP_TABLE", "title": "rollback: drop table on promo_codes",
                "severity": "Critical", "source_file": resp["results"][0].get("source_file"),
            }]
            resp["results"][0]["overall_severity"] = "Info"
            resp["worst_severity"] = "Info"
        if StubV1Handler.behavior == "rate_limit" and resp["results"]:
            resp["results"][-1] = {"index": len(resp["results"]) - 1, "kind": resp["results"][-1]["kind"],
                                   "source_file": resp["results"][-1]["source_file"], "rate_limited": True, "retry_after": 3}
        if StubV1Handler.behavior == "extraction_error" and resp["results"]:
            resp["results"][-1] = {"index": len(resp["results"]) - 1, "kind": resp["results"][-1]["kind"],
                                   "source_file": resp["results"][-1]["source_file"],
                                   "error": {"code": "invalid_input", "message": "not SQL"}}
        return self._json(200, resp)

    _json = json_reply

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_v1():
    yield from run_stub_server(StubV1Handler)  # /mcp base; client derives /v1/analyze


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def test_analyze_v1_sends_bearer_and_posts_to_v1(stub_v1):
    client = sr.SixtaClient(stub_v1, api_key="sk-test")
    resp = client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    assert resp["worst_severity"] == "Critical"
    call = StubV1Handler.calls[0]
    assert call["path"] == "/v1/analyze"
    assert call["auth"] == "Bearer sk-test"


def test_analyze_v1_http_error_raises_tool_error(stub_v1):
    StubV1Handler.behavior = "http_error"
    client = sr.SixtaClient(stub_v1, api_key=None)
    with pytest.raises(sr.SixtaToolError) as exc:
        client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    assert "body too big" in str(exc.value)


def test_analyze_v1_http_401_names_auth_and_url(stub_v1):
    StubV1Handler.behavior = "http_401"
    client = sr.SixtaClient(stub_v1, api_key="sk-bad")
    with pytest.raises(sr.SixtaAuthError) as exc:
        client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    msg = str(exc.value)
    assert "HTTP 401" in msg and "SIXTA_API_KEY" in msg and "/v1/analyze" in msg


def test_analyze_v1_http_429_retries(stub_v1):
    StubV1Handler.behavior = "http_429_once"
    client = sr.SixtaClient(stub_v1, api_key=None)
    resp = client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    assert len(StubV1Handler.calls) == 2
    assert resp["worst_severity"] == "Critical"


def test_analyze_v1_http_401_anonymous_is_tool_error(stub_v1):
    StubV1Handler.behavior = "http_401"
    client = sr.SixtaClient(stub_v1, api_key=None)
    with pytest.raises(sr.SixtaToolError) as exc:
        client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    assert not isinstance(exc.value, sr.SixtaAuthError)
    assert "set SIXTA_API_KEY" in str(exc.value)


def test_analyze_v1_http_5xx_is_connectivity(stub_v1):
    StubV1Handler.behavior = "http_503"
    client = sr.SixtaClient(stub_v1, api_key=None)
    with pytest.raises(sr.SixtaConnectivityError) as exc:
        client.analyze_v1({"engine": "postgresql", "extractions": [{"kind": "query", "sql": "SELECT 1"}]})
    assert "HTTP 503" in str(exc.value)


def test_analyze_v1_connectivity_error():
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    with pytest.raises(sr.SixtaConnectivityError):
        client.analyze_v1({"engine": "postgresql", "extractions": []})


# --------------------------------------------------------------------------
# run_v1 orchestration
# --------------------------------------------------------------------------

def test_run_v1_batches_ddl_and_dml_in_one_post(stub_v1, tmp_path):
    sql = tmp_path / "changes.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);\nUPDATE shop_order SET status='n' WHERE status = NULL;\n")
    client = sr.SixtaClient(stub_v1, api_key=None)
    reports, renders, _context, _worst, _badge, _outcomes = sr.run_v1([str(sql)], _opts(), client, hints={})

    # One POST for the whole run; DDL grouped into one migration extraction,
    # DML as its own query extraction.
    assert len(StubV1Handler.calls) == 1
    kinds = [e["kind"] for e in StubV1Handler.calls[0]["request"]["extractions"]]
    assert kinds == ["migration", "query"]

    # Verdicts map back onto the file: both findings land, worst is Critical.
    assert len(reports) == 1
    sevs = sorted(f.severity for f in reports[0].findings)
    assert sevs == ["Critical", "High"]
    assert sr.worst_rank(reports) == sr.SEVERITY_RANK["Critical"]
    # Server-side renders are returned for the code-quality artifact.
    assert renders and len(renders["code_quality"]) == 2


def test_run_v1_passes_schema_and_hints(stub_v1, tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    opts = _opts(schema_cmd="printf 'CREATE TABLE shop_order (id int);'")
    sr.run_v1([str(sql)], opts, client, hints={"shop_order": {"size_bytes": 12_000_000_000, "has_foreign_keys": True}})

    req = StubV1Handler.calls[0]["request"]
    assert req["schema"] == {"format": "ddl", "content": "CREATE TABLE shop_order (id int);"}
    assert req["table_hints"] == {"shop_order": {"size_bytes": 12_000_000_000, "has_foreign_keys": True}}
    assert req["options"]["render"] == ["markdown", "code-quality"]


def _clear_ci_env(monkeypatch):
    """Hermetic CI context: these tests assert exact context blocks, so the
    real CI env (GITHUB_ACTIONS, GITLAB_CI, GITLAB_USER_EMAIL) must not leak in."""
    for var in ("GITHUB_REPOSITORY", "CI_PROJECT_PATH", "GITLAB_CI", "GITLAB_USER_EMAIL", "GITHUB_ACTIONS", "GITHUB_EVENT_NAME", "SIXTA_NO_ATTRIBUTION",
                "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "SIXTA_NO_CHECK"):
        monkeypatch.delenv(var, raising=False)


def test_run_v1_sends_repo_ref_from_ci_env(stub_v1, tmp_path, monkeypatch):
    # GitHub sets GITHUB_REPOSITORY; the batch carries it as context.repo_ref so
    # Connect Pro can route to the bound connection.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/app-1")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {"repo_ref": "org/app-1"}


def test_run_v1_omits_context_without_ci_repo(stub_v1, tmp_path, monkeypatch):
    _clear_ci_env(monkeypatch)
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert "context" not in StubV1Handler.calls[0]["request"]


def test_run_v1_sends_operator_from_gitlab_env(stub_v1, tmp_path, monkeypatch):
    # GitLab names the change author directly; sent verbatim (trimmed) — the
    # server stores only a salted hash of it. Honored only on a real runner
    # (GITLAB_CI), so a stray env var locally never attributes a run.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("CI_PROJECT_PATH", "org/app-1")
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("GITLAB_USER_EMAIL", " Jane@Acme.com ")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {"repo_ref": "org/app-1", "operator": "Jane@Acme.com"}


def test_run_v1_gitlab_email_ignored_outside_gitlab_ci(stub_v1, tmp_path, monkeypatch):
    # GITLAB_USER_EMAIL exported by a local shell / dotfiles must NOT attribute:
    # the published promise is that local runs never send an identity.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("CI_PROJECT_PATH", "org/app-1")
    monkeypatch.setenv("GITLAB_USER_EMAIL", "jane@acme.com")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {"repo_ref": "org/app-1"}


def test_run_v1_sends_oidc_token_on_github_platform(stub_v1, tmp_path, monkeypatch):
    # The minted Actions OIDC token rides as context.github.oidc_token — the
    # repo-control proof for the App-posted `SIXTA review` check.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/app-1")
    monkeypatch.setattr(sr, "github_oidc_token", lambda: "oidc-tok")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(platform="github"), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {
        "repo_ref": "org/app-1", "github": {"oidc_token": "oidc-tok"},
    }


def test_run_v1_no_oidc_outside_github_platform(stub_v1, tmp_path, monkeypatch):
    # GitLab (or a local run) never mints or sends the token, even if one were
    # mintable — the check is a GitHub App feature.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("CI_PROJECT_PATH", "org/app-1")
    monkeypatch.setattr(sr, "github_oidc_token", lambda: "oidc-tok")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {"repo_ref": "org/app-1"}


def test_run_v1_operator_github_pr_head_author_not_merge_commit(stub_v1, tmp_path, monkeypatch):
    # pull_request events check out GitHub's synthetic merge commit; the change
    # author is HEAD^2's author, not HEAD's.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/app-1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")

    def fake_git(*args):
        return ["dev@acme.com"] if args[-1] == "HEAD^2" else ["noreply@github.com"]

    monkeypatch.setattr(sr, "_git", fake_git)
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"]["operator"] == "dev@acme.com"


def test_run_v1_operator_pr_shallow_checkout_attributes_nobody(stub_v1, tmp_path, monkeypatch):
    # A shallow checkout cannot resolve HEAD^2 on a pull_request event: send
    # nothing rather than the synthetic merge commit's author (wrong person).
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/app-1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")

    def fake_git(*args):
        if args[-1] == "HEAD^2":
            raise subprocess.CalledProcessError(1, "git")
        return ["noreply@github.com"]

    monkeypatch.setattr(sr, "_git", fake_git)
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert "operator" not in StubV1Handler.calls[0]["request"]["context"]


def test_run_v1_operator_push_uses_head_even_for_merge_commits(stub_v1, tmp_path, monkeypatch):
    # push events (no GITHUB_EVENT_NAME=pull_request*) use HEAD's author — the
    # pushed commit is the change. Never HEAD^2: on a pushed merge commit that
    # would credit the merged-in branch's tip author, a third party.
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/app-1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")

    def fake_git(*args):
        assert args[-1] != "HEAD^2", "push events must not consult HEAD^2"
        return ["merger@acme.com"]

    monkeypatch.setattr(sr, "_git", fake_git)
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"]["operator"] == "merger@acme.com"


def test_run_v1_attribution_opt_out_and_non_ci(stub_v1, tmp_path, monkeypatch):
    # SIXTA_NO_ATTRIBUTION suppresses the operator even when CI names one
    # (any accepted flag spelling, via _env_flag), and outside CI the git
    # fallback never runs (no local-dev attribution).
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("CI_PROJECT_PATH", "org/app-1")
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("GITLAB_USER_EMAIL", "jane@acme.com")
    monkeypatch.setenv("SIXTA_NO_ATTRIBUTION", "true")
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient(stub_v1, api_key=None)
    sr.run_v1([str(sql)], _opts(), client, hints={})
    assert StubV1Handler.calls[0]["request"]["context"] == {"repo_ref": "org/app-1"}

    monkeypatch.delenv("SIXTA_NO_ATTRIBUTION")
    monkeypatch.delenv("GITLAB_CI")
    monkeypatch.delenv("GITLAB_USER_EMAIL")
    monkeypatch.delenv("CI_PROJECT_PATH")
    # Outside CI: even inside a git repo, operator_identity resolves nothing.
    assert sr.operator_identity() is None


def test_run_v1_migration_branch_groups_ddl_and_flags_runpython(stub_v1, monkeypatch):
    monkeypatch.setattr(sr, "render_migration", lambda mp, app, name: "CREATE INDEX i ON shop_order (status);")
    monkeypatch.setattr(sr, "has_runpython", lambda path: True)
    client = sr.SixtaClient(stub_v1, api_key=None)
    reports, _, _context, _worst, _, _ = sr.run_v1(["shop/migrations/0002_x.py"], _opts(), client, hints={})

    # DDL from the rendered migration + a local RunPython manual-review finding.
    checks = sorted(f.check_name for f in reports[0].findings)
    assert "runpython-manual-review" in checks
    assert any("RunPython" in s for s in reports[0].sections)


def test_run_v1_rate_limited_extraction_is_skipped_not_gated(stub_v1, tmp_path):
    StubV1Handler.behavior = "rate_limit"
    sql = tmp_path / "c.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")
    client = sr.SixtaClient(stub_v1, api_key=None)
    reports, _, _context, _worst, _, _ = sr.run_v1([str(sql)], _opts(fail_mode="open"), client, hints={})
    assert reports[0].findings == []  # the only extraction was rate limited
    assert any("rate limited" in s for s in reports[0].skipped)
    assert sr.worst_rank(reports) == -1  # does not gate


def test_run_v1_extraction_error_fail_closed_exits(stub_v1, tmp_path):
    StubV1Handler.behavior = "extraction_error"
    sql = tmp_path / "c.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")
    client = sr.SixtaClient(stub_v1, api_key=None)
    with pytest.raises(SystemExit) as exc:
        sr.run_v1([str(sql)], _opts(fail_mode="closed"), client, hints={})
    assert exc.value.code == 2


def test_run_v1_batch_connectivity_fail_open_skips():
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    reports, renders, _context, _worst, _badge, _outcomes = sr.run_v1(["x.sql"], _opts(fail_mode="open"), client, hints={})
    # x.sql does not exist -> read fails -> no extractions -> no POST, empty batch.
    assert renders is None


def test_run_v1_batch_connectivity_fail_closed_exits(tmp_path):
    sql = tmp_path / "c.sql"
    sql.write_text("CREATE INDEX i ON shop_order (status);")
    client = sr.SixtaClient("http://127.0.0.1:1/mcp", api_key=None, timeout=1)
    with pytest.raises(SystemExit) as exc:
        sr.run_v1([str(sql)], _opts(fail_mode="closed"), client, hints={})
    assert exc.value.code == 2


# --------------------------------------------------------------------------
# main() integration: server-rendered code-quality is preferred
# --------------------------------------------------------------------------

def test_main_auth_failure_exits_2_and_writes_artifact(stub_v1, tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SIXTA_API_KEY", "sk-rotated")
    monkeypatch.chdir(tmp_path)
    StubV1Handler.behavior = "http_401"
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")
    cq_path = tmp_path / "gl-code-quality-report.json"

    with pytest.raises(SystemExit) as exc:
        sr.main(["--api", "v1", "--sixta-url", stub_v1, "--fail-mode", "open",
                 "--report-md", str(tmp_path / "report.md"), "--code-quality", str(cq_path),
                 str(sql)])
    assert exc.value.code == 2
    # The upload step's artifact still exists even though the run gated on auth.
    assert json.loads(cq_path.read_text()) == []
    # The report artifact (declared `when: always` in the GitLab template) is
    # written too, carrying the upsert marker so a stale MR note gets superseded.
    report = (tmp_path / "report.md").read_text()
    assert report.startswith(sr.NOTE_MARKER)
    assert "Analysis did not run" in report and "SIXTA_API_KEY" in report


def test_main_auth_failure_local_warns_and_passes(stub_v1, tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SIXTA_API_KEY", "sk-rotated")
    monkeypatch.chdir(tmp_path)
    StubV1Handler.behavior = "http_401"
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(["--api", "v1", "--sixta-url", stub_v1, "--local", str(sql)])
    assert rc == 0  # a stale local key must not block commits


def test_main_v1_prefers_server_code_quality(stub_v1, tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE", "SIXTA_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")
    cq_path = tmp_path / "gl-code-quality-report.json"

    rc = sr.main([
        "--api", "v1", "--sixta-url", stub_v1, "--gate", "high",
        "--report-md", str(tmp_path / "report.md"), "--code-quality", str(cq_path),
        str(sql),
    ])
    assert rc == 1  # Critical finding trips the high gate

    entries = json.loads(cq_path.read_text())
    # The server's fingerprint sentinel proves we wrote the server render, not a local rebuild.
    assert entries and entries[0]["fingerprint"] == "b" * 40
    assert (tmp_path / "report.md").read_text().startswith(sr.NOTE_MARKER)


def test_main_v1_gates_on_server_worst_severity_not_advisory_findings(stub_v1, tmp_path, monkeypatch):
    """A Critical rollback:* finding is advisory: the server keeps it out of
    worst_severity, and the v1 gate must follow the server's verdict."""
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE", "SIXTA_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    StubV1Handler.behavior = "advisory_worst"
    sql = tmp_path / "V1__create_promo.sql"
    sql.write_text("CREATE TABLE promo_codes (id bigint PRIMARY KEY);")

    rc = sr.main([
        "--api", "v1", "--sixta-url", stub_v1, "--gate", "high",
        "--report-md", str(tmp_path / "report.md"), "--code-quality", str(tmp_path / "cq.json"),
        str(sql),
    ])
    assert rc == 0  # server worst_severity Info < high, despite the Critical advisory finding


def test_v1_sections_quote_the_analyzed_sql(stub_v1, tmp_path):
    sql = tmp_path / "q.sql"
    sql.write_text("UPDATE shop_order SET status = NULL WHERE id = 7;")
    client = sr.SixtaClient(stub_v1, api_key=None)
    reports, _, _, _, _, _ = sr.run_v1([str(sql)], _opts(), client, hints={})
    joined = "\n".join(reports[0].sections)
    assert "```sql" in joined and "UPDATE shop_order SET status = NULL" in joined


def test_v1_no_context_block_invites_connection(stub_v1, tmp_path):
    sql = tmp_path / "q.sql"
    sql.write_text("SELECT 1;")
    client = sr.SixtaClient(stub_v1, api_key=None)
    _, _, context, _, _, _ = sr.run_v1([str(sql)], _opts(), client, hints={})
    assert context == {"source": "none"}
    md = sr.render_markdown([sr.FileReport(path="q.sql", sections=["r"])], "high", context)
    assert "connect.sixta.ai/portal/connections" in md


def test_render_markdown_prefers_server_action_links():
    rep = sr.FileReport(path="m.sql", sections=["report"])
    add = sr.render_markdown([rep], "high", {
        "source": "hints", "docs_url": "https://x/docs/ci",
        "action": {"kind": "add_connection", "url": "https://x/portal/connections"},
    })
    assert "https://x/portal/connections" in add and "https://x/docs/ci" in add
    up = sr.render_markdown([rep], "high", {
        "source": "none", "docs_url": "https://x/docs/ci",
        "action": {"kind": "upgrade", "url": "https://x/pricing"},
    })
    assert "Connect Pro: https://x/pricing" in up
    legacy = sr.render_markdown([rep], "high", {"source": "hints"})
    assert "connect.sixta.ai/portal/connections" in legacy


def test_render_markdown_reports_context_source():
    rep = sr.FileReport(path="m.sql", sections=["report"])
    live = sr.render_markdown([rep], "high", {"source": "live", "captured_at": "2026-07-04T12:00:00Z"})
    assert "Production context: **live** database snapshot (snapshot 2026-07-04T12:00:00Z)." in live
    hints = sr.render_markdown([rep], "high", {"source": "hints"})
    assert "Production context: **declared hints**" in hints
    free = sr.render_markdown([rep], "high", None)
    assert "Production context" not in free
    # Grounded-connection guardrail: a live response carrying a note surfaces it.
    warned = sr.render_markdown([rep], "high", {"source": "live", "note": "Grounded on the default writer (db-a), route its repository."})
    assert "Grounded on the default writer (db-a), route its repository." in warned
    assert "⚠" in warned  # warning glyph
    # No note → no warning line.
    assert "⚠" not in live
