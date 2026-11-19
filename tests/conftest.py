import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolate_ci_env(monkeypatch):
    """The kit's own test suite runs inside GitHub Actions / GitLab CI, which
    inject env vars that drive platform auto-detection, the diff base, and the
    API default. Clear them so every test is deterministic regardless of where it
    runs; the platform tests set exactly what they need via monkeypatch."""
    for var in (
        "GITHUB_ACTIONS", "GITHUB_EVENT_PATH", "GITHUB_REF", "GITHUB_BASE_REF",
        "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY", "GITHUB_API_URL",
        "CI_MERGE_REQUEST_DIFF_BASE_SHA", "CI_API_V4_URL", "CI_PROJECT_ID",
        "CI_MERGE_REQUEST_IID", "SIXTA_BOT_TOKEN", "SIXTA_PLATFORM", "SIXTA_API",
        "SIXTA_REQUIRE_ROLLBACK", "SIXTA_ENGINE", "SIXTA_LIQUIBASE_CMD", "SIXTA_API_KEY",
        "SIXTA_INTAKE_URL", "SIXTA_INTAKE_TOKEN", "GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT",
        "GITHUB_SERVER_URL", "GITHUB_SHA", "CI_PIPELINE_IID", "CI_PROJECT_PATH",
        "CI_PROJECT_URL", "CI_COMMIT_SHA", "CI_MERGE_REQUEST_SOURCE_BRANCH_SHA",
        "SIXTA_CHANGE_URL", "SIXTA_CHANGE_TOKEN", "SIXTA_CHANGE_DATABASE", "SIXTA_CHANGE_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


def json_reply(handler, status, body, headers=None):
    """Shared stub-server response writer (used by both stub handlers)."""
    payload = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


def run_stub_server(handler_cls):
    """Shared stub-server lifecycle for fixtures (`yield from run_stub_server(H)`):
    reset class state, serve on an ephemeral port, yield the /mcp URL. The short
    poll_interval keeps shutdown() from idling 0.5s per test."""
    handler_cls.calls = []
    handler_cls.behavior = "ok"
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    finally:
        server.shutdown()


# --------------------------------------------------------------------------
# A local stand-in for the SIXTA backend's register route (POST
# /api/v1/changes). It answers the way web/api/changeregister.go in
# sixta-backend does for a database change: the bearer checked before the
# body is read, 404 with no token configured, an unknown field refused, the
# required fields and the bounds enforced, 201 for a new registration and
# 200 for a replay of the same key. It holds no SQL engine: what it checks is
# the request's shape.
# --------------------------------------------------------------------------

REGISTER_TOKEN = "stand-in-change-token-0123456789"
REGISTER_FIELDS = {
    "idempotency_key", "database", "plane", "subject", "before", "after", "statements", "parameters",
    "revision", "at", "started_at", "finished_at", "duration", "actor", "tool", "pull_request",
    "change_ids", "release",
}
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _ident(v) -> bool:
    return (isinstance(v, str) and 0 < len(v.strip()) <= 200 and "'" not in v and '"' not in v
            and not any(c != " " and (c.isspace() or ord(c) < 32) for c in v))


def register_verdict(body: dict):
    """(status, reply) for a database-change registration, as the route decides it."""
    def refuse(field, why):
        return 400, {"error": "invalid_registration", "field": field, "detail": why}
    unknown = set(body) - REGISTER_FIELDS
    if unknown:
        return 400, {"error": "bad_request", "detail": "the body carries a field it does not have"}
    if not _ident(body.get("idempotency_key")):
        return refuse("idempotency_key", "a registration carries a bounded, quote-free idempotency key")
    if not str(body.get("database") or "").strip():
        return refuse("database", "a registration names the database it changed")
    if body["database"] == "hidden-db":
        return 409, {"error": "hidden_database", "field": "database", "detail": "hidden-db is hidden from SIXTA's view"}
    if body["database"] not in ("orders-main", "billing"):
        return refuse("database", "not a database the estate holds")
    if body.get("plane") not in ("configuration", "instance", "schema", "statement", "deploy"):
        return refuse("plane", "the plane is configuration, instance, schema, statement or deploy")
    statements = body.get("statements") or []
    if statements:
        if body["plane"] not in ("schema", "statement"):
            return refuse("plane", "statements are on the schema or statement plane")
        if len(statements) > 20:
            return refuse("statements", "at most 20 statements")
        for st in statements:
            if not isinstance(st, str) or not st.strip() or len(st.encode()) > 64 << 10:
                return refuse("statements", "a statement is empty, too long, or not text")
    elif not _ident(body.get("subject")) and not (body["plane"] == "deploy" and body.get("revision")):
        return refuse("subject", "a registration names what changed")
    for name in ("subject", "revision", "actor", "tool", "pull_request"):
        if body.get(name) not in (None, "") and not _ident(body[name]):
            return refuse(name, "a bounded, quote-free value")
    if body.get("at") and (body.get("started_at") or body.get("finished_at")):
        return refuse("at", "a registration states at, or started_at with finished_at, not both")
    if body.get("finished_at") and not body.get("started_at"):
        return refuse("started_at", "finished_at is stated with started_at")
    for name in ("at", "started_at", "finished_at"):
        if body.get(name) and not _RFC3339.match(body[name]):
            return refuse(name, "an RFC 3339 instant")
    ids = body.get("change_ids") or []
    if len(ids) > 10 or any(not isinstance(i, int) or i <= 0 for i in ids):
        return refuse("change_ids", "a change id is a positive number, at most 10")
    n = len(statements) or 1
    return 201, {"changes": [{"id": i + 1} for i in range(n)], "duplicate": False,
                 "says": f"Recorded {n} change(s) on {body['database']}, posted by migrations."}


class StubRegisterHandler(BaseHTTPRequestHandler):
    calls: list = []           # every request that passed the bearer check
    received: list = []        # every body that arrived, whatever was answered
    seen_keys: set = set()
    behavior: str = "ok"       # ok | disabled | boom | slow | redirect | echo | echo_known | echo_ok | html
    redirect_to: str = ""

    def do_POST(self):
        cls = StubRegisterHandler
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        auth = self.headers.get("authorization") or ""
        cls.received.append(raw.decode("utf-8", "replace"))
        if self.path != "/api/v1/changes":
            return self._json(404, {"error": "not_found"})
        if cls.behavior == "disabled":
            return self._json(404, {"error": "intake_disabled", "detail": "SIXTA_CHANGE_TOKENS is not configured on this deployment, so changes cannot be registered"})
        if cls.behavior == "redirect":
            self.send_response(307)
            self.send_header("location", cls.redirect_to)
            self.send_header("content-length", "0")
            self.end_headers()
            return None
        # A misbehaving proxy quoting the whole request back, bearer and
        # statements included, in every part of its reply a client might log.
        try:
            statements = " ".join(json.loads(raw).get("statements") or [])
        except ValueError:
            statements = ""
        quoted = "refused " + statements + " " + raw.decode("utf-8", "replace") + " authorization=" + auth
        if cls.behavior == "echo":
            return self._json(502, {"error": quoted, "field": quoted, "detail": quoted, "message": quoted})
        if cls.behavior == "echo_known":  # the route's own error code, with the request quoted beside it
            return self._json(400, {"error": "invalid_registration", "field": quoted, "detail": quoted})
        if cls.behavior == "echo_ok":
            return self._json(200, {"changes": [{"id": 1, "says": quoted}], "duplicate": False, "says": quoted})
        if auth != "Bearer " + REGISTER_TOKEN:
            return self._json(401, {"error": "unauthorized"})
        if len(raw) > 256 << 10:
            return self._json(400, {"error": "bad_request", "detail": "the body exceeds the bound"})
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": "bad_request", "detail": "the body is not the registration shape"})
        cls.calls.append({"path": self.path, "auth": auth, "body": body, "at": time.time()})
        if cls.behavior == "boom":
            return self._json(500, {"error": "internal_error"})
        if cls.behavior == "slow":
            time.sleep(1.0)
        if cls.behavior == "html":
            payload = b"<html>signed out</html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None
        status, reply = register_verdict(body)
        if status == 201:
            if body["idempotency_key"] in cls.seen_keys:
                status, reply = 200, {"changes": reply["changes"], "duplicate": True,
                                      "says": "Already recorded under this idempotency key; nothing was written twice."}
            cls.seen_keys.add(body["idempotency_key"])
        return self._json(status, reply)

    _json = json_reply

    def log_message(self, *args):
        pass


def run_register_stub():
    """`yield from run_register_stub()`: the stand-in's base URL."""
    StubRegisterHandler.seen_keys = set()
    StubRegisterHandler.received = []
    StubRegisterHandler.redirect_to = ""
    for url in run_stub_server(StubRegisterHandler):
        yield url.rsplit("/mcp", 1)[0]
