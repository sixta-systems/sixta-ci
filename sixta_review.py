#!/usr/bin/env python3
"""sixta-review — SIXTA Connect SQL review for database migrations in CI.

Renders every migration changed in a merge/pull request to SQL (Django via
``manage.py sqlmigrate``, Alembic via offline ``alembic upgrade --sql``, Flyway
and other ``.sql`` migrations read directly with Flyway ``${placeholder}``
substitution), sends DDL to SIXTA's ``sixta_analyze_schema_change``
and DML to ``sixta_analyze_query``, and reports back three ways:

* ``sixta-report.md``            — the full SIXTA markdown reports
* ``gl-code-quality-report.json``— GitLab Code Quality entries (MR diff badges)
* an upserted MR note            — when SIXTA_BOT_TOKEN is configured

Exit code gates the pipeline: non-zero when any finding meets the ``--gate``
severity. Connectivity failures follow ``--fail-mode`` (open|closed).

Also runs as a local pre-commit hook: ``sixta-review --local [files...]``.

Wire protocol (two modes, selected by ``--api`` / ``SIXTA_API``):

* ``mcp`` (default) — one bare JSON-RPC ``tools/call`` POST to ``/mcp`` per
  statement group. Verdicts are read from the response's ``structuredContent``
  (the CI contract), with a text-parse fallback for older servers.
* ``v1`` — one batch ``POST /v1/analyze`` per pipeline run: every migration/SQL
  file's statements go in a single request with a shared schema context (from
  ``pg_dump`` when available), and the server returns per-extraction verdicts
  plus ready-made GitLab code-quality JSON. Falls back to building code-quality
  locally when the server does not render it (older ``/v1``). v1 mode also runs
  the rollback audit: each migration extraction carries the framework's own
  reverse render (or ``missing``/``irreversible``) for server-side analysis;
  ``--require-rollback`` makes the no-rollback finding gate-able.

Stdlib only. Python >= 3.9.
"""

from __future__ import annotations

import argparse
import ast
import email.utils
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NoReturn, Optional

__version__ = "0.4.3"

DEFAULT_SIXTA_URL = "https://connect.sixta.ai/mcp"
REPORT_MD = "sixta-report.md"

# Table-hint keys the /v1 batch API understands (a subset of .sixta.yml).
V1_HINT_KEYS = ("size_bytes", "row_estimate", "has_foreign_keys", "has_triggers",
                "has_fulltext_index", "row_format_compressed", "index_count")
CODE_QUALITY_JSON = "gl-code-quality-report.json"
SARIF_JSON = "sixta.sarif"
NOTE_MARKER = "<!-- sixta-review-report -->"

SEVERITY_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Info": 0}
GATE_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": None}
# SIXTA display severity -> GitLab Code Quality severity.
CQ_SEVERITY = {"Critical": "blocker", "High": "critical", "Medium": "major", "Low": "minor", "Info": "info"}
# SIXTA display severity -> SARIF level (GitHub code scanning: error/warning/note).
SARIF_LEVEL = {"Critical": "error", "High": "error", "Medium": "warning", "Low": "note", "Info": "note"}

DDL_KEYWORDS = ("CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE", "COMMENT")
DML_KEYWORDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "MERGE", "REPLACE")
SKIP_KEYWORDS = ("BEGIN", "COMMIT", "ROLLBACK", "SET", "SAVEPOINT", "RELEASE", "START", "USE", "LOCK", "UNLOCK", "PRAGMA")


# --------------------------------------------------------------------------
# SQL text utilities (pure)
# --------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split SQL text into statements on ``;`` — aware of single/double quotes,
    line comments, block comments, MySQL backticks and PostgreSQL
    dollar-quoting ($tag$ ... $tag$), so a function body or a quoted literal
    never splits a statement."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    state: Optional[str] = None  # None | "'" | '"' | '`' | '--' | '/*' | '$tag$'
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "-" and nxt == "-":
                state = "--"
            elif ch == "/" and nxt == "*":
                state = "/*"
            elif ch in ("'", '"', "`"):
                state = ch
            elif ch == "$":
                m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
                if m:
                    state = m.group(0)
                    buf.append(m.group(0))
                    i += len(m.group(0))
                    continue
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
                i += 1
                continue
        elif state == "--":
            if ch == "\n":
                state = None
        elif state == "/*":
            if ch == "*" and nxt == "/":
                state = None
                buf.append("*/")
                i += 2
                continue
        elif state in ("'", '"', "`"):
            if ch == state:
                if state == "'" and nxt == "'":  # escaped quote ''
                    buf.append("''")
                    i += 2
                    continue
                state = None
        else:  # dollar-quote tag
            if sql.startswith(state, i):
                buf.append(state)
                i += len(state)
                state = None
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _first_keyword(stmt: str) -> str:
    """First SQL keyword, skipping leading comments and parentheses."""
    s = re.sub(r"--[^\n]*", " ", stmt)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    m = re.search(r"[A-Za-z]+", s)
    return m.group(0).upper() if m else ""


def classify_statement(stmt: str) -> str:
    """'ddl' | 'dml' | 'skip' | 'other'."""
    kw = _first_keyword(stmt)
    if kw in SKIP_KEYWORDS:
        return "skip"
    if kw in DDL_KEYWORDS:
        return "ddl"
    if kw in DML_KEYWORDS:
        return "dml"
    return "other"


_TABLE_PATTERNS = [
    re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"`]+)", re.I),
    re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[\w.\"`]+\s+ON\s+(?:ONLY\s+)?([\w.\"`]+)", re.I),
    re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"`]+)", re.I),
    re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`]+)", re.I),
    re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?([\w.\"`]+)", re.I),
]


def extract_table(stmt: str) -> Optional[str]:
    for pat in _TABLE_PATTERNS:
        m = pat.search(stmt)
        if m:
            return m.group(1).split(".")[-1].strip('"`').lower()
    return None


# --------------------------------------------------------------------------
# Config / hints
# --------------------------------------------------------------------------

def load_table_hints(root: str = ".") -> dict:
    """Load per-table hints from .sixta.yml (PyYAML if available; JSON content
    also accepted) or .sixta.json. Shape:
    ``tables: {orders: {size_bytes: 5000000000, has_foreign_keys: true}}``"""
    for name in (".sixta.yml", ".sixta.yaml", ".sixta.json"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        data = None
        try:
            data = json.loads(text)
        except ValueError:
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(text)
            except ImportError:
                warn(f"{name} found but PyYAML is not installed and the file is not JSON — hints ignored")
                return {}
        if isinstance(data, dict):
            tables = data.get("tables") or {}
            return {str(k).lower(): v for k, v in tables.items() if isinstance(v, dict)}
    return {}


# --------------------------------------------------------------------------
# Findings model
# --------------------------------------------------------------------------

@dataclass
class Finding:
    path: str
    severity: str  # Critical/High/Medium/Low/Info
    description: str
    check_name: str = "sixta"

    def code_quality(self) -> dict:
        return {
            "description": self.description,
            "check_name": self.check_name,
            "fingerprint": hashlib.sha1(f"{self.path}:{self.check_name}:{self.description}".encode()).hexdigest(),
            "severity": CQ_SEVERITY.get(self.severity, "info"),
            "location": {"path": self.path, "lines": {"begin": 1}},
        }


@dataclass
class FileReport:
    path: str
    sections: list[str] = field(default_factory=list)  # SIXTA report texts / notes
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # analyses skipped (fail-open)


def worst_rank(reports: list[FileReport]) -> int:
    worst = -1
    for r in reports:
        for f in r.findings:
            worst = max(worst, SEVERITY_RANK.get(f.severity, 0))
    return worst


# --------------------------------------------------------------------------
# SIXTA client (bare JSON-RPC tools/call — the stateless CI contract)
# --------------------------------------------------------------------------

class SixtaConnectivityError(Exception):
    pass


class SixtaToolError(Exception):
    pass


class SixtaAuthError(Exception):
    """A configured API key was rejected (HTTP 401/403 with a Bearer header
    sent). A persistent misconfiguration, not a transient outage — deliberately
    NOT a SixtaToolError subclass so no fail-open catch site can swallow it;
    main() decides once (CI gates, local pre-commit warns)."""


_AUTH_HINT = " — authentication failed; check SIXTA_API_KEY"
_AUTH_MAYBE_HINT = " — if this is an authentication problem, check SIXTA_API_KEY"

RETRY_AFTER_DEFAULT_S = 30
RETRY_AFTER_CAP_S = 120


def _redact_url(url: str) -> str:
    """Scheme+host+path only — error messages land verbatim in PR/MR comments,
    so credentials or routing tokens in the configured URL must not leak."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal — hostname strips the brackets urlsplit needs
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _http_error_message(exc: urllib.error.HTTPError, url: str) -> str:
    """Human-readable message for an HTTP error response. The JSON error body
    is best-effort context only — no body shape or read failure may mask the
    status code."""
    detail = ""
    try:
        err_body = json.loads(exc.read().decode())
        err = err_body.get("error") if isinstance(err_body, dict) else None
        if isinstance(err, dict):
            raw = err.get("message")
            detail = "" if raw is None else str(raw)
        elif isinstance(err, str):
            detail = err
    except Exception:  # e.g. IncompleteRead, non-JSON body, wrong charset
        detail = ""
    msg = f"HTTP {exc.code} from {_redact_url(url)}"
    if detail:
        msg += f": {detail}"
    return msg


def _raise_http_error(exc: urllib.error.HTTPError, url: str, authenticated: bool) -> NoReturn:
    """Classify an HTTP error response. 401/403 with a configured key is an
    auth misconfiguration (gates in CI regardless of --fail-mode); anonymous
    401/403 is a tool error so fail-open still applies (fork PRs run without
    secrets). 5xx and post-retry 429 are transient — the "SIXTA unreachable"
    class the fail-mode docs describe. Any other 4xx is a tool error."""
    msg = _http_error_message(exc, url)
    if exc.code in (401, 403):
        if authenticated:
            raise SixtaAuthError(msg + _AUTH_HINT) from exc
        raise SixtaToolError(msg + " — anonymous call rejected; set SIXTA_API_KEY") from exc
    if exc.code == 429:
        raise SixtaConnectivityError(f"rate limited, retries exhausted: {msg}") from exc
    if exc.code < 400 or exc.code >= 500:
        # < 400: an unfollowed redirect (urllib refuses to re-POST a body on
        # 3xx) — the service never handled the request, same as 5xx.
        raise SixtaConnectivityError(msg) from exc
    raise SixtaToolError(msg) from exc


def _looks_like_auth_error(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in ("unauthorized", "forbidden", "api key", "authentication"))


def _is_auth_rejection(text: str) -> bool:
    """Unambiguous auth-rejection wording, safe to gate on (unlike the broad
    _looks_like_auth_error, which only decorates): a quota nudge mentioning
    "API key" or a "forbidden by policy" verdict must not match."""
    t = text.lower()
    return any(marker in t for marker in ("unauthorized", "invalid api key", "api key expired",
                                          "expired api key", "invalid credentials"))


def _hint_if_auth(msg: str) -> str:
    """Append a *conditional* SIXTA_API_KEY hint to auth-flavored error text.
    The broad heuristic only decorates — it never upgrades an error to
    SixtaAuthError, and the wording stays tentative because messages like
    "forbidden by policy" match without being auth failures."""
    return msg + _AUTH_MAYBE_HINT if _looks_like_auth_error(msg) else msg


def _retry_after_header_seconds(value: Optional[str], default: int = RETRY_AFTER_DEFAULT_S) -> int:
    """Parse a Retry-After header: delta-seconds or HTTP-date (RFC 9110)."""
    if not value:
        return default
    try:
        n = int(value)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            n = int((dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return default
    return min(max(n, 0), RETRY_AFTER_CAP_S)


def v1_endpoint(mcp_url: str) -> str:
    """Derive the /v1/analyze URL from a configured /mcp URL."""
    if mcp_url.endswith("/mcp"):
        return mcp_url[:-len("/mcp")] + "/v1/analyze"
    if mcp_url.endswith("/v1/analyze"):
        return mcp_url
    return mcp_url.rstrip("/") + "/v1/analyze"


class SixtaClient:
    def __init__(self, url: str, api_key: Optional[str], timeout: int = 60, max_retries: int = 3):
        self.url = url
        self.v1_url = v1_endpoint(url)
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0

    def analyze_v1(self, request: dict) -> dict:
        """POST a batch to /v1/analyze and return the parsed JSON response.

        The route always answers 200 for a well-formed batch (per-extraction
        rate-limit/errors ride inside ``results``). HTTP 429 from a fronting
        proxy is retried with bounded backoff, like the mcp path; every other
        HTTP error status is classified by ``_raise_http_error``, and pure
        transport failures become a SixtaConnectivityError."""
        payload = json.dumps(request).encode()
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(self.v1_url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:  # subclass of URLError — catch first
                if self._backoff_429(exc, "/v1/analyze", attempt):
                    continue
                _raise_http_error(exc, self.v1_url, authenticated=bool(self.api_key))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                raise SixtaConnectivityError(f"POST {_redact_url(self.v1_url)} failed: {exc}") from exc

    def _backoff_429(self, exc: urllib.error.HTTPError, label: str, attempt: int) -> bool:
        """Bounded backoff for an HTTP 429; True means the caller should retry."""
        if exc.code != 429 or attempt > self.max_retries:
            return False
        wait = _retry_after_header_seconds(exc.headers.get("retry-after"))
        exc.close()  # release the socket before sleeping
        warn(f"rate limited (HTTP 429) on {label}; backing off {wait}s (attempt {attempt}/{self.max_retries})")
        time.sleep(wait)
        return True

    def call(self, tool: str, arguments: dict) -> dict:
        """Return the MCP CallToolResult ({content, structuredContent?, isError?})."""
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        ).encode()
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(self.url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:  # subclass of URLError — catch first
                if self._backoff_429(exc, tool, attempt):
                    continue
                _raise_http_error(exc, self.url, authenticated=bool(self.api_key))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                raise SixtaConnectivityError(f"POST {_redact_url(self.url)} failed: {exc}") from exc

            if "error" in body:  # JSON-RPC level error (auth, body cap, rate limit, ...)
                err = body["error"]
                raw = err.get("message") if isinstance(err, dict) else None
                msg = raw if isinstance(raw, str) and raw else str(err)
                if "rate limit" in msg.lower() and attempt <= self.max_retries:
                    wait = _retry_after_seconds(msg)
                    warn(f"rate limited on {tool}; backing off {wait}s (attempt {attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                if self.api_key and _is_auth_rejection(msg):
                    raise SixtaAuthError(msg + _AUTH_HINT)
                raise SixtaToolError(_hint_if_auth(msg))
            result = body.get("result") or {}
            if result.get("isError"):
                text = _result_text(result)
                if "rate limit" in text.lower() and attempt <= self.max_retries:
                    wait = _retry_after_seconds(text)
                    warn(f"rate limited on {tool}; backing off {wait}s (attempt {attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                if self.api_key and _is_auth_rejection(text):
                    raise SixtaAuthError(text + _AUTH_HINT)
                raise SixtaToolError(_hint_if_auth(text))
            return result


def _result_text(result: dict) -> str:
    for block in result.get("content") or []:
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


def _retry_after_seconds(text: str, default: int = RETRY_AFTER_DEFAULT_S) -> int:
    m = re.search(r"wait about (\d+)s", text)
    return min(int(m.group(1)) if m else default, RETRY_AFTER_CAP_S)


# --------------------------------------------------------------------------
# Verdict extraction (structuredContent first, text fallback)
# --------------------------------------------------------------------------

def findings_from_result(result: dict, path: str) -> list[Finding]:
    struct = result.get("structuredContent")
    if isinstance(struct, dict):
        return _findings_from_structured(struct, path)
    return _findings_from_text(_result_text(result), path)


def _findings_from_structured(struct: dict, path: str) -> list[Finding]:
    out: list[Finding] = []
    for s in struct.get("statements") or []:  # schema-change shape
        sev = s.get("severity") or "Info"
        desc = f"SIXTA: {s.get('operation', 'DDL')}" + (f" on {s['table']}" if s.get("table") else "")
        desc += f" — risk {s.get('risk', sev)}, lock: {s.get('lock_type', '?')}"
        if s.get("blocks_writes"):
            desc += " (blocks writes)"
        if s.get("has_safe_alternative"):
            desc += ". A safe execution strategy is in the report."
        out.append(Finding(path=path, severity=sev, description=desc, check_name="sixta_analyze_schema_change"))
    for f in struct.get("findings") or []:  # query-analysis shape
        out.append(
            Finding(
                path=path,
                severity=f.get("severity") or "Info",
                description=f"SIXTA: {f.get('title', f.get('rule_id', 'finding'))}",
                check_name=str(f.get("rule_id", "sixta_analyze_query")),
            )
        )
    if not out and struct.get("verdict") == "clean":
        return []
    return out


_TEXT_OVERALL = re.compile(r"overall (?:risk|severity):? (Critical|High|Medium|Low|Info)")
_TEXT_FINDING = re.compile(r"^\s*(?:\d+\.\s+)?\*\*(.+?)\*\* — (Critical|High|Medium|Low|Info)\s*$", re.M)


def _findings_from_text(text: str, path: str) -> list[Finding]:
    out = [
        Finding(path=path, severity=sev, description=f"SIXTA: {title}")
        for title, sev in _TEXT_FINDING.findall(text)
    ]
    if not out:
        m = _TEXT_OVERALL.search(text)
        if m:
            out.append(Finding(path=path, severity=m.group(1), description="SIXTA: see report for details"))
    return out


# --------------------------------------------------------------------------
# Django migration discovery + rendering
# --------------------------------------------------------------------------

MIGRATION_RE = re.compile(r"(?:^|/)(?P<app>[^/]+)/migrations/(?P<name>(?!__init__)[^/]+)\.py$")


def migration_target(path: str) -> Optional[tuple[str, str]]:
    m = MIGRATION_RE.search(path.replace(os.sep, "/"))
    return (m.group("app"), m.group("name")) if m else None


def changed_files(base_sha: Optional[str], local: bool, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    if local:
        staged = _git("diff", "--cached", "--name-only", "--diff-filter=AM")
        untracked = _git("ls-files", "--others", "--exclude-standard")
        files = staged + untracked
    else:
        if not base_sha:
            die("no diff base: set CI_MERGE_REQUEST_DIFF_BASE_SHA (run in a merge-request pipeline) or pass --base-sha")
        files = _git("diff", "--name-only", "--diff-filter=AM", f"{base_sha}...HEAD")
    return [f for f in files if is_migration_file(f)]


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def render_migration(manage_py: str, app: str, name: str) -> str:
    """``manage.py sqlmigrate`` — requires a live database connection (Django
    uses it to resolve constraint names), hence the service container in CI."""
    proc = subprocess.run(
        [sys.executable, manage_py, "sqlmigrate", app, name],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sqlmigrate {app} {name} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def has_runpython(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(re.search(r"\bRunPython\b", fh.read()))
    except OSError:
        return False


# --------------------------------------------------------------------------
# Alembic (SQLAlchemy) — offline SQL render (docs/framework-support.md, mechanism A)
# --------------------------------------------------------------------------

# A revision file under alembic/versions/ or migrations/versions/ (not __init__).
ALEMBIC_RE = re.compile(r"(?:^|/)(?:alembic|migrations)/versions/(?!__init__)[^/]+\.py$")
_ALEMBIC_REVISION_RE = re.compile(r"^\s*revision(?::[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_ALEMBIC_DOWN_RE = re.compile(r"^\s*down_revision(?::[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|(None))", re.M)
# Data-migration smells that don't render to analyzable DDL offline.
_ALEMBIC_DATA_RE = re.compile(r"\bop\.bulk_insert\s*\(|\bop\.get_bind\s*\(|\bbind\.execute\s*\(", re.I)


def alembic_target(path: str) -> bool:
    return bool(ALEMBIC_RE.search(path.replace(os.sep, "/")))


def _alembic_revisions(path: str) -> tuple[str, str]:
    """(revision, down_revision) from a migration file; down='base' when None.
    Raises for merge/branched migrations (tuple down_revision) — not supported yet."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rev = _ALEMBIC_REVISION_RE.search(text)
    if not rev:
        raise RuntimeError(f"{os.path.basename(path)}: no Alembic 'revision' identifier found")
    down = _ALEMBIC_DOWN_RE.search(text)
    if down is None:
        raise RuntimeError(f"{os.path.basename(path)}: could not parse down_revision (merge/branched migrations aren't supported yet)")
    return rev.group(1), (down.group(1) or "base")


def render_alembic(path: str, opts: argparse.Namespace) -> str:
    """``alembic upgrade <down>:<rev> --sql`` — offline, no database connection.
    Renders exactly this revision's SQL, using the project's alembic config for
    the target dialect."""
    rev, down = _alembic_revisions(path)
    config = getattr(opts, "alembic_config", None) or "alembic.ini"
    proc = subprocess.run(
        ["alembic", "-c", config, "upgrade", f"{down}:{rev}", "--sql"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic offline render for {os.path.basename(path)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def alembic_data_ops(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(_ALEMBIC_DATA_RE.search(fh.read()))
    except OSError:
        return False


# --------------------------------------------------------------------------
# Spring Boot / Flyway (docs/spring-boot-support.md, Phase S1)
# --------------------------------------------------------------------------

# Repeatable migration basename (R__views.sql) and Java-based migration path
# (src/main/java/**/db/migration/V3__Backfill.java — renders no SQL offline).
_FLYWAY_REPEATABLE_RE = re.compile(r"^R__.+\.sql$")
_FLYWAY_JAVA_RE = re.compile(r"(?:^|/)db/migration/(?:[^/]+/)*[VRU][^/]*__[^/]*\.java$")
# Flyway placeholder token; names may carry dots/colons (${flyway:defaultSchema}).
_FLYWAY_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z0-9_.:\-]+)\}")

_SPRING_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_SPRING_CONFIG_DIRS = (".", "src/main/resources", "src/main/resources/config", "config")
# Engine markers in build/config text, strongest first: the Flyway 10+ per-DB
# module, then the JDBC driver artifact, then the datasource URL. A hit on both
# engines is ambiguous and detection abstains (with a warning).
_ENGINE_MARKERS = (
    ("postgresql", re.compile(r"flyway-database-postgresql|org\.postgresql|jdbc:postgresql|postgresql://", re.I)),
    ("mysql", re.compile(r"flyway-mysql|mysql-connector|com\.mysql|jdbc:(?:mysql|mariadb)|mysql://", re.I)),
)


def flyway_java_target(path: str) -> bool:
    return bool(_FLYWAY_JAVA_RE.search(path.replace(os.sep, "/")))


def _is_flyway_sql(basename: str) -> bool:
    return bool(_FLYWAY_VERSIONED_RE.match(basename) or _FLYWAY_REPEATABLE_RE.match(basename)
                or _FLYWAY_UNDO_RE.match(basename))


def _spring_files(root: str = ".") -> list[tuple[str, str]]:
    """(path, text) for the build + Spring config files that reveal the engine
    and Flyway placeholder values. Best-effort: unreadable or oversized files
    are skipped."""
    paths = [os.path.join(root, n) for n in _SPRING_BUILD_FILES]
    for d in _SPRING_CONFIG_DIRS:
        base = os.path.join(root, d)
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        paths += [os.path.join(base, n) for n in entries
                  if n.startswith("application") and n.endswith((".properties", ".yml", ".yaml"))]
    out: list[tuple[str, str]] = []
    for path in paths:
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > 512_000:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                out.append((path, fh.read()))
        except OSError:
            continue
    return out


def detect_engine(root: str = ".") -> Optional[str]:
    """Best-effort engine detection for JVM/Spring repos, used when
    --engine/SIXTA_ENGINE is ``auto`` or unset. Returns None when no marker
    matched, or when both engines matched (ambiguous)."""
    matched = {engine for _, text in _spring_files(root)
               for engine, pat in _ENGINE_MARKERS if pat.search(text)}
    if len(matched) > 1:
        warn("both postgresql and mysql markers found in build/config files; set --engine / SIXTA_ENGINE explicitly")
        return None
    return matched.pop() if matched else None


def resolve_engine(explicit: Optional[str]) -> str:
    """An explicitly configured engine wins; ``auto``/unset tries
    :func:`detect_engine` and falls back to postgresql (the historical
    default), saying which happened."""
    if explicit and explicit != "auto":
        return explicit
    detected = detect_engine()
    if detected:
        info(f"engine auto-detected: {detected} (set --engine / SIXTA_ENGINE to override)")
    elif _spring_files():
        info("engine not configured and not detectable from build/config files; assuming postgresql")
    return detected or "postgresql"


# JPA schema management without a migration tool (Phase S4): with
# ddl-auto=create/update, Hibernate mutates the schema at boot, so schema
# changes never show up in a PR as SQL and CI review is blind to them.
_DDL_AUTO_PROPS_RE = re.compile(
    r"^\s*(?:spring\.jpa\.hibernate\.ddl-auto|spring\.jpa\.properties\.hibernate\.hbm2ddl\.auto|hibernate\.hbm2ddl\.auto)"
    r"\s*[=:]\s*[\"']?(create-drop|create|update)[\"']?\s*$", re.M)
_DDL_AUTO_YML_RE = re.compile(r"^\s*ddl-auto\s*:\s*[\"']?(create-drop|create|update)[\"']?\s*$", re.M)
_MIGRATION_TOOL_RE = re.compile(r"flyway|liquibase", re.I)

_DDL_AUTO_SECTION = ("_Hibernate `ddl-auto` manages this repo's schema with no migration tool: schema changes "
                     "never appear in a PR as SQL, so they cannot be reviewed here. Emit a DDL script "
                     "(`spring.jpa.properties.jakarta.persistence.schema-generation.scripts.action=create`) "
                     "or adopt Flyway/Liquibase._")


def jpa_ddl_auto_unmanaged(root: str = ".") -> Optional[tuple[str, str]]:
    """(config path, ddl-auto value) when Hibernate generates the schema
    (create/update/create-drop) and no migration tool appears anywhere in the
    build/config files. None when a migration tool is present (mixed setups
    manage their schema properly) or ddl-auto is validate/none/absent."""
    hit: Optional[tuple[str, str]] = None
    for path, text in _spring_files(root):
        if _MIGRATION_TOOL_RE.search(text):
            return None
        if hit is None:
            m = _DDL_AUTO_PROPS_RE.search(text) if path.endswith(".properties") else _DDL_AUTO_YML_RE.search(text)
            if m:
                hit = (path, m.group(1))
    return hit


def append_ddl_auto_note(reports: list["FileReport"], root: str = ".") -> None:
    """One informational finding per run when the repo's schema is Hibernate-
    managed and invisible to review. Logged always; attached to the report only
    when the run analyzed something (never the sole content of a PR comment)."""
    hit = jpa_ddl_auto_unmanaged(root)
    if not hit:
        return
    path, value = hit
    desc = (f"SIXTA: spring.jpa.hibernate.ddl-auto={value} with no migration tool. Hibernate mutates the "
            f"schema at boot; schema changes never appear in a PR and were NOT reviewed. Emit a DDL script "
            f"(jakarta.persistence.schema-generation.scripts.action=create) or adopt Flyway/Liquibase.")
    info(desc[len("SIXTA: "):])
    if not reports:
        return
    rep = FileReport(path=os.path.normpath(path))
    rep.findings.append(Finding(path=os.path.normpath(path), severity="Info", description=desc,
                                check_name="jpa-ddl-auto-unmanaged"))
    rep.sections.append(_DDL_AUTO_SECTION)
    reports.append(rep)


def flyway_placeholders(root: str = ".") -> dict[str, str]:
    """Configured placeholder values (``spring.flyway.placeholders.<name>``)
    from Spring config: .properties parsed directly, .yml/.yaml via PyYAML when
    installed. Unconfigured placeholders fall back to their own name at
    substitution time, so missing config is fine."""
    values: dict[str, str] = {}
    for path, text in _spring_files(root):
        if path.endswith(".properties"):
            for m in re.finditer(r"^\s*spring\.flyway\.placeholders\.([A-Za-z0-9_.\-]+)\s*[=:]\s*(.*?)\s*$", text, re.M):
                values[m.group(1)] = m.group(2)
        else:
            try:
                import yaml  # type: ignore
            except ImportError:
                continue
            try:
                docs = list(yaml.safe_load_all(text))
            except yaml.YAMLError:
                continue
            for doc in docs:
                node = doc
                for key in ("spring", "flyway", "placeholders"):
                    node = node.get(key) if isinstance(node, dict) else None
                if isinstance(node, dict):
                    values.update({str(k): str(v) for k, v in node.items()})
    return values


def substitute_flyway_placeholders(sql: str, values: dict) -> str:
    """Replace Flyway ``${placeholder}`` tokens before analysis: the configured
    value when known, else the placeholder's own name as a bare token (valid in
    both identifier and string-literal positions). Flyway substitutes these
    everywhere in the script, so this mirrors what actually executes."""
    def _repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in values:
            return str(values[name])
        return re.sub(r"[^A-Za-z0-9_]", "_", name)
    return _FLYWAY_PLACEHOLDER_RE.sub(_repl, sql)


# --------------------------------------------------------------------------
# Liquibase (docs/spring-boot-support.md, Phase S2)
#
# Two changelog families, two mechanisms:
# * formatted SQL (--liquibase formatted sql) — parsed statically: changesets
#   split on `--changeset author:id`, inline `--rollback` feeds the rollback
#   audit, `--property` values substitute ${...}. Only changesets that are new
#   or edited vs the diff base are analyzed (changelogs are append-mostly).
# * XML/YAML/JSON — rendered to SQL via the Liquibase CLI in offline mode
#   (`--url=offline:<engine>`, no database). Offline connections persist
#   executed changesets to a state CSV (./databasechangelog.csv by default!),
#   so every invocation here points changeLogFile= at a throwaway temp CSV.
#   Rollback = changelog-sync (mark all executed in the temp state) followed
#   by rollback-count-sql with an overshoot count (Liquibase clamps it).
#   A changelog that only includes other changelogs (the master/index) is not
#   rendered: its leaves are analyzed when they themselves change.
# --------------------------------------------------------------------------

_LIQUIBASE_FORMATTED_RE = re.compile(r"^﻿?\s*--\s*liquibase\s+formatted\s+sql", re.I)
_LIQUIBASE_CHANGESET_LINE = re.compile(r"^\s*--\s*changeset\s+([^:\s]+):(\S+)", re.I)
_LIQUIBASE_ROLLBACK_LINE = re.compile(r"^\s*--\s*rollback(?:\s+(.*))?$", re.I)
_LIQUIBASE_PROPERTY_LINE = re.compile(
    r"^\s*--\s*property\s+name\s*=\s*\"?([\w.\-]+)\"?\s+value\s*=\s*\"?([^\"\n]*?)\"?\s*$", re.I)
_LIQUIBASE_META_LINE = re.compile(
    r"^\s*--\s*(?:precondition|comment|validCheckSum|ignoreLines|labels|logicalFilePath|liquibase)", re.I)

_LIQUIBASE_STRUCTURED_EXTS = (".xml", ".yml", ".yaml", ".json")
_LIQUIBASE_STRUCTURED_HINT_RE = re.compile(r"<databaseChangeLog\b|^\s*[\"']?databaseChangeLog[\"']?\s*:", re.M)
_LIQUIBASE_INCLUDE_RE = re.compile(r"<include(?:All)?\b|^\s*-?\s*[\"']?include(?:All)?[\"']?\s*:", re.M)
_LIQUIBASE_BOOKKEEPING_RE = re.compile(r"\bDATABASECHANGELOG(?:LOCK)?\b", re.I)

_LIQUIBASE_INDEX_DESC = ("SIXTA: changelog index (include/includeAll) changed. Included changelogs are "
                         "analyzed when they themselves change; nothing was rendered for the index itself.")
_LIQUIBASE_INDEX_SECTION = "_Changelog index changed: included changelogs are analyzed when they change._"


@dataclass
class LiquibaseChangeset:
    author: str
    cid: str
    sql: str
    line: int
    has_rollback: bool = False
    rollback_sql: str = ""


def _read_small(path: str, cap: int = 1_000_000) -> Optional[str]:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > cap:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def is_liquibase_formatted(text: str) -> bool:
    return bool(_LIQUIBASE_FORMATTED_RE.match(text or ""))


def liquibase_structured_target(path: str) -> bool:
    if not path.endswith(_LIQUIBASE_STRUCTURED_EXTS):
        return False
    text = _read_small(path)
    return bool(text and _LIQUIBASE_STRUCTURED_HINT_RE.search(text))


def parse_formatted_changelog(text: str) -> list[LiquibaseChangeset]:
    """Changesets from a formatted-SQL changelog, with ``--property`` values
    substituted into the SQL and inline ``--rollback`` lines collected.
    A bare ``--rollback`` (or ``--rollback empty``) counts as a declared
    empty rollback: has_rollback is true, rollback_sql stays empty."""
    props: dict[str, str] = {}
    changesets: list[LiquibaseChangeset] = []
    current: Optional[LiquibaseChangeset] = None
    sql_lines: list[str] = []
    rollback_lines: list[str] = []

    def _close() -> None:
        if current is None:
            return
        current.sql = substitute_flyway_placeholders("\n".join(sql_lines).strip(), props)
        current.rollback_sql = substitute_flyway_placeholders("\n".join(rollback_lines).strip(), props)
        changesets.append(current)

    for lineno, line in enumerate(text.splitlines(), 1):
        m = _LIQUIBASE_CHANGESET_LINE.match(line)
        if m:
            _close()
            current = LiquibaseChangeset(author=m.group(1), cid=m.group(2), sql="", line=lineno)
            sql_lines, rollback_lines = [], []
            continue
        pm = _LIQUIBASE_PROPERTY_LINE.match(line)
        if pm:
            props[pm.group(1)] = pm.group(2)
            continue
        if current is None:
            continue  # file header
        rm = _LIQUIBASE_ROLLBACK_LINE.match(line)
        if rm:
            current.has_rollback = True
            body = (rm.group(1) or "").strip()
            if body and body.lower() != "empty":
                rollback_lines.append(body)
            continue
        if _LIQUIBASE_META_LINE.match(line):
            continue
        sql_lines.append(line)
    _close()
    return changesets


def _diff_base_ref(opts: argparse.Namespace) -> Optional[str]:
    return getattr(opts, "base_sha", None) or ("HEAD" if getattr(opts, "local", False) else None)


def _repo_relative(path: str) -> str:
    """git show needs a repo-relative path; explicit CLI args may be absolute."""
    if not os.path.isabs(path):
        return path
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.relpath(path, proc.stdout.strip())
    except OSError:
        pass
    return path


def _file_at_diff_base(path: str, opts: argparse.Namespace) -> Optional[str]:
    """The file's content at the diff base (CI: --base-sha; local: HEAD).
    None when there is no base or the file did not exist there."""
    ref = _diff_base_ref(opts)
    if not ref:
        return None
    try:
        proc = subprocess.run(["git", "show", f"{ref}:{_repo_relative(path)}"], capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def new_formatted_changesets(path: str, text: str, opts: argparse.Namespace) -> list[LiquibaseChangeset]:
    """The changesets that are new or edited relative to the diff base. With no
    base available, every changeset counts (and old ones get re-analyzed)."""
    changesets = parse_formatted_changelog(text)
    base_text = _file_at_diff_base(path, opts)
    if base_text is None:
        return changesets
    base = {(c.author, c.cid): c.sql for c in parse_formatted_changelog(base_text)}
    return [c for c in changesets if base.get((c.author, c.cid)) != c.sql]


def extract_liquibase_formatted(path: str, text: str, opts: argparse.Namespace) -> Optional[tuple[str, None]]:
    new = new_formatted_changesets(path, text, opts)
    if not new:
        info(f"{path}: no new or edited changesets vs the diff base — nothing to analyze")
        return None
    return "\n".join(c.sql.rstrip().rstrip(";") + ";" for c in new if c.sql.strip()), None


def liquibase_formatted_rollback(path: str, opts: argparse.Namespace) -> Optional[dict]:
    """Rollback audit for a formatted-SQL changelog: the new changesets'
    ``--rollback`` SQL, reversed (the order Liquibase would run them). Any new
    changeset without a ``--rollback`` comment makes the whole change
    ``missing`` (a batch rollback would fail there). Declared-empty rollbacks
    count as present but contribute no SQL; if that leaves nothing, the audit
    is unchecked rather than sending empty SQL."""
    text = _read_small(path)
    if text is None:
        return None
    if _diff_base_ref(opts) is None:
        # No usable diff base: every changeset would look new, and one old
        # changeset without --rollback would wrongly report the whole change
        # as missing. Leave the audit unchecked instead of guessing.
        info(f"rollback: {path}: no diff base to separate new changesets — rollback unchecked")
        return None
    new = new_formatted_changesets(path, text, opts)
    if not new:
        return None
    if not all(c.has_rollback for c in new):
        return {"status": "missing"}
    parts = [c.rollback_sql for c in reversed(new) if c.rollback_sql.strip()]
    if not parts:
        info(f"rollback: {path}: all new changesets declare empty rollbacks — rollback unchecked")
        return None
    return {"sql": "\n".join(parts)}


def _liquibase_cli(opts: argparse.Namespace) -> str:
    return getattr(opts, "liquibase_cmd", None) or "liquibase"


def _liquibase_engine(opts: argparse.Namespace) -> str:
    engine = getattr(opts, "engine", None)
    return engine if engine in ("postgresql", "mysql") else "postgresql"


def _strip_liquibase_bookkeeping(sql: str) -> str:
    """Drop DATABASECHANGELOG bookkeeping and comment-only blocks (the CLI's
    header banner would otherwise pass for SQL)."""
    stmts = [s for s in split_statements(sql)
             if _first_keyword(s) and not _LIQUIBASE_BOOKKEEPING_RE.search(s)]
    return ";\n".join(stmts) + (";" if stmts else "")


def _offline_state_dir() -> tuple[str, str]:
    """A throwaway offline-state CSV path (inside its own temp dir so cleanup
    is one rmtree). The file must not pre-exist: Liquibase creates it."""
    tmpdir = tempfile.mkdtemp(prefix="sixta-liquibase-")
    return tmpdir, os.path.join(tmpdir, "state.csv")


def render_liquibase(path: str, opts: argparse.Namespace) -> str:
    """Offline render of an XML/YAML/JSON changelog via the Liquibase CLI
    (mechanism A): ``update-sql`` against ``offline:<engine>`` needs no
    database. The offline state CSV goes to a temp file so repeated runs
    stay stateless (the default would persist ./databasechangelog.csv and
    silently render nothing the second time). Renders the whole (leaf)
    changelog; bookkeeping and comment banners are stripped."""
    engine = _liquibase_engine(opts)
    tmpdir, state = _offline_state_dir()
    cmd = [_liquibase_cli(opts), "--search-path=.", f"--changelog-file={path}",
           f"--url=offline:{engine}?changeLogFile={state}", "update-sql"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(
            f"liquibase CLI not available ({exc}) — install it in the CI job (see README, Spring Boot / "
            f"Liquibase) to analyze XML/YAML changelogs, or keep changelogs in SQL format")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"liquibase update-sql for {os.path.basename(path)} failed:\n{proc.stderr.strip()[:400]}")
    return _strip_liquibase_bookkeeping(proc.stdout)


def liquibase_structured_rollback(path: str, opts: argparse.Namespace) -> Optional[dict]:
    """Rollback audit for an XML/YAML/JSON changelog, verified against the
    real CLI: ``changelog-sync`` marks every changeset executed in a throwaway
    offline state CSV, then ``rollback-count-sql`` with an overshoot count
    renders the reverse of all of them (Liquibase clamps the count;
    ``future-rollback-sql`` renders nothing offline). A changeset with no
    auto-inverse and no <rollback> block fails with a rollback-impossible
    error -> ``missing``; other failures leave the audit unchecked."""
    engine = _liquibase_engine(opts)
    tmpdir, state = _offline_state_dir()
    base = [_liquibase_cli(opts), "--search-path=.", f"--changelog-file={path}",
            f"--url=offline:{engine}?changeLogFile={state}"]
    try:
        sync = subprocess.run(base + ["changelog-sync"], capture_output=True, text=True)
        if sync.returncode != 0:
            info(f"rollback: liquibase changelog-sync for {os.path.basename(path)} exited {sync.returncode} — rollback unchecked")
            return None
        proc = subprocess.run(base + ["rollback-count-sql", "999999"], capture_output=True, text=True)
    except OSError as exc:
        info(f"rollback: liquibase rollback render could not run ({exc}) — rollback unchecked")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if proc.returncode == 0:
        sql = _strip_liquibase_bookkeeping(proc.stdout)
        if sql.strip():
            return {"sql": sql}
        info(f"rollback: liquibase rollback render for {os.path.basename(path)} rendered nothing — rollback unchecked")
        return None
    err = proc.stderr.lower()
    if "rollbackimpossible" in err or "no inverse" in err or "does not support rollback" in err:
        return {"status": "missing"}
    info(f"rollback: liquibase rollback render for {os.path.basename(path)} exited {proc.returncode} — rollback unchecked")
    return None


# --------------------------------------------------------------------------
# SQL embedded in Java + MyBatis mappers (docs/spring-boot-support.md, Phase S3)
#
# Static extraction (mechanism C): native SQL living in annotations
# (@Query(nativeQuery = true), @NativeQuery, @NamedNativeQuery, Spring Data
# JDBC/R2DBC @Query), in JdbcTemplate / JdbcClient / createNativeQuery call
# sites, and in MyBatis XML mapper statements. JPQL (@Query without
# nativeQuery in a JPA repository) is deliberately skipped: entity-language
# queries would only produce false positives in a SQL analyzer.
# --------------------------------------------------------------------------

# A .java file is worth reading only when it hints at NATIVE embedded SQL.
# Bare @Query is deliberately absent: a JPQL-only repository would be
# discovered, extract nothing, and leave noise ("analyzed, 0 findings") on
# every JPA PR. nativeQuery/@NativeQuery/@NamedNativeQuery, the JDBC/R2DBC
# call sites, and the Spring Data JDBC/R2DBC Query import cover the SQL cases.
_JAVA_SQL_HINT_RE = re.compile(
    r"nativeQuery|@(?:NativeQuery|NamedNativeQuery)\b|JdbcTemplate|NamedParameterJdbcTemplate|JdbcClient"
    r"|DatabaseClient|createNativeQuery"
    r"|org\.springframework\.data\.(?:jdbc|r2dbc)\.repository\.query\.Query")
_JAVA_TEXT_BLOCK_RE = re.compile(r'"""\n?(.*?)"""', re.S)
_JAVA_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_JAVA_ANNOTATION_RE = re.compile(r"@(Query|NativeQuery|NamedNativeQuery)\s*\(")
_JAVA_CALL_RE = re.compile(
    r"\.\s*(query|queryForObject|queryForList|queryForMap|queryForRowSet|queryForStream"
    r"|update|batchUpdate|execute|sql|createNativeQuery)\s*\(")
_JAVA_CONSTANT_RE = re.compile(r'String\s+(\w+)\s*=\s*((?:"""\n?(?s:.*?)"""|"(?:[^"\\\n]|\\.)*"|[^;])+);')
# Spring Data JDBC / R2DBC repositories: @Query is always native SQL there.
_JAVA_NATIVE_QUERY_IMPORT_RE = re.compile(
    r"import\s+org\.springframework\.data\.(?:jdbc|r2dbc)\.repository\.query\.Query\s*;")

_MYBATIS_HINT_RE = re.compile(r"<mapper\s+namespace\s*=|mybatis\.org//DTD\s+Mapper", re.I)
_MYBATIS_STATEMENT_TAGS = ("select", "insert", "update", "delete")

_MYBATIS_DOLLAR_DESC = ("SIXTA: MyBatis ${...} string interpolation in this mapper concatenates the value "
                        "into the SQL text (injection risk, unlike #{...} parameters). Whitelist or bind it.")
_MYBATIS_DOLLAR_SECTION = "_Mapper uses `${...}` string interpolation: injection-prone, prefer `#{...}` parameters._"


def _skip_java_noncode(text: str, i: int) -> int:
    """The index just past a string literal or comment starting at i, or i when
    none starts here. Keeps paren counting honest: a ')' inside a string,
    // comment, or /* comment */ is not code."""
    ch = text[i]
    if ch == '"':
        block = _JAVA_TEXT_BLOCK_RE.match(text, i) or _JAVA_STRING_RE.match(text, i)
        if block:
            return block.end()
    elif ch == "/" and text[i:i + 2] == "//":
        nl = text.find("\n", i)
        return len(text) if nl == -1 else nl
    elif ch == "/" and text[i:i + 2] == "/*":
        end = text.find("*/", i + 2)
        return len(text) if end == -1 else end + 2
    return i


def _balanced_paren_span(text: str, open_idx: int) -> Optional[str]:
    """The content between text[open_idx] == '(' and its matching ')', skipping
    string literals and comments. None when unbalanced (malformed source)."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        j = _skip_java_noncode(text, i)
        if j != i:
            i = j
            continue
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return None


def _split_top_level(args: str, sep: str = ",") -> list[str]:
    """Split an annotation/call argument blob (or a string expression) on a
    top-level separator, ignoring separators inside strings, comments, and
    brackets."""
    parts: list[str] = []
    depth, buf, i, n = 0, [], 0, len(args)
    while i < n:
        j = _skip_java_noncode(args, i)
        if j != i:
            if args[i] == '"':
                buf.append(args[i:j])  # keep strings, drop comments
            i = j
            continue
        ch = args[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


def _java_unescape(s: str) -> str:
    return (s.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\r", " ")
            .replace(r"\"", '"').replace(r"\'", "'").replace("\\\\", "\\"))


def _resolve_string_expr(expr: str, constants: dict, depth: int = 0) -> Optional[str]:
    """The compile-time value of a Java string expression: literals and
    same-file String constants joined by ``+``. None when any operand is
    dynamic (a method call, parameter, unknown identifier): analyzing SQL with
    holes ("SELECT * FROM  WHERE ...") would mislead, so such fragments are
    skipped entirely."""
    if depth > 5:
        return None
    out: list[str] = []
    for part in _split_top_level(expr, "+"):
        s = part.strip()
        if not s:
            return None
        block = _JAVA_TEXT_BLOCK_RE.fullmatch(s)
        if block:
            out.append(block.group(1))
            continue
        m = _JAVA_STRING_RE.fullmatch(s)
        if m:
            out.append(_java_unescape(m.group(1)))
            continue
        if re.fullmatch(r"\w+", s) and s in constants:
            resolved = _resolve_string_expr(constants[s], constants, depth + 1)
            if resolved is None:
                return None
            out.append(resolved)
            continue
        return None
    return "".join(out) if out else None


def _java_constants(text: str) -> dict:
    return {m.group(1): m.group(2) for m in _JAVA_CONSTANT_RE.finditer(text)}


def normalize_embedded_sql(sql: str) -> str:
    """Make embedded-SQL parameter syntaxes parseable: SpEL expressions
    (``:#{...}`` / ``?#{...}``) become a named parameter, MyBatis ``#{...}``
    becomes ``?``, JPA positional ``?1`` becomes plain ``?``. Plain ``:name``
    and ``?`` pass through untouched."""
    sql = re.sub(r"[?:]#\{[^}]*\}", ":spel_param", sql)
    sql = re.sub(r"#\{[^}]*\}", "?", sql)
    sql = re.sub(r"\?(\d+)", "?", sql)
    return sql


def _looks_like_sql(fragment: str) -> bool:
    return _first_keyword(fragment) in DDL_KEYWORDS + DML_KEYWORDS


def java_sql_target(path: str) -> bool:
    if not path.endswith(".java") or flyway_java_target(path):
        return False
    text = _read_small(path)
    return bool(text and _JAVA_SQL_HINT_RE.search(text))


def extract_java_sql(text: str) -> list[str]:
    """Native SQL fragments from Java source, normalized for parsing. JPQL
    (@Query without nativeQuery=true in a JPA repository) is skipped."""
    constants = _java_constants(text)
    jdbc_query_import = bool(_JAVA_NATIVE_QUERY_IMPORT_RE.search(text))
    fragments: list[str] = []

    for m in _JAVA_ANNOTATION_RE.finditer(text):
        name = m.group(1)
        blob = _balanced_paren_span(text, m.end() - 1)
        if blob is None:
            continue
        named: dict[str, str] = {}
        positional = None
        for attr in _split_top_level(blob):
            am = re.match(r"\s*(\w+)\s*=(?!=)\s*(.*)", attr, re.S)
            if am:
                named[am.group(1)] = am.group(2)
            elif positional is None:
                positional = attr
        if name == "Query" and not jdbc_query_import and "true" not in named.get("nativeQuery", ""):
            continue  # JPQL, not SQL
        expr = named.get("value") or named.get("query") or positional or ""
        sql = (_resolve_string_expr(expr, constants) or "").strip()
        if sql and _looks_like_sql(sql):
            fragments.append(normalize_embedded_sql(sql))

    for m in _JAVA_CALL_RE.finditer(text):
        blob = _balanced_paren_span(text, m.end() - 1)
        if blob is None:
            continue
        args = _split_top_level(blob)
        if not args:
            continue
        sql = (_resolve_string_expr(args[0], constants) or "").strip()
        if sql and _looks_like_sql(sql):
            fragments.append(normalize_embedded_sql(sql))

    seen: set[str] = set()  # annotation + call-site scans can hit the same literal
    return [f for f in fragments if not (f in seen or seen.add(f))]


def mybatis_mapper_target(path: str) -> bool:
    if not path.endswith(".xml"):
        return False
    text = _read_small(path)
    return bool(text and _MYBATIS_HINT_RE.search(text))


class _MybatisIncomplete(Exception):
    """The statement cannot be flattened faithfully (unresolvable or cyclic
    <include>): skip it rather than analyze made-up SQL."""


def _mybatis_flatten(el, sql_fragments: dict, stack: tuple = ()) -> str:
    """Best-effort flattening of MyBatis dynamic SQL to one analyzable branch:
    <if>/<when> keep their content, <choose> keeps only its first <when>,
    <where>/<set> become the keyword, <trim>/<foreach> honor their
    prefix/open/close attributes, <include> resolves through the mapper's
    <sql> fragments, <bind>/<selectKey> disappear (selectKey is its own
    statement, not part of the parent's SQL)."""
    parts = [el.text or ""]
    for child in el:
        tag = child.tag.lower() if isinstance(child.tag, str) else ""
        if tag == "choose":
            when = next((c for c in child if isinstance(c.tag, str) and c.tag.lower() == "when"), None)
            parts.append(_mybatis_flatten(when, sql_fragments, stack) if when is not None else "")
        elif tag == "include":
            refid = child.get("refid", "")
            frag = sql_fragments.get(refid)
            if frag is None or refid in stack:
                raise _MybatisIncomplete(refid or "include")
            parts.append(_mybatis_flatten(frag, sql_fragments, stack + (refid,)))
        elif tag in ("bind", "selectkey"):
            pass
        elif tag == "where":
            inner = _mybatis_flatten(child, sql_fragments, stack).strip()
            parts.append(" WHERE " + re.sub(r"^(AND|OR)\b", "", inner, flags=re.I).strip())
        elif tag == "set":
            inner = _mybatis_flatten(child, sql_fragments, stack).strip().rstrip(",")
            parts.append(" SET " + inner)
        elif tag == "trim":
            inner = _mybatis_flatten(child, sql_fragments, stack).strip()
            for override in (child.get("prefixOverrides") or "").split("|"):
                override = override.strip()
                if override and re.match(re.escape(override), inner, re.I):
                    inner = inner[len(override):].lstrip()
                    break
            parts.append(f" {child.get('prefix', '')} {inner} {child.get('suffix', '')} ")
        elif tag == "foreach":
            inner = _mybatis_flatten(child, sql_fragments, stack).strip()
            parts.append(f" {child.get('open', '')}{inner}{child.get('close', '')} ")
        else:
            parts.append(_mybatis_flatten(child, sql_fragments, stack))
        parts.append(child.tail or "")
    return "".join(parts)


def extract_mybatis_sql(text: str) -> tuple[list[str], bool]:
    """(normalized SQL fragments, uses ${...} interpolation) from a MyBatis
    mapper. Raises RuntimeError when the XML does not parse (the caller
    records the skip)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", text))
    except ET.ParseError as exc:
        raise RuntimeError(f"mapper XML did not parse: {exc}")
    sql_fragments = {el.get("id", ""): el for el in root.iter()
                     if isinstance(el.tag, str) and el.tag.lower() == "sql"}
    fragments: list[str] = []
    for el in root.iter():
        if not (isinstance(el.tag, str) and el.tag.lower() in _MYBATIS_STATEMENT_TAGS):
            continue
        try:
            body = _mybatis_flatten(el, sql_fragments)
        except _MybatisIncomplete as exc:
            info(f"mapper statement id={el.get('id', '?')} skipped: unresolvable <include refid=\"{exc}\"> "
                 f"(cross-mapper or cyclic)")
            continue
        body = substitute_flyway_placeholders(body, {})  # ${col} -> col, same token rule
        body = normalize_embedded_sql(body)
        body = re.sub(r"\s+", " ", body).strip()
        if body and _looks_like_sql(body):
            fragments.append(body)
    return fragments, "${" in text


# --------------------------------------------------------------------------
# Framework dispatch — turn a changed file into SQL + an optional review note
# --------------------------------------------------------------------------

_RUNPYTHON_DESC = ("SIXTA: migration contains RunPython — its effects emit no SQL and were NOT analyzed. "
                   "Review data-migration logic by hand (long transactions, per-row updates on big tables).")
_RUNPYTHON_SECTION = "_Contains `RunPython`: not renderable to SQL — flagged for human review._"
_ALEMBIC_DATA_DESC = ("SIXTA: migration contains data-migration code (op.bulk_insert / get_bind) — it emits no "
                      "analyzable DDL offline and was NOT analyzed. Review it by hand (long transactions, "
                      "per-row updates on big tables).")
_ALEMBIC_DATA_SECTION = "_Contains data-migration code: not renderable to SQL — flagged for human review._"
_FLYWAY_JAVA_DESC = ("SIXTA: Flyway Java-based migration. It renders no SQL offline and was NOT analyzed. "
                     "Review the migration code by hand (long transactions, per-row updates on big tables).")
_FLYWAY_JAVA_SECTION = "_Java-based Flyway migration: not renderable to SQL offline. Flagged for human review._"
_ROLLBACK_ARTIFACT_DESC = ("SIXTA: rollback artifact (undo script). Not analyzed as a forward change; when its "
                           "forward migration is in this changeset, the rollback audit covers it there.")
_ROLLBACK_ARTIFACT_SECTION = "_Rollback artifact: feeds the forward migration's rollback audit, not analyzed as a forward change._"


def _rollback_artifact_forward_exists(path: str) -> bool:
    """True when the rollback-named file has the forward migration it undoes
    sitting next to it: U<version>__*.sql needs a V<version>__*.sql sibling,
    foo.down.sql / foo.rollback.sql need foo.sql. Without the pair, the
    rollback naming is treated as coincidence and the file analyzed normally."""
    base = os.path.basename(path)
    directory = os.path.dirname(path) or "."
    um = _FLYWAY_UNDO_RE.match(base)
    if um:
        try:
            names = os.listdir(directory)
        except OSError:
            return False
        for name in names:
            vm = _FLYWAY_VERSIONED_RE.match(name)
            if vm and vm.group("version") == um.group("version"):
                return True
        return False
    for suffix in _ROLLBACK_SQL_SUFFIXES:
        if base.endswith(suffix):
            return os.path.exists(os.path.join(directory, base[: -len(suffix)] + ".sql"))
    return False


def _manual_review(path: str, description: str, check_name: str, section: str) -> tuple["Finding", str]:
    return Finding(path=path, severity="Info", description=description, check_name=check_name), section


def is_migration_file(path: str) -> bool:
    """Any file the kit knows how to turn into SQL (used by change discovery):
    migrations, changelogs, and app code carrying embedded SQL."""
    return (bool(migration_target(path)) or alembic_target(path) or flyway_java_target(path)
            or liquibase_structured_target(path) or java_sql_target(path)
            or mybatis_mapper_target(path) or path.endswith(".sql"))


def extract_migration(path: str, opts: argparse.Namespace) -> Optional[tuple[str, Optional[tuple["Finding", str]]]]:
    """Render a changed file to SQL, dispatching by framework. Returns
    ``(sql, manual)`` where ``manual`` is an optional (Finding, section) pair for
    a code data-migration that emits no SQL, or ``None`` to skip the file.
    Raises RuntimeError if a renderer/read fails (the caller records the skip)."""
    target = migration_target(path)
    if target:
        app, name = target
        sql = render_migration(opts.manage_py, app, name)
        manual = _manual_review(path, _RUNPYTHON_DESC, "runpython-manual-review", _RUNPYTHON_SECTION) if has_runpython(path) else None
        return sql, manual
    if alembic_target(path):
        sql = render_alembic(path, opts)
        manual = _manual_review(path, _ALEMBIC_DATA_DESC, "data-migration-manual-review", _ALEMBIC_DATA_SECTION) if alembic_data_ops(path) else None
        return sql, manual
    if flyway_java_target(path):
        return "", _manual_review(path, _FLYWAY_JAVA_DESC, "flyway-java-manual-review", _FLYWAY_JAVA_SECTION)
    if liquibase_structured_target(path):
        text = _read_small(path) or ""
        if _LIQUIBASE_INCLUDE_RE.search(text):
            return "", _manual_review(path, _LIQUIBASE_INDEX_DESC, "changelog-index", _LIQUIBASE_INDEX_SECTION)
        return render_liquibase(path, opts), None
    if java_sql_target(path):
        fragments = extract_java_sql(_read_small(path) or "")
        if not fragments:
            return None
        return ";\n".join(f.rstrip().rstrip(";") for f in fragments) + ";", None
    if mybatis_mapper_target(path):
        fragments, uses_dollar = extract_mybatis_sql(_read_small(path) or "")
        if not fragments and not uses_dollar:
            return None
        sql = ";\n".join(f.rstrip().rstrip(";") for f in fragments) + (";" if fragments else "")
        manual = None
        if uses_dollar:
            # Info like every local flag: a local severity can't gate
            # consistently across api modes (v1's server worst_severity is
            # authoritative), so it informs rather than pretending to block.
            manual = _manual_review(path, _MYBATIS_DOLLAR_DESC, "mybatis-string-interpolation",
                                    _MYBATIS_DOLLAR_SECTION)
        return sql, manual
    if path.endswith(".sql"):
        base = os.path.basename(path)
        if _is_rollback_sql_file(base) and _rollback_artifact_forward_exists(path):
            # An undo script is not a forward change: its content rides on the
            # forward migration's rollback audit (see sql_rollback), so grading
            # its DROPs as production risk would be a false alarm. Only the
            # naming convention is not proof enough (Update_prices__2024.sql
            # matches the Flyway undo pattern): the matching forward migration
            # must exist, else the file is analyzed as a normal change.
            return "", _manual_review(path, _ROLLBACK_ARTIFACT_DESC, "rollback-artifact", _ROLLBACK_ARTIFACT_SECTION)
        try:
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
        except OSError as exc:
            raise RuntimeError(f"cannot read {path}: {exc}")
        if is_liquibase_formatted(sql):
            return extract_liquibase_formatted(path, sql, opts)
        if _is_flyway_sql(base) and "${" in sql:
            sql = substitute_flyway_placeholders(sql, flyway_placeholders())
        return sql, None
    return None


# --------------------------------------------------------------------------
# Rollback audit (v1 batch mode only) — probe the framework's OWN reverse
# migration and attach it to the migration extraction. The server analyzes the
# rendered rollback and raises the "no rollback prepared" finding family; with
# options.require_rollback it becomes gate-able. Never author reverse SQL here,
# and never fail the run because a backwards render broke: any unexpected
# failure leaves the extraction unchecked (no `rollback` field at all).
# MCP mode skips this entirely — the MCP tools carry no rollback parameter.
# --------------------------------------------------------------------------

def django_rollback(manage_py: str, app: str, name: str) -> Optional[dict]:
    """Reverse render via ``manage.py sqlmigrate <app> <name> --backwards``.
    Success -> ``{"sql": ...}``; Django's IrreversibleError (any nonzero exit
    mentioning "irreversible") -> ``{"status": "irreversible"}``; anything else
    -> ``None`` (unchecked, logged)."""
    try:
        proc = subprocess.run(
            [sys.executable, manage_py, "sqlmigrate", app, name, "--backwards"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        info(f"rollback: sqlmigrate {app} {name} --backwards could not run ({exc}) — rollback unchecked")
        return None
    if proc.returncode == 0:
        return {"sql": proc.stdout}
    if "irreversible" in proc.stderr.lower():
        return {"status": "irreversible"}
    info(f"rollback: sqlmigrate {app} {name} --backwards exited {proc.returncode} — rollback unchecked")
    return None


def _alembic_downgrade_missing(path: str) -> bool:
    """True when the migration has no usable ``downgrade()``: the function is
    absent, or its body is only ``pass`` / a docstring / ``...`` /
    ``raise NotImplementedError``. Unparseable files return False (can't tell,
    so don't claim "missing")."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return False
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "downgrade"),
        None,
    )
    if func is None:
        return True
    for stmt in func.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring or bare `...`
        if isinstance(stmt, ast.Raise):
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                continue
        return False  # a real statement — the downgrade does something
    return True


def alembic_rollback(path: str, opts: argparse.Namespace) -> Optional[dict]:
    """Reverse render via offline ``alembic downgrade <rev>:<down> --sql``
    (mirror of :func:`render_alembic`). A trivially empty ``downgrade()`` body
    (pass / raise NotImplementedError / absent) -> ``{"status": "missing"}``
    without rendering (the offline render would still emit alembic_version
    bookkeeping SQL, so its output can't distinguish an empty downgrade). A
    real body that renders -> ``{"sql": ...}``; a real body whose render fails
    or comes back empty -> ``None`` (unchecked, logged)."""
    try:
        rev, down = _alembic_revisions(path)
    except RuntimeError:
        return None  # merge/unparseable — the forward render already reported it
    if _alembic_downgrade_missing(path):
        return {"status": "missing"}
    config = getattr(opts, "alembic_config", None) or "alembic.ini"
    try:
        proc = subprocess.run(
            ["alembic", "-c", config, "downgrade", f"{rev}:{down}", "--sql"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        info(f"rollback: alembic downgrade render for {os.path.basename(path)} could not run ({exc}) — rollback unchecked")
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return {"sql": proc.stdout}
    info(f"rollback: alembic downgrade render for {os.path.basename(path)} failed or rendered nothing — rollback unchecked")
    return None


# Flyway-style versioned migration / undo companion (V2_1__desc.sql / U2_1__*.sql).
_FLYWAY_VERSIONED_RE = re.compile(r"^V(?P<version>.+?)__.+\.sql$")
_FLYWAY_UNDO_RE = re.compile(r"^U(?P<version>.+?)__.+\.sql$")
_ROLLBACK_SQL_SUFFIXES = (".rollback.sql", ".down.sql")


def _is_rollback_sql_file(basename: str) -> bool:
    return bool(_FLYWAY_UNDO_RE.match(basename)) or basename.endswith(_ROLLBACK_SQL_SUFFIXES)


def _read_rollback_file(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
    except OSError as exc:
        info(f"rollback: cannot read companion {path} ({exc}) — rollback unchecked")
        return None
    # Flyway substitutes ${placeholders} in undo scripts exactly like forward
    # ones; mirror that so the server analyzes what would actually run.
    if _is_flyway_sql(os.path.basename(path)) and "${" in sql:
        sql = substitute_flyway_placeholders(sql, flyway_placeholders())
    return {"sql": sql}


def sql_rollback(path: str) -> Optional[dict]:
    """Companion undo-file check for plain ``.sql`` migrations. Flyway
    ``V<version>__name.sql`` looks for ``U<version>__*.sql`` in the same
    directory; a bare ``foo.sql`` looks for ``foo.rollback.sql`` /
    ``foo.down.sql``. Found -> ``{"sql": <contents>}``; not found ->
    ``{"status": "missing"}``. Files that ARE rollback artifacts are never
    checked (``None``)."""
    base = os.path.basename(path)
    if _is_rollback_sql_file(base):
        return None
    if _FLYWAY_REPEATABLE_RE.match(base):
        # Repeatable migrations re-run on every checksum change; their rollback
        # is the previous file version, so a missing-undo finding would be noise.
        return None
    directory = os.path.dirname(path) or "."
    m = _FLYWAY_VERSIONED_RE.match(base)
    if m:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            names = []
        for name in names:
            um = _FLYWAY_UNDO_RE.match(name)
            if um and um.group("version") == m.group("version"):
                return _read_rollback_file(os.path.join(directory, name))
        return {"status": "missing"}
    stem = base[: -len(".sql")]
    for suffix in _ROLLBACK_SQL_SUFFIXES:
        candidate = os.path.join(directory, stem + suffix)
        if os.path.exists(candidate):
            return _read_rollback_file(candidate)
    return {"status": "missing"}


def extract_rollback(path: str, opts: argparse.Namespace) -> Optional[dict]:
    """Framework dispatch for the rollback probe. Returns the migration
    extraction's ``rollback`` value or ``None`` to leave it unchecked. Must
    never raise."""
    target = migration_target(path)
    if target:
        app, name = target
        return django_rollback(opts.manage_py, app, name)
    if alembic_target(path):
        return alembic_rollback(path, opts)
    if liquibase_structured_target(path):
        text = _read_small(path) or ""
        if _LIQUIBASE_INCLUDE_RE.search(text):
            return None  # index of other changelogs — nothing of its own to roll back
        return liquibase_structured_rollback(path, opts)
    if path.endswith(".sql"):
        head = _read_small(path)
        if head is not None and is_liquibase_formatted(head):
            return liquibase_formatted_rollback(path, opts)
        return sql_rollback(path)
    return None


# --------------------------------------------------------------------------
# Analysis orchestration
# --------------------------------------------------------------------------

def analyze_sql(
    client: SixtaClient,
    path: str,
    sql: str,
    engine: str,
    version: Optional[str],
    hints: dict,
    fail_mode: str,
) -> FileReport:
    report = FileReport(path=path)
    statements = split_statements(sql)
    ddl = [s for s in statements if classify_statement(s) == "ddl"]
    dml = [s for s in statements if classify_statement(s) == "dml"]
    other = [s for s in statements if classify_statement(s) == "other"]

    # DDL: one call per hint-group. Statements on a table with .sixta.yml hints
    # go in their own call so table_size_bytes etc. apply to the right table;
    # everything else batches into a single call.
    for group_sql, table_hints in _ddl_groups(ddl, hints):
        args: dict = {"sql": group_sql, "engine": engine}
        if version:
            args["version"] = version
        if table_hints.get("size_bytes"):
            args["table_size_bytes"] = int(table_hints["size_bytes"])
        if "has_foreign_keys" in table_hints:
            args["table_has_foreign_keys"] = bool(table_hints["has_foreign_keys"])
        if "has_triggers" in table_hints:
            args["table_has_triggers"] = bool(table_hints["has_triggers"])
        _run_tool(client, "sixta_analyze_schema_change", args, report, fail_mode)

    # DML: one call per statement (RunSQL data migrations, .sql files).
    for stmt in dml:
        args = {"query": stmt, "engine": engine}
        if version:
            args["version"] = version
        _run_tool(client, "sixta_analyze_query", args, report, fail_mode)

    for stmt in other:
        kw = _first_keyword(stmt)
        report.sections.append(f"_Not analyzed ({kw or 'unrecognized'} statement — outside sixta-review v1 scope)._")

    return report


def _ddl_groups(ddl: list[str], hints: dict) -> list[tuple[str, dict]]:
    if not ddl:
        return []
    if not hints:
        return [(";\n".join(ddl) + ";", {})]
    groups: dict[Optional[str], list[str]] = {}
    for stmt in ddl:
        table = extract_table(stmt)
        key = table if table in hints else None
        groups.setdefault(key, []).append(stmt)
    out: list[tuple[str, dict]] = []
    for key, stmts in groups.items():
        out.append((";\n".join(stmts) + ";", hints.get(key, {}) if key else {}))
    return out


def _run_tool(client: SixtaClient, tool: str, args: dict, report: FileReport, fail_mode: str) -> None:
    try:
        result = client.call(tool, args)
    except SixtaConnectivityError as exc:
        if fail_mode == "closed":
            die(f"SIXTA unreachable and --fail-mode=closed: {exc}", code=2)
        warn(str(exc))
        report.skipped.append(f"{tool}: SIXTA unreachable — analysis skipped (fail-open). {exc}")
        return
    except SixtaToolError as exc:
        if fail_mode == "closed":
            die(f"SIXTA tool error and --fail-mode=closed: {exc}", code=2)
        warn(f"{tool}: {exc}")
        report.skipped.append(f"{tool}: tool error — analysis skipped (fail-open). {exc}")
        return
    report.sections.append(_result_text(result))
    report.findings.extend(findings_from_result(result, report.path))


def analyze_files(files: list[str], opts: argparse.Namespace, client: SixtaClient, hints: dict) -> list[FileReport]:
    reports: list[FileReport] = []
    for path in files:
        try:
            extracted = extract_migration(path, opts)
        except RuntimeError as exc:
            rep = FileReport(path=path)
            rep.skipped.append(str(exc))
            warn(str(exc))
            reports.append(rep)
            continue
        if extracted is None:
            continue
        sql, manual = extracted
        rep = analyze_sql(client, path, sql, opts.engine, opts.engine_version, hints, opts.fail_mode)
        if manual:
            finding, section = manual
            rep.findings.append(finding)
            rep.sections.append(section)
        reports.append(rep)
    return reports


# --------------------------------------------------------------------------
# Batch analysis via POST /v1/analyze (one request per pipeline run)
# --------------------------------------------------------------------------

def capture_schema(opts: argparse.Namespace) -> Optional[str]:
    """Best-effort shared schema DDL for the batch. Runs an explicit
    ``--schema-cmd`` when set, else ``pg_dump --schema-only`` when the engine is
    PostgreSQL and a database is configured (PGHOST/PGDATABASE/DATABASE_URL).
    Any failure logs a warning and returns None — the batch proceeds without
    schema-tier confidence rather than failing."""
    cmd = opts.schema_cmd
    if not cmd and opts.engine == "postgresql" and (
        os.environ.get("DATABASE_URL") or os.environ.get("PGHOST") or os.environ.get("PGDATABASE")
    ):
        cmd = "pg_dump --schema-only --no-owner --no-privileges"
    if not cmd:
        return None
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"schema capture ({cmd!r}) failed: {exc} — continuing without shared schema")
        return None
    if proc.returncode != 0:
        warn(f"schema capture ({cmd!r}) exited {proc.returncode}: {proc.stderr.strip()[:200]} — continuing without shared schema")
        return None
    return proc.stdout.strip() or None


def _v1_table_hints(hints: dict) -> dict:
    """Project .sixta.yml table hints onto the keys /v1 understands."""
    out: dict = {}
    for table, h in (hints or {}).items():
        if not isinstance(h, dict):
            continue
        clean = {k: h[k] for k in V1_HINT_KEYS if k in h}
        if clean:
            out[str(table).lower()] = clean
    return out


def run_v1(files: list[str], opts: argparse.Namespace, client: SixtaClient, hints: dict):
    """Analyze the whole changeset in one /v1/analyze POST. Returns
    ``(reports, server_renders, context, server_worst)`` where server_renders
    is the response's ``renders`` block (or None), used to prefer server-side
    code-quality JSON, context is the response's ``context`` block (or None)
    reporting where the production context came from (Connect Pro), and
    server_worst is the response's ``worst_severity`` (or None). The server's
    worst_severity is the authoritative gate input: it deliberately excludes
    advisory findings (the ``rollback:*`` family informs but does not gate)."""
    reports: dict[str, FileReport] = {}
    order: list[str] = []
    extractions: list[dict] = []
    ext_owner: list[str] = []  # extraction index -> owning file path

    def rep_for(path: str) -> FileReport:
        if path not in reports:
            reports[path] = FileReport(path=path)
            order.append(path)
        return reports[path]

    for path in files:
        try:
            extracted = extract_migration(path, opts)
        except RuntimeError as exc:
            rep_for(path).skipped.append(str(exc))
            warn(str(exc))
            continue
        if extracted is None:
            continue  # nothing extractable: no empty report section either
        rep = rep_for(path)
        sql, manual = extracted

        statements = split_statements(sql)
        ddl = [s for s in statements if classify_statement(s) == "ddl"]
        # All of one file's DDL rides in a single migration extraction (one
        # charge, statements still scored individually server-side); DML is one
        # query extraction per statement. Table hints are applied server-side.
        if ddl:
            extraction: dict = {"kind": "migration", "sql": ";\n".join(ddl) + ";", "source_file": path}
            rollback = extract_rollback(path, opts)
            if rollback is not None:
                extraction["rollback"] = rollback
            extractions.append(extraction)
            ext_owner.append(path)
        for stmt in statements:
            kind = classify_statement(stmt)
            if kind == "dml":
                extractions.append({"kind": "query", "sql": stmt, "source_file": path})
                ext_owner.append(path)
            elif kind == "other":
                kw = _first_keyword(stmt)
                rep.sections.append(f"_Not analyzed ({kw or 'unrecognized'} statement — outside sixta-review v1 scope)._")

        if manual:
            finding, section = manual
            rep.findings.append(finding)
            rep.sections.append(section)

    server_renders = None
    context = None
    server_worst = None
    if extractions:
        render = ["markdown", "sarif"] if getattr(opts, "platform", "gitlab") == "github" else ["markdown", "code-quality"]
        options: dict = {"render": render}
        if getattr(opts, "require_rollback", False):
            options["require_rollback"] = True  # absent = false, keeps older servers untouched
        request: dict = {"engine": opts.engine, "options": options, "extractions": extractions}
        if opts.engine_version:
            request["version"] = opts.engine_version
        rref = repo_ref()
        if rref:
            request["context"] = {"repo_ref": rref}  # repo→connection routing (Connect Pro)
        schema = capture_schema(opts)
        if schema:
            request["schema"] = {"format": "ddl", "content": schema}
        table_hints = _v1_table_hints(hints)
        if table_hints:
            request["table_hints"] = table_hints

        try:
            response = client.analyze_v1(request)
        except SixtaConnectivityError as exc:
            _batch_failed(reports, ext_owner, opts, f"SIXTA unreachable — batch analysis skipped (fail-open). {exc}", exc)
            return [reports[p] for p in order], None, None, None
        except SixtaToolError as exc:
            _batch_failed(reports, ext_owner, opts, f"SIXTA error — batch analysis skipped (fail-open). {exc}", exc)
            return [reports[p] for p in order], None, None, None

        _apply_v1_results(response, ext_owner, reports, opts, extractions)
        renders = response.get("renders")
        server_renders = renders if isinstance(renders, dict) else None
        ctx = response.get("context")
        # No context block from the server means no live grounding: surface
        # that as source "none" so the report can invite setting a connection up.
        context = ctx if isinstance(ctx, dict) else {"source": "none"}
        ws = response.get("worst_severity")
        server_worst = ws if ws in SEVERITY_RANK else None

    return [reports[p] for p in order], server_renders, context, server_worst


def _batch_failed(reports: dict, ext_owner: list, opts: argparse.Namespace, msg: str, exc: Exception) -> None:
    if opts.fail_mode == "closed":
        die(f"SIXTA batch failed and --fail-mode=closed: {exc}", code=2)
    warn(str(exc))
    for path in dict.fromkeys(ext_owner):  # unique, preserving order
        reports[path].skipped.append(msg)


def _sql_snippet(sql: str, max_lines: int = 8, max_chars: int = 480) -> str:
    """The analyzed statement, fenced for the report, truncated for sanity."""
    body = sql.strip()
    lines = body.splitlines()
    if len(lines) > max_lines:
        body = "\n".join(lines[:max_lines]) + "\n-- ... truncated"
    elif len(body) > max_chars:
        body = body[:max_chars] + "\n-- ... truncated"
    return f"```sql\n{body}\n```"


def _apply_v1_results(response: dict, ext_owner: list, reports: dict, opts: argparse.Namespace, extractions: Optional[list] = None) -> None:
    for res in response.get("results") or []:
        idx = res.get("index")
        path = ext_owner[idx] if isinstance(idx, int) and 0 <= idx < len(ext_owner) else res.get("source_file")
        if path is None or path not in reports:
            continue
        rep = reports[path]
        kind = res.get("kind", "analysis")
        if res.get("rate_limited"):
            if opts.fail_mode == "closed":
                die(f"SIXTA rate-limited an extraction and --fail-mode=closed", code=2)
            rep.skipped.append(f"{kind}: rate limited (retry after {res.get('retry_after', '?')}s) — analysis skipped (fail-open).")
            continue
        if res.get("error"):
            message = str((res["error"] or {}).get("message") or "error")
            if opts.fail_mode == "closed":
                die(f"SIXTA extraction error and --fail-mode=closed: {message}", code=2)
            rep.skipped.append(f"{kind}: {_hint_if_auth(message)} — analysis skipped (fail-open).")
            continue
        if res.get("report_text"):
            # Anchor the verdict to the exact statement it judged: with several
            # extractions per file, an unquoted "query analysis" section is
            # ambiguous (which UPDATE?). The SQL was sent by us, so quoting it
            # back is payload the reader already owns.
            sql = (extractions[idx].get("sql") if extractions and isinstance(idx, int) and 0 <= idx < len(extractions) else None)
            section = f"{_sql_snippet(sql)}\n\n{res['report_text']}" if sql else res["report_text"]
            rep.sections.append(section)
        for f in res.get("findings") or []:
            rep.findings.append(
                Finding(
                    path=res.get("source_file") or path,
                    severity=f.get("severity") or "Info",
                    description=f"SIXTA: {f.get('title', f.get('rule_id', 'finding'))}",
                    check_name=str(f.get("rule_id") or f.get("operation") or "sixta"),
                )
            )


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

def render_markdown(reports: list[FileReport], gate: str, context: dict | None = None) -> str:
    lines = [NOTE_MARKER, "## SIXTA SQL review", ""]
    total = sum(len(r.findings) for r in reports)
    worst = worst_rank(reports)
    worst_name = next((k for k, v in SEVERITY_RANK.items() if v == worst), None)
    if not reports:
        lines.append("No changed migrations or SQL files found — nothing to analyze.")
    else:
        summary = f"{len(reports)} file(s) analyzed, {total} finding(s)"
        if worst_name and total:
            summary += f", worst severity **{worst_name}**"
        gate_rank = GATE_RANK.get(gate)
        if gate_rank is not None and worst >= gate_rank:
            summary += f" — **gate ({gate}) failed**"
        lines.append(summary + ".")
    # Connect Pro provenance: present only for entitled orgs; free responses
    # carry no context block, so this line never appears for them.
    if context and context.get("source") == "live":
        captured = context.get("captured_at") or ""
        suffix = f" (snapshot {captured})" if captured else ""
        lines.append(f"Production context: **live** database snapshot{suffix}.")
        # Grounded-connection guardrail: the server flags a writer-first pick among
        # several databases so a mis-binding (grading this service's migration
        # against another's database) is visible instead of silent.
        note = context.get("note")
        if isinstance(note, str) and note:
            lines.append(f"⚠️ {note}")
    elif context and context.get("source") in ("hints", "none"):
        source = context.get("source")
        opening = (
            "Production context: **declared hints**."
            if source == "hints"
            else "Production context: none. Verdicts use conservative assumptions where table size matters."
        )
        # Prefer the server's entitlement-aware links (context.action + docs_url);
        # fall back to the generic pointer for older servers.
        action = context.get("action") if isinstance(context.get("action"), dict) else None
        docs = context.get("docs_url")
        if action and action.get("kind") == "add_connection" and action.get("url"):
            cta = f"Add a live read-only connection to grade verdicts against your real table sizes, engine version, and traffic: {action['url']}"
        elif action and action.get("kind") == "upgrade" and action.get("url"):
            cta = f"Live-grounded verdicts (real table sizes, engine version, and traffic) are available on Connect Pro: {action['url']}"
        else:
            cta = (
                "A live read-only connection grades verdicts against your real table sizes, engine "
                "version, and traffic: connect.sixta.ai/portal/connections."
            )
        line = f"{opening} {cta}"
        if docs:
            line += f" How live context works: {docs}"
        lines.append(line)
    for r in reports:
        lines += ["", f"### `{r.path}`", ""]
        for s in r.sections:
            lines += [s, ""]
        for s in r.skipped:
            lines += [f"> ⚠️ {s}", ""]
    return "\n".join(lines)


def write_code_quality(reports: list[FileReport], out_path: str) -> None:
    write_code_quality_entries([f.code_quality() for r in reports for f in r.findings], out_path)


def write_code_quality_entries(entries: list, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1)


def upsert_mr_note(markdown: str) -> None:
    """Post/update the report as an MR note. Requires SIXTA_BOT_TOKEN (project
    access token with `api` scope, Reporter+): CI_JOB_TOKEN cannot post notes."""
    token = os.environ.get("SIXTA_BOT_TOKEN")
    api = os.environ.get("CI_API_V4_URL")
    project = os.environ.get("CI_PROJECT_ID")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    if not token:
        info("SIXTA_BOT_TOKEN not set — skipping MR comment (report is still in artifacts/code-quality)")
        return
    if not (api and project and mr_iid):
        warn("not in a merge-request pipeline context — skipping MR comment")
        return
    base = f"{api}/projects/{project}/merge_requests/{mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": token, "content-type": "application/json"}
    body = markdown if len(markdown) < 900_000 else markdown[:900_000] + "\n\n_…truncated_"

    existing_id = None
    try:
        req = urllib.request.Request(f"{base}?per_page=100", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            for note in json.loads(resp.read().decode()):
                if isinstance(note.get("body"), str) and note["body"].startswith(NOTE_MARKER):
                    existing_id = note["id"]
                    break
        url = f"{base}/{existing_id}" if existing_id else base
        data = json.dumps({"body": body}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="PUT" if existing_id else "POST")
        with urllib.request.urlopen(req, timeout=30):
            pass
        info(f"MR note {'updated' if existing_id else 'posted'}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        warn(f"could not post MR note: {exc}")


# --------------------------------------------------------------------------
# GitHub Actions platform (PR comment, step summary, SARIF)
# --------------------------------------------------------------------------

def detect_platform(explicit: str) -> str:
    """Resolve --platform auto to github (in GitHub Actions) or gitlab."""
    if explicit != "auto":
        return explicit
    return "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "gitlab"


def repo_ref() -> str | None:
    """The repository identifier from the CI environment (e.g. ``org/app-1``),
    sent as ``context.repo_ref`` so Connect Pro can route this batch to the
    connection you bound that repo to. GitHub sets GITHUB_REPOSITORY; GitLab sets
    CI_PROJECT_PATH. None outside CI (routing then falls back to the writer)."""
    ref = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("CI_PROJECT_PATH")
    ref = (ref or "").strip()
    return ref[:256] or None


def github_base_sha() -> Optional[str]:
    """Diff base for a pull_request run: the PR event payload's base sha, or
    GITHUB_BASE_REF resolved to a sha (needs a full-history checkout)."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, encoding="utf-8") as fh:
                sha = json.load(fh).get("pull_request", {}).get("base", {}).get("sha")
            if sha:
                return sha
        except (OSError, ValueError):
            pass
    base_ref = os.environ.get("GITHUB_BASE_REF")
    for ref in ((f"origin/{base_ref}", base_ref) if base_ref else ()):
        try:
            out = _git("rev-parse", ref)
            if out:
                return out[0]
        except subprocess.CalledProcessError:
            continue
    return None


def github_pr_number() -> Optional[str]:
    """PR number from the event payload, or GITHUB_REF (refs/pull/<n>/merge)."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, encoding="utf-8") as fh:
                num = json.load(fh).get("pull_request", {}).get("number")
            if num is not None:
                return str(num)
        except (OSError, ValueError):
            pass
    ref = os.environ.get("GITHUB_REF", "")
    m = re.match(r"refs/pull/(\d+)/", ref)
    return m.group(1) if m else None


def build_sarif(reports: list[FileReport]) -> dict:
    """Local SARIF 2.1.0 fallback, built from findings when the server did not
    render it (mcp mode or an older server). Mirrors the server's shape."""
    results = []
    rule_ids: list[str] = []
    for r in reports:
        for f in r.findings:
            rid = f"sixta:{f.check_name}"
            if rid not in rule_ids:
                rule_ids.append(rid)
            results.append({
                "ruleId": rid,
                "level": SARIF_LEVEL.get(f.severity, "note"),
                "message": {"text": f.description},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path}, "region": {"startLine": 1}}}],
            })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "SIXTA", "informationUri": "https://connect.sixta.ai",
                                "rules": [{"id": rid, "name": rid} for rid in rule_ids]}},
            "results": results,
        }],
    }


def write_sarif(sarif_obj: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(sarif_obj, fh, indent=2)
    info(f"wrote SARIF to {out_path}")


def write_github_summary(markdown: str) -> None:
    """Append the report to the job's GITHUB_STEP_SUMMARY, if present."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.replace(NOTE_MARKER + "\n", "") + "\n")
    except OSError as exc:
        warn(f"could not write step summary: {exc}")


def upsert_github_comment(markdown: str) -> None:
    """Post/update the report as a PR comment via the GitHub REST API, matched
    by the hidden marker (same upsert pattern as the GitLab note). Uses
    GITHUB_TOKEN (the default Actions token; needs `pull-requests: write`)."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    pr = github_pr_number()
    if not token:
        info("GITHUB_TOKEN not set — skipping PR comment (report is still in the step summary / SARIF)")
        return
    if not (repo and pr):
        warn("not a pull_request event — skipping PR comment")
        return
    base = f"{api}/repos/{repo}/issues/{pr}/comments"
    headers = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
               "content-type": "application/json", "user-agent": "sixta-ci"}
    body = markdown if len(markdown) < 60_000 else markdown[:60_000] + "\n\n_…truncated_"

    existing_id = None
    try:
        req = urllib.request.Request(f"{base}?per_page=100", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            for note in json.loads(resp.read().decode()):
                if isinstance(note.get("body"), str) and note["body"].startswith(NOTE_MARKER):
                    existing_id = note["id"]
                    break
        if existing_id:
            url = f"{api}/repos/{repo}/issues/comments/{existing_id}"
            method = "PATCH"
        else:
            url = base
            method = "POST"
        data = json.dumps({"body": body}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30):
            pass
        info(f"PR comment {'updated' if existing_id else 'posted'}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        warn(f"could not post PR comment: {exc}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"sixta-review: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"sixta-review: WARNING: {msg}", file=sys.stderr)


def die(msg: str, code: int = 2) -> NoReturn:
    print(f"sixta-review: ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _env_flag(name: str) -> bool:
    """Boolean env parsing for wrapper-forwarded inputs ('false' means false)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sixta-review", description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="*", help="explicit files to review (default: git diff discovery)")
    p.add_argument("--local", action="store_true", help="pre-commit mode: staged/untracked files, console report only")
    p.add_argument("--platform", default=os.environ.get("SIXTA_PLATFORM", "auto"), choices=["auto", "gitlab", "github"],
                   help="CI platform for diff base, comment upsert, and artifacts (auto-detects GitHub Actions)")
    p.add_argument("--api", default=None, choices=["mcp", "v1"],
                   help="mcp: one JSON-RPC call per statement group; v1: one batch POST per run (default on GitHub)")
    p.add_argument("--schema-cmd", default=os.environ.get("SIXTA_SCHEMA_CMD") or None,
                   help="v1 only: command whose stdout is the shared schema DDL (default: pg_dump when a DB is configured)")
    p.add_argument("--engine", default=os.environ.get("SIXTA_ENGINE") or "auto", choices=["auto", "postgresql", "mysql"],
                   help="database engine; auto (default) detects it from pom.xml / build.gradle / application.* and falls back to postgresql")
    p.add_argument("--engine-version", default=os.environ.get("SIXTA_ENGINE_VERSION") or None)
    p.add_argument("--gate", default=os.environ.get("SIXTA_GATE", "high"), choices=list(GATE_RANK))
    p.add_argument("--fail-mode", default=os.environ.get("SIXTA_FAIL_MODE", "open"), choices=["open", "closed"],
                   help="behavior when SIXTA is unreachable/erroring (findings always gate)")
    p.add_argument("--require-rollback", action="store_true", default=_env_flag("SIXTA_REQUIRE_ROLLBACK"),
                   help="v1 only: raise the server's no-rollback finding to gate-able severity "
                        "(the rollback audit itself always runs in v1 mode)")
    p.add_argument("--sixta-url", default=os.environ.get("SIXTA_URL", DEFAULT_SIXTA_URL))
    p.add_argument("--manage-py", default=os.environ.get("SIXTA_MANAGE_PY", "manage.py"))
    p.add_argument("--alembic-config", default=os.environ.get("SIXTA_ALEMBIC_CONFIG", "alembic.ini"),
                   help="Alembic config file for offline SQL rendering (alembic upgrade --sql)")
    p.add_argument("--liquibase-cmd", default=os.environ.get("SIXTA_LIQUIBASE_CMD", "liquibase"),
                   help="Liquibase CLI used to render XML/YAML/JSON changelogs offline (update-sql; no database needed)")
    p.add_argument("--base-sha", default=os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"))
    p.add_argument("--report-md", default=REPORT_MD)
    p.add_argument("--code-quality", default=CODE_QUALITY_JSON)
    p.add_argument("--sarif", default=os.environ.get("SIXTA_SARIF", SARIF_JSON), help="GitHub: SARIF output path for code-scanning upload")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _write_empty_artifacts(opts: argparse.Namespace) -> None:
    """Empty SARIF / code-quality file so `always`-gated upload steps have one."""
    if opts.platform == "github":
        write_sarif(build_sarif([]), opts.sarif)
    else:
        write_code_quality([], opts.code_quality)


def _auth_failed(opts: argparse.Namespace, exc: SixtaAuthError) -> int:
    """Single decision point for a rejected API key. In CI this always fails
    (exit 2) regardless of --fail-mode — a bad key is persistent, and failing
    open would keep pipelines green with analysis silently skipped — while
    still publishing everything dependent steps and reviewers rely on: the
    report/artifact files, and an upserted PR/MR note superseding the previous
    run's verdict (a stale "gate passed" note must not stand next to a red
    pipeline). A local pre-commit run warns and lets the commit proceed:
    blocking every commit on a stale shell key would punish the developer,
    not protect CI."""
    if opts.local:
        warn(f"SIXTA rejected the API key: {exc}")
        warn("skipping SIXTA analysis for this commit — fix SIXTA_API_KEY to re-enable")
        return 0
    markdown = "\n".join([
        NOTE_MARKER,
        "## SIXTA SQL review",
        "",
        f"> ⚠️ Analysis did not run: {exc}",
        "",
        "Findings for this revision are unknown — fix the key and re-run the pipeline.",
        "",
    ])
    with open(opts.report_md, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    _write_empty_artifacts(opts)
    if opts.platform == "github":
        write_github_summary(markdown)
        upsert_github_comment(markdown)
    else:
        upsert_mr_note(markdown)
    die(f"SIXTA rejected the API key: {exc}", code=2)


def main(argv: Optional[list[str]] = None) -> int:
    opts = build_parser().parse_args(argv)
    opts.platform = detect_platform(opts.platform)
    # Resolve the API mode: explicit flag > SIXTA_API env > platform default
    # (GitHub is new, so it defaults to the v1 batch API; GitLab stays mcp).
    opts.api = opts.api or os.environ.get("SIXTA_API") or ("v1" if opts.platform == "github" else "mcp")
    if opts.require_rollback and opts.api != "v1":
        warn("--require-rollback has no effect in mcp mode — the rollback audit is /v1 only (set SIXTA_API=v1)")
    if opts.platform == "github" and not opts.base_sha:
        opts.base_sha = github_base_sha()
    opts.engine = resolve_engine(opts.engine)

    api_key = os.environ.get("SIXTA_API_KEY")
    if not api_key:
        warn("SIXTA_API_KEY not set. The hosted connect.sixta.ai requires a key and will return 401 (get a free one in about 30 seconds at connect.sixta.ai/portal); anonymous calls only work against self-hosted servers with anonymous access enabled.")

    files = changed_files(opts.base_sha, opts.local, opts.files)
    if not files:
        info("no changed migrations or SQL files — nothing to do")
        if not opts.local:
            _write_empty_artifacts(opts)
        return 0
    info(f"analyzing {len(files)} file(s): {', '.join(files)}")

    client = SixtaClient(opts.sixta_url, api_key)
    hints = load_table_hints()
    server_renders = None
    v1_context = None
    server_worst = None
    try:
        if opts.api == "v1":
            reports, server_renders, v1_context, server_worst = run_v1(files, opts, client, hints)
        else:
            reports = analyze_files(files, opts, client, hints)
    except SixtaAuthError as exc:
        return _auth_failed(opts, exc)
    append_ddl_auto_note(reports)
    markdown = render_markdown(reports, opts.gate, v1_context)

    if opts.local:
        print(markdown.replace(NOTE_MARKER + "\n", ""))
    else:
        with open(opts.report_md, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        if opts.platform == "github":
            # Prefer the server-rendered SARIF (the /v1 path); fall back to
            # building it locally from findings (mcp mode or an older server).
            sarif = server_renders.get("sarif") if isinstance(server_renders, dict) else None
            write_sarif(sarif if isinstance(sarif, dict) else build_sarif(reports), opts.sarif)
            write_github_summary(markdown)
            upsert_github_comment(markdown)
        else:
            # Prefer the server-rendered GitLab code-quality JSON; fall back to
            # building it locally from findings for older servers.
            cq = server_renders.get("code_quality") if isinstance(server_renders, dict) else None
            if isinstance(cq, list):
                write_code_quality_entries(cq, opts.code_quality)
            else:
                write_code_quality(reports, opts.code_quality)
            upsert_mr_note(markdown)

    gate_rank = GATE_RANK.get(opts.gate)
    if server_worst is not None:
        # v1 mode: the server's worst_severity is the gate input. It already
        # encodes which findings gate (advisory rollback:* findings, and any
        # local manual-review flags, inform but do not fail the pipeline).
        worst = SEVERITY_RANK[server_worst]
        worst_label = f"server worst_severity {server_worst}"
    else:
        worst = worst_rank(reports)
        worst_name = next((k for k, v in SEVERITY_RANK.items() if v == worst), "Info")
        worst_label = f"worst finding severity {worst_name}"
    if gate_rank is not None and worst >= gate_rank:
        info(f"gate failed: {worst_label} >= {opts.gate}")
        return 1
    info("gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
