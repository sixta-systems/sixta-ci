import json
import os
import sys
import threading
from http.server import HTTPServer

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
        "SIXTA_REQUIRE_ROLLBACK", "SIXTA_API_KEY",
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
