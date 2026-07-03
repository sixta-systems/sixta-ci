"""Tests for Alembic (SQLAlchemy) offline-render support (no alembic needed)."""

import types

import pytest

import sixta_review as sr


MIGRATION = '''"""add email"""
revision = "abc123"
down_revision = "def456"

def upgrade():
    op.add_column("users", sa.Column("email", sa.String()))
'''

BASE = 'revision = "aaa"\ndown_revision = None\n\ndef upgrade():\n    op.create_table("t")\n'
TYPED = 'revision: str = "r1"\ndown_revision: Union[str, None] = "r0"\n\ndef upgrade():\n    pass\n'
MERGE = 'revision = "m1"\ndown_revision = ("a", "b")\n\ndef upgrade():\n    pass\n'
DATA = 'revision = "d1"\ndown_revision = "d0"\n\ndef upgrade():\n    conn = op.get_bind()\n    op.bulk_insert(tbl, [{"x": 1}])\n'


# --- detection --------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("alembic/versions/abc_add_email.py", True),
    ("migrations/versions/0001_init.py", True),
    ("src/db/migrations/versions/xyz.py", True),
    ("alembic/versions/__init__.py", False),
    ("shop/migrations/0002_add.py", False),   # Django, not Alembic
    ("migrations/0001_versions.py", False),    # no /versions/ segment
    ("schema.sql", False),
])
def test_alembic_target(path, expected):
    assert sr.alembic_target(path) is expected


def test_is_migration_file_covers_all_paths():
    assert sr.is_migration_file("alembic/versions/x.py")     # alembic
    assert sr.is_migration_file("shop/migrations/0002.py")   # django
    assert sr.is_migration_file("db/V1__init.sql")           # raw sql
    assert not sr.is_migration_file("shop/models.py")


# --- revision parsing -------------------------------------------------------

def test_alembic_revisions_parses(tmp_path):
    f = tmp_path / "m.py"; f.write_text(MIGRATION)
    assert sr._alembic_revisions(str(f)) == ("abc123", "def456")


def test_alembic_revisions_base_when_down_none(tmp_path):
    f = tmp_path / "m.py"; f.write_text(BASE)
    assert sr._alembic_revisions(str(f)) == ("aaa", "base")


def test_alembic_revisions_handles_type_annotations(tmp_path):
    f = tmp_path / "m.py"; f.write_text(TYPED)
    assert sr._alembic_revisions(str(f)) == ("r1", "r0")


def test_alembic_revisions_merge_is_unsupported(tmp_path):
    f = tmp_path / "m.py"; f.write_text(MERGE)
    with pytest.raises(RuntimeError, match="merge"):
        sr._alembic_revisions(str(f))


# --- offline render ---------------------------------------------------------

def test_render_alembic_builds_the_offline_range(tmp_path, monkeypatch):
    f = tmp_path / "m.py"; f.write_text(MIGRATION)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ALTER TABLE users ADD COLUMN email varchar;", stderr="")

    monkeypatch.setattr(sr.subprocess, "run", fake_run)
    opts = sr.build_parser().parse_args([])
    sql = sr.render_alembic(str(f), opts)

    assert "ALTER TABLE users" in sql
    assert seen["cmd"] == ["alembic", "-c", "alembic.ini", "upgrade", "def456:abc123", "--sql"]


def test_render_alembic_failure_raises(tmp_path, monkeypatch):
    f = tmp_path / "m.py"; f.write_text(MIGRATION)
    monkeypatch.setattr(sr.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    opts = sr.build_parser().parse_args([])
    with pytest.raises(RuntimeError, match="alembic offline render"):
        sr.render_alembic(str(f), opts)


# --- data-migration flag ----------------------------------------------------

def test_alembic_data_ops_detects_data_migrations(tmp_path):
    d = tmp_path / "d.py"; d.write_text(DATA)
    n = tmp_path / "n.py"; n.write_text(MIGRATION)
    assert sr.alembic_data_ops(str(d)) is True
    assert sr.alembic_data_ops(str(n)) is False


# --- dispatch through extract_migration -------------------------------------

def test_extract_migration_dispatches_alembic(monkeypatch):
    monkeypatch.setattr(sr, "render_alembic", lambda path, opts: "CREATE INDEX i ON t (x);")
    opts = sr.build_parser().parse_args([])
    sql, manual = sr.extract_migration("alembic/versions/abc.py", opts)
    assert "CREATE INDEX" in sql
    assert manual is None


def test_extract_migration_flags_alembic_data_migration(monkeypatch):
    monkeypatch.setattr(sr, "render_alembic", lambda path, opts: "CREATE TABLE t (id int);")
    monkeypatch.setattr(sr, "alembic_data_ops", lambda path: True)
    opts = sr.build_parser().parse_args([])
    _sql, manual = sr.extract_migration("alembic/versions/abc.py", opts)
    assert manual is not None
    finding, section = manual
    assert finding.check_name == "data-migration-manual-review"
    assert "data-migration" in section.lower()
