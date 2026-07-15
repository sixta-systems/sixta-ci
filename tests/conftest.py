import os
import sys

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
        "SIXTA_REQUIRE_ROLLBACK", "SIXTA_ENGINE", "SIXTA_LIQUIBASE_CMD",
    ):
        monkeypatch.delenv(var, raising=False)
