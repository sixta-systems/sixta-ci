"""Tests for the POST /v1/outcome gate write-back (stub REST server, no network)."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler

import pytest

import sixta_review as sr
from conftest import json_reply, run_stub_server

OLD_ID = "a" * 64


def _cid(sql: str) -> str:
    """Deterministic change id, like the server's content hash: a re-run of
    unchanged SQL yields the same id, an edit yields a new one."""
    return hashlib.sha256(sql.encode()).hexdigest()


# --------------------------------------------------------------------------
# Stub server: /v1/analyze (keyed responses echo change_id) + /v1/outcome
# --------------------------------------------------------------------------

def _v1_response(request: dict, keyed: bool, clean: bool) -> dict:
    results = []
    worst = "Info"
    for i, ex in enumerate(request.get("extractions", [])):
        src = ex.get("source_file")
        sev = "Info" if clean else ("High" if ex["kind"] == "migration" else "Critical")
        worst = sev if sr.SEVERITY_RANK[sev] > sr.SEVERITY_RANK[worst] else worst
        res = {"index": i, "kind": ex["kind"], "source_file": src, "overall_severity": sev,
               "findings": [], "report_text": f"**SIXTA {ex['kind']} analysis**"}
        if keyed:
            res["change_id"] = _cid(ex["sql"])
        results.append(res)
    return {"engine": request.get("engine"), "results": results, "worst_severity": worst,
            "renders": {"markdown": "server markdown", "code_quality": []},
            "usage": {"calls_charged": len(results)}}


class StubOutcomeHandler(BaseHTTPRequestHandler):
    calls: list = []
    behavior: str = "ok"  # ok | clean | outcome_503 | outcome_not_recorded

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        request = json.loads(raw)
        cls = StubOutcomeHandler
        cls.calls.append({"request": request, "auth": self.headers.get("authorization"), "path": self.path})
        if self.path == "/v1/outcome":
            if cls.behavior == "outcome_503":
                return self._json(503, {"error": {"code": "record_unavailable", "message": "outcome store down"}})
            if cls.behavior == "outcome_not_recorded":
                return self._json(200, {"recorded": False})
            return self._json(200, {"recorded": True})
        if self.path != "/v1/analyze":
            return self._json(404, {"error": {"code": "not_found", "message": "not found"}})
        return self._json(200, _v1_response(request, keyed=bool(self.headers.get("authorization")),
                                            clean=cls.behavior == "clean"))

    _json = json_reply

    def log_message(self, *args):
        pass


@pytest.fixture
def stub(monkeypatch):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    yield from run_stub_server(StubOutcomeHandler)


def _outcome_calls():
    return [c for c in StubOutcomeHandler.calls if c["path"] == "/v1/outcome"]


def _main_args(url, tmp_path, *extra):
    return ["--api", "v1", "--sixta-url", url, "--gate", "high",
            "--report-md", str(tmp_path / "report.md"),
            "--code-quality", str(tmp_path / "cq.json"), *extra]


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://connect.sixta.ai/mcp", "https://connect.sixta.ai/v1/outcome"),
        ("https://connect.sixta.ai/v1/analyze", "https://connect.sixta.ai/v1/outcome"),
        ("https://example.test/", "https://example.test/v1/outcome"),
    ],
)
def test_v1_outcome_url(url, expected):
    assert sr.SixtaClient(url, api_key=None).v1_outcome_url == expected


def test_report_outcome_posts_bearer_and_body(stub):
    client = sr.SixtaClient(stub, api_key="sk-test")
    assert client.report_outcome(OLD_ID, "gate_failed", "High") is True
    (call,) = _outcome_calls()
    assert call["auth"] == "Bearer sk-test"
    assert call["request"] == {"change_id": OLD_ID, "kind": "gate_failed", "severity": "High"}


def test_report_outcome_omits_absent_severity_and_reads_recorded(stub):
    StubOutcomeHandler.behavior = "outcome_not_recorded"
    client = sr.SixtaClient(stub, api_key="sk-test")
    assert client.report_outcome(OLD_ID, "acted_upon") is False
    (call,) = _outcome_calls()
    assert call["request"] == {"change_id": OLD_ID, "kind": "acted_upon"}


# --------------------------------------------------------------------------
# Planner (pure)
# --------------------------------------------------------------------------

def _t(cid, file, sev):
    return {"id": cid, "file": file, "severity": sev}


def test_plan_outcomes_all_pass_on_passing_run():
    targets = [_t("1" * 64, "a.sql", "Medium"), _t("2" * 64, "b.sql", None)]
    events, state, _banked = sr.plan_outcomes(targets, [], sr.GATE_RANK["high"], run_failed=False)
    assert [e["kind"] for e in events] == ["gate_passed", "gate_passed"]
    assert state == []


def test_plan_outcomes_failing_run_splits_by_own_severity():
    """An innocuous change riding in a failing run is not smeared as gate_failed."""
    targets = [_t("1" * 64, "a.sql", "Critical"), _t("2" * 64, "b.sql", "Low")]
    events, state, _banked = sr.plan_outcomes(targets, [], sr.GATE_RANK["high"], run_failed=True)
    kinds = {e["change_id"]: e["kind"] for e in events}
    assert kinds == {"1" * 64: "gate_failed", "2" * 64: "gate_passed"}
    assert state == [_t("1" * 64, "a.sql", "Critical")]


def test_plan_outcomes_acted_upon_when_file_reanalyzed_clean():
    prev = [_t(OLD_ID, "a.sql", "High")]
    targets = [_t("3" * 64, "a.sql", "Info")]
    events, state, _banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=False)
    assert {"change_id": OLD_ID, "kind": "acted_upon", "severity": "High"} in events
    assert state == []


def test_plan_outcomes_no_acted_upon_while_file_still_fails():
    prev = [_t(OLD_ID, "a.sql", "High")]
    targets = [_t("3" * 64, "a.sql", "Critical")]
    events, state, _banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=True)
    assert [e["kind"] for e in events] == ["gate_failed"]  # new id fails; old id resolves later
    assert state == [_t("3" * 64, "a.sql", "Critical")]


def test_plan_outcomes_unchanged_failure_reports_gate_failed_not_acted_upon():
    prev = [_t(OLD_ID, "a.sql", "High")]
    targets = [_t(OLD_ID, "a.sql", "High")]  # same content hash: nothing changed
    events, state, _banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=True)
    assert [e["kind"] for e in events] == ["gate_failed"]
    assert state == [_t(OLD_ID, "a.sql", "High")]


def test_plan_outcomes_carries_prev_failures_for_unanalyzed_files():
    prev = [_t(OLD_ID, "gone.sql", "High")]
    targets = [_t("3" * 64, "a.sql", "Info")]
    events, state, _banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=False)
    assert [e["kind"] for e in events] == ["gate_passed"]  # no verdict on gone.sql: no event
    assert state == prev  # kept: a later run may still resolve it


# --------------------------------------------------------------------------
# Comment-embedded state
# --------------------------------------------------------------------------

def test_gate_state_roundtrip_survives_truncation_point():
    md = sr.NOTE_MARKER + "\n## SIXTA SQL review\n\nbody"
    failed = [_t(OLD_ID, "a.sql", "High")]
    embedded = sr.embed_gate_state(md, failed)
    # State rides directly under the upsert marker so body truncation keeps it.
    assert embedded.splitlines()[1].startswith(sr.STATE_MARKER)
    assert sr.parse_gate_state(embedded) == failed
    # An empty list still embeds: it must overwrite a previous run's state.
    assert sr.parse_gate_state(sr.embed_gate_state(md, [])) == []


def test_parse_gate_state_tolerates_missing_and_garbage():
    assert sr.parse_gate_state(None) == []
    assert sr.parse_gate_state("no marker here") == []
    assert sr.parse_gate_state(sr.STATE_MARKER + "{not json -->") == []
    assert sr.parse_gate_state(sr.STATE_MARKER + '{"v":1,"failed":"nope"} -->') == []


# --------------------------------------------------------------------------
# main() end-to-end
# --------------------------------------------------------------------------

def test_main_keyed_run_reports_gate_outcomes(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("SIXTA_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(_main_args(stub, tmp_path, str(sql)))
    assert rc == 1  # Critical query trips the high gate
    sent_sql = StubOutcomeHandler.calls[0]["request"]["extractions"][0]["sql"]
    (call,) = _outcome_calls()
    assert call["request"] == {"change_id": _cid(sent_sql), "kind": "gate_failed", "severity": "Critical"}
    # The failing id is remembered in the report the comment upsert carries.
    report = (tmp_path / "report.md").read_text()
    assert sr.parse_gate_state(report) == [
        {"id": call["request"]["change_id"], "file": str(sql), "severity": "Critical"}]


def test_main_acted_upon_after_fix(stub, tmp_path, monkeypatch):
    """Re-run of the same PR: the previously failing change's id is gone and the
    file now analyzes clean, so the old id is reported acted_upon."""
    StubOutcomeHandler.behavior = "clean"
    monkeypatch.setenv("SIXTA_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status IS NULL;")
    prev_body = sr.embed_gate_state(
        sr.NOTE_MARKER + "\nold report", [{"id": OLD_ID, "file": str(sql), "severity": "Critical"}])
    monkeypatch.setattr(sr, "previous_report_body", lambda platform: prev_body)

    rc = sr.main(_main_args(stub, tmp_path, str(sql)))
    assert rc == 0
    sent_sql = StubOutcomeHandler.calls[0]["request"]["extractions"][0]["sql"]
    by_kind = {c["request"]["kind"]: c["request"] for c in _outcome_calls()}
    assert by_kind["gate_passed"]["change_id"] == _cid(sent_sql)
    assert by_kind["acted_upon"] == {"change_id": OLD_ID, "kind": "acted_upon", "severity": "Critical"}
    assert sr.parse_gate_state((tmp_path / "report.md").read_text()) == []


def test_main_outcome_failure_never_gates(stub, tmp_path, monkeypatch):
    StubOutcomeHandler.behavior = "outcome_503"
    monkeypatch.setenv("SIXTA_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(_main_args(stub, tmp_path, str(sql)))
    assert rc == 1  # the gate verdict, not an error exit
    assert len(_outcome_calls()) == 1  # stops at the first failure, no send loop


def test_main_anonymous_run_sends_no_outcomes(stub, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(_main_args(stub, tmp_path, str(sql)))
    assert rc == 1
    assert _outcome_calls() == []
    assert sr.STATE_MARKER not in (tmp_path / "report.md").read_text()


def test_main_outcomes_opt_out(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("SIXTA_API_KEY", "sk-test")
    monkeypatch.setenv("SIXTA_OUTCOMES", "false")
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(_main_args(stub, tmp_path, str(sql)))
    assert rc == 1
    assert _outcome_calls() == []


def test_main_local_run_sends_no_outcomes(stub, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIXTA_API_KEY", "sk-test")
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "changes.sql"
    sql.write_text("UPDATE shop_order SET status='n' WHERE status = NULL;")

    rc = sr.main(["--api", "v1", "--sixta-url", stub, "--gate", "high", "--local", str(sql)])
    assert rc == 1
    assert _outcome_calls() == []


# --------------------------------------------------------------------------
# v0.8: override label + the Banked line
# --------------------------------------------------------------------------

def test_plan_outcomes_override_reports_overridden_not_gate_failed():
    targets = [_t("1" * 64, "a.sql", "Critical"), _t("2" * 64, "b.sql", "Low")]
    events, state, banked = sr.plan_outcomes(targets, [], sr.GATE_RANK["high"], run_failed=True, overridden=True)
    kinds = {e["change_id"]: e["kind"] for e in events}
    assert kinds == {"1" * 64: "overridden", "2" * 64: "gate_passed"}
    assert state == [_t("1" * 64, "a.sql", "Critical")]  # override still remembers the failure


def test_plan_outcomes_banks_prev_impact_on_acted_upon():
    prev = [{**_t(OLD_ID, "a.sql", "High"), "impact": 660_000}]
    targets = [_t("3" * 64, "a.sql", "Info")]
    events, state, banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=False)
    assert {"change_id": OLD_ID, "kind": "acted_upon", "severity": "High"} in events
    assert banked == [{"file": "a.sql", "impact": 660_000}]


def test_plan_outcomes_state_carries_the_failing_impact_bound():
    targets = [{**_t("1" * 64, "a.sql", "Critical"), "impact": 30_000}]
    _events, state, _banked = sr.plan_outcomes(targets, [], sr.GATE_RANK["high"], run_failed=True)
    assert state == [{**_t("1" * 64, "a.sql", "Critical"), "impact": 30_000}]


def test_plan_outcomes_numberless_save_banks_nothing():
    prev = [_t(OLD_ID, "a.sql", "High")]  # no impact bound on the failing run
    targets = [_t("3" * 64, "a.sql", "Info")]
    _events, _state, banked = sr.plan_outcomes(targets, prev, sr.GATE_RANK["high"], run_failed=False)
    assert banked == []


def test_banked_line_wording_and_units():
    assert sr.banked_line([]) is None
    assert sr.banked_line([{"file": "a.sql", "impact": 30_000}]) == \
        "Banked: ~30 seconds of lock time this table never took."
    assert sr.banked_line([{"file": "a.sql", "impact": 660_000}]) == \
        "Banked: ~11 minutes of lock time this table never took."
    assert sr.banked_line([{"file": "a.sql", "impact": 3_600_000}, {"file": "b.sql", "impact": 3_600_000}]) == \
        "Banked: ~2 hours of lock time these tables never took."


def _clear_label_env(monkeypatch):
    for var in ("GITLAB_CI", "CI_MERGE_REQUEST_LABELS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY",
                "GITHUB_TOKEN", "GITHUB_EVENT_PATH", "GITHUB_REF", "GITHUB_API_URL"):
        monkeypatch.delenv(var, raising=False)


def test_override_requested_gitlab_labels_env(monkeypatch):
    _clear_label_env(monkeypatch)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", "backend, SIXTA: Override ,urgent")
    assert sr.override_requested("gitlab") is True
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", "backend,urgent")
    assert sr.override_requested("gitlab") is False


def test_override_requested_github_live_lookup(monkeypatch):
    # The label is added AFTER the gate went red, so the frozen event payload
    # cannot carry it — the live issues API is the source of truth.
    _clear_label_env(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")
    seen = {}

    class _Resp:
        def __init__(self, body):
            self._b = json.dumps(body).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp([{"name": "sixta: override"}, {"name": "bug"}])

    monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen)
    assert sr.override_requested("github") is True
    assert seen["url"].startswith("https://api.github.com/repos/acme/app/issues/7/labels")

    def fake_urlopen_none(req, timeout=None):
        return _Resp([{"name": "bug"}])

    monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen_none)
    assert sr.override_requested("github") is False


def test_override_requested_fails_closed(monkeypatch):
    # Outside a runner, or when the API errors: no override (honest default).
    _clear_label_env(monkeypatch)
    assert sr.override_requested("github") is False
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/7/merge")

    def boom(req, timeout=None):
        raise OSError("api down")

    monkeypatch.setattr(sr.urllib.request, "urlopen", boom)
    assert sr.override_requested("github") is False
