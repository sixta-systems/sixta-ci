"""Tests for the SIXTA backend PR-gate intake (stub HTTP server, no network)."""

import json
from http.server import BaseHTTPRequestHandler

import pytest

import sixta_review as sr
from conftest import json_reply, run_stub_server


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# Endpoint derivation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://sixta.example.com", "https://sixta.example.com/api/v1/intake/pr-gate"),
        ("https://sixta.example.com/", "https://sixta.example.com/api/v1/intake/pr-gate"),
        ("https://sixta.example.com/api/v1/intake/pr-gate", "https://sixta.example.com/api/v1/intake/pr-gate"),
        ("http://127.0.0.1:8100", "http://127.0.0.1:8100/api/v1/intake/pr-gate"),
    ],
)
def test_intake_endpoint(url, expected):
    assert sr.intake_endpoint(url) == expected


# --------------------------------------------------------------------------
# Run identity from the CI environment
# --------------------------------------------------------------------------

def _github_pr_env(monkeypatch, tmp_path, number=7, head_sha="abc1234def"):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": number, "head": {"sha": head_sha}}}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/app")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "41")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")


def test_intake_context_github(monkeypatch, tmp_path):
    _github_pr_env(monkeypatch, tmp_path)
    assert sr.intake_context("github") == {
        "repo": "octo/app",
        "pr": 7,
        "pr_url": "https://github.com/octo/app/pull/7",
        "head_sha": "abc1234def",
        "run": 41002,  # run_number*1000 + attempt: a re-attempt outranks what it retries
    }


def test_intake_context_github_without_pr_is_none(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/app")  # a branch push: no PR payload
    assert sr.intake_context("github") is None


def test_intake_context_gitlab(monkeypatch):
    monkeypatch.setenv("CI_PROJECT_PATH", "group/app")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "9")
    monkeypatch.setenv("CI_PIPELINE_IID", "345")
    monkeypatch.setenv("CI_PROJECT_URL", "https://gitlab.example.com/group/app")
    monkeypatch.setenv("CI_COMMIT_SHA", "deadbeef00")
    assert sr.intake_context("gitlab") == {
        "repo": "group/app",
        "pr": 9,
        "pr_url": "https://gitlab.example.com/group/app/-/merge_requests/9",
        "head_sha": "deadbeef00",
        "run": 345,
    }


def test_intake_context_gitlab_without_mr_is_none(monkeypatch):
    monkeypatch.setenv("CI_PROJECT_PATH", "group/app")
    assert sr.intake_context("gitlab") is None


# --------------------------------------------------------------------------
# The finding set (joined to the statements the server judged)
# --------------------------------------------------------------------------

def _extractions():
    return [
        {"kind": "migration", "sql": "CREATE INDEX i ON shop_order (status);", "source_file": "m/0002.sql"},
        {"kind": "query", "sql": "UPDATE shop_order SET s=1 WHERE s = NULL", "source_file": "m/0002.sql"},
    ]


def _response():
    return {
        "results": [
            {"index": 0, "kind": "migration", "source_file": "m/0002.sql", "findings": [
                {"rule_id": "ddl:CREATE_INDEX", "title": "create index on shop_order", "severity": "High",
                 "operation": "CREATE_INDEX", "table": "shop_order", "source_file": "m/0002.sql", "source_line": 1},
                {"title": "no rule id", "severity": "Info"},  # identity-less: skipped, never fileable
            ]},
            {"index": 1, "kind": "query", "source_file": "m/0002.sql", "findings": [
                {"rule_id": "NULL-EQUALS", "title": "Equality comparison with NULL", "severity": "Critical",
                 "source_file": "m/0002.sql", "source_line": 2},
            ]},
        ],
    }


def test_intake_findings_joins_statements_by_index():
    got = sr.intake_findings(_response(), _extractions())
    assert got == [
        {"rule_id": "ddl:CREATE_INDEX", "severity": "High", "sql": "CREATE INDEX i ON shop_order (status);",
         "title": "create index on shop_order", "operation": "CREATE_INDEX", "table": "shop_order",
         "source_file": "m/0002.sql", "source_line": 1},
        {"rule_id": "NULL-EQUALS", "severity": "Critical", "sql": "UPDATE shop_order SET s=1 WHERE s = NULL",
         "title": "Equality comparison with NULL", "source_file": "m/0002.sql", "source_line": 2},
    ]


def test_intake_findings_none_on_error_result():
    resp = _response()
    resp["results"][1] = {"index": 1, "kind": "query", "error": {"code": "invalid_input", "message": "not SQL"}}
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_on_rate_limited_result():
    resp = _response()
    resp["results"][1] = {"index": 1, "kind": "query", "rate_limited": True, "retry_after": 3}
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_when_a_findings_statement_is_unrecoverable():
    resp = _response()
    resp["results"][1]["index"] = 99  # the join is broken: the finding cannot carry its statement
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_when_an_extraction_is_unanswered():
    # A response omitting a submitted extraction is a run that did not
    # analyze it: posting the remainder as authoritative would resolve the
    # missing statement's findings unchecked.
    resp = _response()
    del resp["results"][1]
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_on_a_duplicate_result_index():
    resp = _response()
    resp["results"][1]["index"] = 0
    resp["results"][1]["kind"] = "migration"
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_when_a_result_answers_as_the_wrong_kind():
    # A migration result at a submitted query extraction's index is not the
    # analysis that was asked for; joining it to the query SQL would post
    # findings under the wrong contract as authoritative.
    resp = _response()
    resp["results"][1]["kind"] = "migration"
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_none_when_an_informational_kind_displaces_a_result():
    # The kit never submits an "explain" extraction, so an extraction
    # answered only by one was not analyzed as migration or query.
    resp = _response()
    resp["results"][1]["kind"] = "explain"
    assert sr.intake_findings(resp, _extractions()) is None


def test_intake_findings_ignores_unknown_result_kinds():
    resp = _response()
    resp["results"].append({"index": 0, "kind": "explain", "findings": [{"rule_id": "X", "severity": "Info"}]})
    got = sr.intake_findings(resp, _extractions())
    assert [f["rule_id"] for f in got] == ["ddl:CREATE_INDEX", "NULL-EQUALS"]


# --------------------------------------------------------------------------
# Stub intake server
# --------------------------------------------------------------------------

class StubIntakeHandler(BaseHTTPRequestHandler):
    calls: list = []
    behavior: str = "ok"  # ok | stale | boom

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        StubIntakeHandler.calls.append({
            "path": self.path,
            "auth": self.headers.get("authorization"),
            "body": json.loads(raw),
        })
        if StubIntakeHandler.behavior == "stale":
            return self._json(409, {"error": "stale_run", "detail": "run 3 arrived after run 5 was recorded"})
        if StubIntakeHandler.behavior == "boom":
            return self._json(500, {"error": "internal"})
        body = StubIntakeHandler.calls[-1]["body"]
        return self._json(200, {"filed": len(body.get("findings") or []), "created": 1,
                                "regressed": 0, "refreshed": 0, "resolved": 2})

    _json = json_reply

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_intake():
    # run_stub_server yields ".../mcp"; the intake wants the bare base URL.
    for url in run_stub_server(StubIntakeHandler):
        yield url.rsplit("/mcp", 1)[0]


# --------------------------------------------------------------------------
# post_intake
# --------------------------------------------------------------------------

def test_post_intake_posts_bearer_and_the_full_set(stub_intake, monkeypatch, tmp_path):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    opts = _opts(intake_url=stub_intake, platform="github", engine="postgresql")
    sr.post_intake(opts, [{"rule_id": "NULL-EQUALS", "severity": "Critical", "sql": "SELECT 1"}])
    assert len(StubIntakeHandler.calls) == 1
    call = StubIntakeHandler.calls[0]
    assert call["path"] == "/api/v1/intake/pr-gate"
    assert call["auth"] == "Bearer tok-1"
    assert call["body"]["repo"] == "octo/app"
    assert call["body"]["pr"] == 7
    assert call["body"]["run"] == 41002
    assert call["body"]["engine"] == "postgresql"
    assert call["body"]["findings"] == [{"rule_id": "NULL-EQUALS", "severity": "Critical", "sql": "SELECT 1"}]


def test_post_intake_clean_run_posts_the_empty_set(stub_intake, monkeypatch, tmp_path):
    # The empty set is the post that resolves the previous push's findings.
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), [])
    assert len(StubIntakeHandler.calls) == 1
    assert StubIntakeHandler.calls[0]["body"]["findings"] == []


def test_post_intake_incomplete_run_posts_nothing(stub_intake, monkeypatch, tmp_path):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), None)
    assert StubIntakeHandler.calls == []


def test_post_intake_unconfigured_is_silent(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    sr.post_intake(_opts(platform="github"), [])
    assert StubIntakeHandler.calls == []
    assert "intake" not in capsys.readouterr().err


def test_post_intake_half_configured_warns_and_skips(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")  # token without a URL
    sr.post_intake(_opts(platform="github"), [])
    assert StubIntakeHandler.calls == []
    assert "must both be set" in capsys.readouterr().err


def test_post_intake_local_run_never_posts(stub_intake, monkeypatch, tmp_path):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github", local=True), [])
    assert StubIntakeHandler.calls == []


def test_post_intake_mcp_mode_never_posts_even_the_empty_set(stub_intake, monkeypatch, tmp_path, capsys):
    # In mcp mode a non-empty run never posts, so an empty post (the
    # no-changed-files path included) could resolve findings a later mcp
    # run has no way to re-file. The gate lives inside post_intake so no
    # call site can forget it.
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github", api="mcp"), [])
    assert StubIntakeHandler.calls == []
    assert "SIXTA_API=v1" in capsys.readouterr().err


def test_post_intake_plain_http_beyond_loopback_warns(monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url="http://sixta.invalid:8100", platform="github"), [])
    assert "unencrypted" in capsys.readouterr().err


def test_post_intake_loopback_http_is_not_warned_about(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), [])
    assert "unencrypted" not in capsys.readouterr().err


def test_post_intake_outside_a_pr_posts_nothing(stub_intake, monkeypatch):
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), [])
    assert StubIntakeHandler.calls == []


def test_post_intake_over_the_backend_bound_skips(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    too_many = [{"rule_id": f"R{i}", "severity": "Info", "sql": "SELECT 1"} for i in range(sr.INTAKE_MAX_FINDINGS + 1)]
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), too_many)
    assert StubIntakeHandler.calls == []
    assert "per-run bound" in capsys.readouterr().err


def test_post_intake_http_error_never_raises(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    StubIntakeHandler.behavior = "boom"
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), [])
    assert "gate is unaffected" in capsys.readouterr().err


def test_post_intake_stale_run_refusal_is_calm(stub_intake, monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    StubIntakeHandler.behavior = "stale"
    sr.post_intake(_opts(intake_url=stub_intake, platform="github"), [])
    err = capsys.readouterr().err
    assert "stale" in err
    assert "WARNING" not in err  # the guard working as designed is not a warning


def test_post_intake_connection_refused_never_raises(monkeypatch, tmp_path, capsys):
    _github_pr_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SIXTA_INTAKE_TOKEN", "tok-1")
    sr.post_intake(_opts(intake_url="http://127.0.0.1:9", platform="github"), [])
    assert "gate is unaffected" in capsys.readouterr().err


# --------------------------------------------------------------------------
# run_v1 integration: the intake set rides the return
# --------------------------------------------------------------------------

class StubV1MiniHandler(BaseHTTPRequestHandler):
    calls: list = []
    behavior: str = "ok"

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        request = json.loads(raw)
        StubV1MiniHandler.calls.append({"request": request})
        results = []
        for i, ex in enumerate(request.get("extractions", [])):
            res = {"index": i, "kind": ex["kind"], "source_file": ex.get("source_file"),
                   "overall_severity": "Critical",
                   "findings": [{"rule_id": "NULL-EQUALS", "title": "Equality comparison with NULL",
                                 "severity": "Critical", "source_file": ex.get("source_file"), "source_line": 1}],
                   "report_text": "**SIXTA query analysis**"}
            if StubV1MiniHandler.behavior == "rate_limit":
                res = {"index": i, "kind": ex["kind"], "rate_limited": True, "retry_after": 3}
            results.append(res)
        return self._json(200, {"results": results, "worst_severity": "Critical"})

    _json = json_reply

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_v1_mini():
    yield from run_stub_server(StubV1MiniHandler)


def test_run_v1_returns_the_intake_set(stub_v1_mini, tmp_path):
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET s='n' WHERE s = NULL;\n")
    client = sr.SixtaClient(stub_v1_mini, api_key=None)
    *_, intake = sr.run_v1([str(sql)], _opts(), client, hints={})
    assert intake == [{
        "rule_id": "NULL-EQUALS", "severity": "Critical",
        "sql": "UPDATE shop_order SET s='n' WHERE s = NULL",
        "title": "Equality comparison with NULL", "source_file": str(sql), "source_line": 1,
    }]


def test_run_v1_intake_none_when_a_result_was_rate_limited(stub_v1_mini, tmp_path):
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET s='n' WHERE s = NULL;\n")
    StubV1MiniHandler.behavior = "rate_limit"
    client = sr.SixtaClient(stub_v1_mini, api_key=None)
    *_, intake = sr.run_v1([str(sql)], _opts(), client, hints={})
    assert intake is None


def test_run_v1_intake_none_when_a_files_sql_could_not_be_rendered(monkeypatch):
    def _boom(path, opts):
        raise RuntimeError("render failed")
    monkeypatch.setattr(sr, "extract_migration", _boom)
    *_, intake = sr.run_v1(["shop/migrations/0002_x.py"], _opts(), client=None, hints={})
    assert intake is None


def test_run_v1_intake_empty_when_nothing_was_extractable(tmp_path):
    sql = tmp_path / "session.sql"
    sql.write_text("SET search_path TO shop;\n")  # a skip-keyword statement: nothing analyzable
    *_, intake = sr.run_v1([str(sql)], _opts(), client=None, hints={})
    assert intake == []
