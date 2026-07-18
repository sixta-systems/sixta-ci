"""The 'reviewed by SIXTA' badge: footer line, README snippet, and opt-out
entitlement handling (no network — pure helpers plus render_markdown)."""

import pytest

import sixta_review as sr


def _opts(**overrides):
    opts = sr.build_parser().parse_args(["--api", "v1"])
    opts.schema_cmd = None
    for k, v in overrides.items():
        setattr(opts, k, v)
    return opts


# --------------------------------------------------------------------------
# badge_origin
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://connect.sixta.ai/mcp", "https://connect.sixta.ai"),
        ("http://127.0.0.1:8080/mcp", "http://127.0.0.1:8080"),
        ("https://selfhost.example/v1/analyze", "https://selfhost.example"),
        ("connect.sixta.ai/mcp", "https://connect.sixta.ai"),
    ],
)
def test_badge_origin(url, expected):
    assert sr.badge_origin(url) == expected


# --------------------------------------------------------------------------
# Footer line
# --------------------------------------------------------------------------

def test_footer_on_by_default_and_neutral():
    line = sr.badge_footer_line(_opts(), None)
    assert "badge.svg" in line
    assert "Reviewed by SIXTA" in line
    assert "utm_source=badge" in line
    # Neutral always: no verdict wording in the footer.
    assert "unsafe" not in line
    assert "failed" not in line


def test_footer_uses_the_configured_host_for_self_hosted():
    line = sr.badge_footer_line(_opts(sixta_url="https://sixta.corp.internal/mcp"), None)
    assert "https://sixta.corp.internal/badge.svg" in line
    assert "connect.sixta.ai" not in line


def test_footer_opt_out_honored_when_server_says_removable():
    assert sr.badge_footer_line(_opts(badge=False), {"removable": True}) is None


def test_footer_opt_out_refused_without_entitlement(capsys):
    # Free plan (removable false) and no-signal (mcp mode / auth failure / anon)
    # both keep the footer and say why on stderr.
    for badge_info in ({"removable": False}, None):
        line = sr.badge_footer_line(_opts(badge=False), badge_info)
        assert line is not None and "badge.svg" in line
    err = capsys.readouterr().err
    assert "Connect Pro" in err


def test_footer_opt_out_in_mcp_mode_points_at_v1(capsys):
    line = sr.badge_footer_line(_opts(api="mcp", badge=False), None)
    assert line is not None
    assert "SIXTA_API=v1" in capsys.readouterr().err


# --------------------------------------------------------------------------
# render_markdown placement
# --------------------------------------------------------------------------

def test_render_markdown_appends_footer_last():
    rep = sr.FileReport(path="q.sql", sections=["report body"])
    md = sr.render_markdown([rep], "high", None, "FOOTER-LINE")
    assert md.rstrip().endswith("FOOTER-LINE")
    # And stays out when None (local mode / removed by a Pro opt-out).
    assert "FOOTER-LINE" not in sr.render_markdown([rep], "high", None, None)


def test_auth_failed_comment_carries_the_footer(tmp_path, monkeypatch):
    opts = _opts(local=False, platform="gitlab", report_md=str(tmp_path / "r.md"),
                 code_quality=str(tmp_path / "cq.json"))
    monkeypatch.setattr(sr, "upsert_mr_note", lambda md: None)
    with pytest.raises(SystemExit):
        sr._auth_failed(opts, sr.SixtaAuthError("401 unauthorized"))
    md = (tmp_path / "r.md").read_text()
    assert "badge.svg" in md
    assert "Analysis did not run" in md


# --------------------------------------------------------------------------
# README snippet
# --------------------------------------------------------------------------

def test_snippet_only_when_the_server_minted_a_url():
    assert sr.badge_snippet(None, sr.DEFAULT_SIXTA_URL) is None
    assert sr.badge_snippet({"removable": False}, sr.DEFAULT_SIXTA_URL) is None
    snip = sr.badge_snippet({"url": "https://connect.sixta.ai/badge/bXYZ.svg"}, sr.DEFAULT_SIXTA_URL)
    assert "https://connect.sixta.ai/badge/bXYZ.svg" in snip
    assert "```markdown" in snip
    assert "utm_medium=readme" in snip


# --------------------------------------------------------------------------
# Flag plumbing
# --------------------------------------------------------------------------

def test_badge_flag_defaults_and_env(monkeypatch):
    assert sr.build_parser().parse_args([]).badge is True
    assert sr.build_parser().parse_args(["--no-badge"]).badge is False
    monkeypatch.setenv("SIXTA_BADGE", "false")
    assert sr.build_parser().parse_args([]).badge is False
    monkeypatch.setenv("SIXTA_BADGE", "true")
    assert sr.build_parser().parse_args([]).badge is True
    # Wrapper-forwarded empty string means "unset": default on.
    monkeypatch.setenv("SIXTA_BADGE", "")
    assert sr.build_parser().parse_args([]).badge is True
