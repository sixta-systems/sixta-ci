"""Tests for the GitHub Actions platform mode (no network; stub REST server)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sixta_review as sr


# --------------------------------------------------------------------------
# Platform detection + PR context
# --------------------------------------------------------------------------

def test_detect_platform_explicit_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert sr.detect_platform("gitlab") == "gitlab"


def test_detect_platform_auto(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert sr.detect_platform("auto") == "github"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert sr.detect_platform("auto") == "gitlab"


def test_github_base_sha_from_event_payload(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"base": {"sha": "abc123"}, "number": 7}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert sr.github_base_sha() == "abc123"
    assert sr.github_pr_number() == "7"


def test_github_pr_number_from_ref(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setenv("GITHUB_REF", "refs/pull/42/merge")
    assert sr.github_pr_number() == "42"


def test_api_defaults_to_v1_on_github(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("SIXTA_API", raising=False)
    # Mirror main()'s resolution order: explicit flag > env > platform default.
    opts = sr.build_parser().parse_args([])
    platform = sr.detect_platform(opts.platform)
    api = opts.api or None or ("v1" if platform == "github" else "mcp")
    assert (platform, api) == ("github", "v1")


# --------------------------------------------------------------------------
# SARIF (local fallback)
# --------------------------------------------------------------------------

def test_build_sarif_shape_and_levels():
    reports = [
        sr.FileReport(path="shop/0002.py", findings=[
            sr.Finding(path="shop/0002.py", severity="Critical", description="null compare", check_name="TEXT-EQ-NULL"),
            sr.Finding(path="shop/0002.py", severity="Medium", description="select star", check_name="SELECT-STAR"),
        ]),
    ]
    log = sr.build_sarif(reports)
    assert log["version"] == "2.1.0"
    assert "sarif-2.1.0" in log["$schema"]
    run = log["runs"][0]
    assert run["tool"]["driver"]["name"] == "SIXTA"
    assert len(run["results"]) == 2
    levels = {r["ruleId"]: r["level"] for r in run["results"]}
    assert levels["sixta:TEXT-EQ-NULL"] == "error"    # Critical -> error
    assert levels["sixta:SELECT-STAR"] == "warning"   # Medium -> warning
    loc = run["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "shop/0002.py"
    assert loc["region"]["startLine"] >= 1


def test_build_sarif_dedups_rules():
    reports = [sr.FileReport(path="a.sql", findings=[
        sr.Finding(path="a.sql", severity="High", description="x1", check_name="R"),
        sr.Finding(path="a.sql", severity="High", description="x2", check_name="R"),
    ])]
    rules = sr.build_sarif(reports)["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["sixta:R"]


def test_write_github_summary_appends(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    sr.write_github_summary(sr.NOTE_MARKER + "\n# SIXTA report\nbody")
    assert "# SIXTA report" in summary.read_text()
    assert sr.NOTE_MARKER not in summary.read_text()  # marker stripped from the visible summary


# --------------------------------------------------------------------------
# PR comment upsert (stub GitHub REST server)
# --------------------------------------------------------------------------

class _GitHubStub(BaseHTTPRequestHandler):
    comments = []  # class-level state, reset per test
    calls = []

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send(200, _GitHubStub.comments)

    def do_POST(self):
        _GitHubStub.calls.append(("POST", self.path))
        self._send(201, {"id": 999})

    def do_PATCH(self):
        _GitHubStub.calls.append(("PATCH", self.path))
        self._send(200, {"id": 999})


@pytest.fixture
def github_server(monkeypatch):
    _GitHubStub.comments = []
    _GitHubStub.calls = []
    server = HTTPServer(("127.0.0.1", 0), _GitHubStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    monkeypatch.setenv("GITHUB_API_URL", f"http://{host}:{port}")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/5/merge")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    yield server
    server.shutdown()


def test_upsert_github_comment_posts_new(github_server):
    sr.upsert_github_comment(sr.NOTE_MARKER + "\nreport")
    assert _GitHubStub.calls == [("POST", "/repos/acme/app/issues/5/comments")]


def test_upsert_github_comment_updates_existing(github_server):
    _GitHubStub.comments = [{"id": 123, "body": sr.NOTE_MARKER + "\nold"}]
    sr.upsert_github_comment(sr.NOTE_MARKER + "\nnew")
    assert _GitHubStub.calls == [("PATCH", "/repos/acme/app/issues/comments/123")]


def test_upsert_github_comment_skips_without_token(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    sr.upsert_github_comment("x")  # must not raise
