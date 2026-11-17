"""The recipes under recipes/, each run with its real tool against a local
stand-in for the backend's register route: the tool applies a migration to a
scratch SQLite file, and the recipe must register one change with the right
times. No network, no real backend, no database server.

A tool that is not installed skips its test. SIXTA_RECIPES_REQUIRE (a
comma-separated list of django, alembic, sqlite3, liquibase, flyway, javac)
turns a missing tool into a failure, so CI cannot pass by skipping.
"""

import glob
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from conftest import REGISTER_FIELDS, REGISTER_TOKEN, StubRegisterHandler, run_register_stub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES = os.path.join(ROOT, "recipes")
SCRIPT = os.path.join(ROOT, "sixta_review.py")
REQUIRED = {t.strip() for t in os.environ.get("SIXTA_RECIPES_REQUIRE", "").split(",") if t.strip()}


def _need(tool, present):
    if present:
        return
    if tool in REQUIRED:
        pytest.fail(f"{tool} is required by SIXTA_RECIPES_REQUIRE but is not installed")
    pytest.skip(f"{tool} is not installed")


def _need_module(name):
    try:
        __import__(name)
    except ImportError:
        _need(name, False)


def _need_cli(name):
    _need(name, shutil.which(name) is not None)


@pytest.fixture
def backend():
    yield from run_register_stub()


@pytest.fixture
def env(backend):
    """The environment a pipeline gives the migration step."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("SIXTA_")}
    e.update(
        SIXTA_CHANGE_URL=backend, SIXTA_CHANGE_TOKEN=REGISTER_TOKEN, SIXTA_CHANGE_DATABASE="orders-main",
        SIXTA_REGISTER=f"{sys.executable} {SCRIPT} register",
    )
    return e


def _run(cmd, cwd, env, expect=0):
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == expect, f"{cmd} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert REGISTER_TOKEN not in proc.stdout + proc.stderr
    return proc


def _now():
    return datetime.now(timezone.utc)


def _instant(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _the_one_change(before, after, tool):
    """Exactly one registration arrived, in the route's shape, timed inside
    the window the tool ran in."""
    assert len(StubRegisterHandler.calls) == 1, StubRegisterHandler.calls
    return _check(StubRegisterHandler.calls[0], before, after, tool)


def _check(call, before, after, tool):
    assert call["auth"] == "Bearer " + REGISTER_TOKEN
    body = call["body"]
    assert set(body) <= REGISTER_FIELDS
    assert body["database"] == "orders-main" and body["plane"] == "schema" and body["tool"] == tool
    assert body["idempotency_key"]
    started, finished = _instant(body["started_at"]), _instant(body["finished_at"])
    slack = timedelta(seconds=1)  # the recipes state whole seconds
    assert before - slack <= started <= finished <= after + slack
    return body


# --------------------------------------------------------------------------
# Django: the post_migrate receiver and the management-command wrapper
# --------------------------------------------------------------------------

DJANGO_MIGRATION = '''
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("status", models.CharField(max_length=20)),
            ],
        ),
        migrations.AddIndex(model_name="order", index=models.Index(fields=["status"], name="shop_order_status_idx")),
    ]
'''


def _django_project(tmp_path, receiver_in_ready):
    (tmp_path / "manage.py").write_text(textwrap.dedent('''
        import os, sys
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
        from django.core.management import execute_from_command_line
        execute_from_command_line(sys.argv)
    '''))
    (tmp_path / "settings.py").write_text(textwrap.dedent('''
        SECRET_KEY = "not-secret"
        INSTALLED_APPS = ["shop.apps.ShopConfig"]
        DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}
        DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
        USE_TZ = True
    '''))
    shop = tmp_path / "shop"
    (shop / "migrations").mkdir(parents=True)
    (shop / "management" / "commands").mkdir(parents=True)
    for pkg in (shop, shop / "migrations", shop / "management", shop / "management" / "commands"):
        (pkg / "__init__.py").write_text("")
    ready = "    def ready(self):\n        from . import sixta_register  # noqa: F401\n" if receiver_in_ready else ""
    (shop / "apps.py").write_text(
        "from django.apps import AppConfig\n\n\nclass ShopConfig(AppConfig):\n    name = \"shop\"\n" + ready)
    (shop / "models.py").write_text(textwrap.dedent('''
        from django.db import models


        class Order(models.Model):
            status = models.CharField(max_length=20)

            class Meta:
                indexes = [models.Index(fields=["status"], name="shop_order_status_idx")]
    '''))
    (shop / "migrations" / "0001_initial.py").write_text(DJANGO_MIGRATION)
    shutil.copy(os.path.join(RECIPES, "django", "sixta_register.py"), shop / "sixta_register.py")
    shutil.copy(os.path.join(RECIPES, "django", "sixta_migrate.py"), shop / "management" / "commands" / "sixta_migrate.py")


def _check_django(body):
    assert body["subject"] == "shop.0001_initial" and body["revision"] == "shop.0001_initial"
    joined = "\n".join(body["statements"])
    assert 'CREATE TABLE "shop_order"' in joined
    assert 'CREATE INDEX "shop_order_status_idx"' in joined
    assert "django_migrations" not in joined  # Django's own bookkeeping is not a change


def test_django_post_migrate_receiver_registers_the_run(tmp_path, env):
    _need_module("django")
    _django_project(tmp_path, receiver_in_ready=True)
    before = _now()
    proc = _run([sys.executable, "manage.py", "migrate"], tmp_path, env)
    _check_django(_the_one_change(before, _now(), "django"))
    assert "SIXTA recorded the change on orders-main" in proc.stderr
    # A second migrate applies nothing, so it registers nothing.
    _run([sys.executable, "manage.py", "migrate"], tmp_path, env)
    assert len(StubRegisterHandler.calls) == 1


def test_django_management_command_wrapper_registers_the_run(tmp_path, env):
    _need_module("django")
    _django_project(tmp_path, receiver_in_ready=False)
    before = _now()
    _run([sys.executable, "manage.py", "sixta_migrate"], tmp_path, env)
    _check_django(_the_one_change(before, _now(), "django"))


def test_django_plain_migrate_registers_nothing_without_the_receiver(tmp_path, env):
    # The wrapper is for projects that do not want the receiver on every process.
    _need_module("django")
    _django_project(tmp_path, receiver_in_ready=False)
    _run([sys.executable, "manage.py", "migrate"], tmp_path, env)
    assert StubRegisterHandler.calls == []


def test_django_migrate_succeeds_when_the_backend_is_unreachable(tmp_path, env):
    _need_module("django")
    _django_project(tmp_path, receiver_in_ready=True)
    env["SIXTA_CHANGE_URL"] = "http://127.0.0.1:9"
    proc = _run([sys.executable, "manage.py", "migrate"], tmp_path, env)  # exit 0: the migration is unaffected
    assert "not registered" in proc.stderr
    assert os.path.exists(tmp_path / "db.sqlite3")


def test_django_migrate_is_untouched_when_nothing_is_configured(tmp_path, env):
    _need_module("django")
    _django_project(tmp_path, receiver_in_ready=True)
    for name in ("SIXTA_CHANGE_URL", "SIXTA_CHANGE_TOKEN"):
        env.pop(name)
    proc = _run([sys.executable, "manage.py", "migrate"], tmp_path, env)
    assert StubRegisterHandler.calls == [] and "sixta" not in proc.stderr.lower()


# --------------------------------------------------------------------------
# Alembic: the env.py hook around run_migrations
# --------------------------------------------------------------------------

ALEMBIC_ENV_TAIL = '''
from sqlalchemy import create_engine

config = context.config


def run_migrations_online():
    connectable = create_engine(config.get_main_option("sqlalchemy.url"))
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with sixta_registering(connection):
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
'''

ALEMBIC_REVISION = '''
import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4"
down_revision = None


def upgrade():
    op.create_table("orders", sa.Column("id", sa.Integer, primary_key=True), sa.Column("status", sa.String(20)))
    op.create_index("idx_orders_status", "orders", ["status"])
    op.execute(sa.text("UPDATE orders SET status = :s WHERE status IS NULL").bindparams(s="canary-value-9f3a"))


def downgrade():
    op.drop_table("orders")
'''


def _alembic_project(tmp_path):
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = migrations\nsqlalchemy.url = sqlite:///db.sqlite3\n")
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    with open(os.path.join(RECIPES, "alembic", "env_hook.py"), encoding="utf-8") as fh:
        hook = fh.read()
    (tmp_path / "migrations" / "env.py").write_text(hook + ALEMBIC_ENV_TAIL)  # the recipe, pasted as its header says
    (tmp_path / "migrations" / "script.py.mako").write_text("")
    (tmp_path / "migrations" / "versions" / "a1b2c3d4_orders.py").write_text(ALEMBIC_REVISION)


def test_alembic_env_hook_registers_the_upgrade(tmp_path, env):
    _need_module("alembic")
    _alembic_project(tmp_path)
    before = _now()
    proc = _run([sys.executable, "-m", "alembic", "upgrade", "head"], tmp_path, env)
    body = _the_one_change(before, _now(), "alembic")
    assert body["revision"] == "a1b2c3d4" and body["subject"] == "alembic revision a1b2c3d4"
    joined = "\n".join(body["statements"])
    assert "CREATE TABLE orders" in joined and "CREATE INDEX idx_orders_status" in joined
    assert "UPDATE orders SET status" in joined
    assert "canary-value-9f3a" not in joined  # a bind parameter's value is never read
    assert "alembic_version" not in joined    # Alembic's own bookkeeping is not a change
    assert "SIXTA recorded the change on orders-main" in proc.stderr
    # Already at head: nothing is applied, nothing is registered.
    _run([sys.executable, "-m", "alembic", "upgrade", "head"], tmp_path, env)
    assert len(StubRegisterHandler.calls) == 1


def test_alembic_upgrade_succeeds_when_the_backend_is_unreachable(tmp_path, env):
    _need_module("alembic")
    _alembic_project(tmp_path)
    env["SIXTA_CHANGE_URL"] = "http://127.0.0.1:9"
    proc = _run([sys.executable, "-m", "alembic", "upgrade", "head"], tmp_path, env)
    assert "not registered" in proc.stderr
    current = _run([sys.executable, "-m", "alembic", "current"], tmp_path, env)
    assert "a1b2c3d4" in current.stdout  # the migration itself went through


def test_alembic_offline_render_registers_nothing(tmp_path, env):
    _need_module("alembic")
    _alembic_project(tmp_path)
    _run([sys.executable, "-m", "alembic", "upgrade", "head", "--sql"], tmp_path, env)
    assert StubRegisterHandler.calls == []


# --------------------------------------------------------------------------
# Plain SQL in a pipeline step
# --------------------------------------------------------------------------

CHANGE_SQL = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);\nCREATE INDEX idx_orders_status ON orders (status);\n"


def test_plain_sql_step_registers_the_file(tmp_path, env):
    _need_cli("sqlite3")
    (tmp_path / "change.sql").write_text(CHANGE_SQL)
    env["GITHUB_SHA"] = "0cd1ae7"
    before = _now()
    _run(["sh", os.path.join(RECIPES, "sql", "run-sql.sh"), "change.sql",
          "sqlite3", "-bail", "db.sqlite3", ".read change.sql"], tmp_path, env)
    body = _the_one_change(before, _now(), "sqlite3")
    assert body["statements"] == [
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)",
        "CREATE INDEX idx_orders_status ON orders (status)",
    ]
    assert body["subject"] == "change.sql" and body["revision"] == "0cd1ae7"


def test_plain_sql_step_keeps_a_failing_commands_exit_code_and_registers_nothing(tmp_path, env):
    (tmp_path / "change.sql").write_text(CHANGE_SQL)
    _run(["sh", os.path.join(RECIPES, "sql", "run-sql.sh"), "change.sql", "sh", "-c", "exit 3"], tmp_path, env, expect=3)
    assert StubRegisterHandler.calls == []


def test_plain_sql_step_succeeds_when_the_backend_is_unreachable(tmp_path, env):
    (tmp_path / "change.sql").write_text(CHANGE_SQL)
    env["SIXTA_CHANGE_URL"] = "http://127.0.0.1:9"
    proc = _run(["sh", os.path.join(RECIPES, "sql", "run-sql.sh"), "change.sql", "true"], tmp_path, env)
    assert "not registered" in proc.stderr


# --------------------------------------------------------------------------
# Liquibase: the CLI wrapper
# --------------------------------------------------------------------------

LIQUIBASE_CHANGELOG = """--liquibase formatted sql

--changeset dba:1
CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);

--changeset dba:2
CREATE INDEX idx_orders_status ON orders (status);
"""


def test_liquibase_wrapper_registers_the_update(tmp_path, env):
    _need_cli("liquibase")
    (tmp_path / "changelog.sql").write_text(LIQUIBASE_CHANGELOG)
    args = ["sh", os.path.join(RECIPES, "liquibase", "liquibase-update.sh"),
            "--changelog-file=changelog.sql", f"--url=jdbc:sqlite:{tmp_path / 'db.sqlite3'}"]
    before = _now()
    _run(args, tmp_path, env)
    body = _the_one_change(before, _now(), "liquibase")
    joined = "\n".join(body["statements"])
    assert "CREATE TABLE orders" in joined and "CREATE INDEX idx_orders_status" in joined
    assert "DATABASECHANGELOG" not in joined.upper()  # Liquibase's own bookkeeping is not a change
    # Nothing pending: the update applies nothing and registers nothing.
    _run(args, tmp_path, env)
    assert len(StubRegisterHandler.calls) == 1


# --------------------------------------------------------------------------
# Flyway: the command-line wrapper and the Java callback
# --------------------------------------------------------------------------

def _flyway_project(tmp_path):
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "V1__create_orders.sql").write_text("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);\n")
    (sql / "V2__add_status_index.sql").write_text("CREATE INDEX idx_orders_status ON orders (status);\n")
    return [f"-url=jdbc:sqlite:{tmp_path / 'db.sqlite3'}", f"-locations=filesystem:{sql}"]


def _check_flyway(before, after):
    assert len(StubRegisterHandler.calls) == 2, StubRegisterHandler.calls  # one change per migration
    first, second = (_check(c, before, after, "flyway") for c in StubRegisterHandler.calls)
    assert (first["subject"], first["revision"]) == ("V1__create_orders.sql", "1")
    assert (second["subject"], second["revision"]) == ("V2__add_status_index.sql", "2")
    assert "CREATE TABLE orders" in "\n".join(first["statements"])
    assert "CREATE INDEX idx_orders_status" in "\n".join(second["statements"])
    assert first["idempotency_key"] != second["idempotency_key"]
    for body in (first, second):
        assert "flyway_schema_history" not in "\n".join(body["statements"])


def test_flyway_cli_wrapper_registers_each_migration(tmp_path, env):
    _need_cli("flyway")
    args = ["sh", os.path.join(RECIPES, "flyway", "flyway-migrate.sh"), *_flyway_project(tmp_path)]
    before = _now()
    _run(args, tmp_path, env)
    _check_flyway(before, _now())
    # Nothing pending: the run applies nothing and registers nothing.
    _run(args, tmp_path, env)
    assert len(StubRegisterHandler.calls) == 2


def _flyway_core_jar():
    home = os.path.dirname(os.path.realpath(shutil.which("flyway")))
    jars = glob.glob(os.path.join(home, "lib", "**", "flyway-core-*.jar"), recursive=True)
    assert jars, f"no flyway-core jar under {home}/lib"
    return jars[0]


def test_flyway_java_callback_registers_each_migration(tmp_path, env):
    _need_cli("flyway")
    _need_cli("javac")
    _need_cli("jar")
    classes, jars = tmp_path / "classes", tmp_path / "jars"
    classes.mkdir()
    jars.mkdir()
    _run(["javac", "-cp", _flyway_core_jar(), "-d", str(classes),
          os.path.join(RECIPES, "flyway", "SixtaRegisterCallback.java")], tmp_path, env)
    _run(["jar", "cf", str(jars / "sixta-register-callback.jar"), "-C", str(classes), "."], tmp_path, env)
    before = _now()
    _run(["flyway", *_flyway_project(tmp_path), f"-jarDirs={jars}",
          "-callbacks=db.callback.SixtaRegisterCallback", "migrate"], tmp_path, env)
    _check_flyway(before, _now())


def test_flyway_java_callback_leaves_the_migration_alone_when_the_backend_is_unreachable(tmp_path, env):
    _need_cli("flyway")
    _need_cli("javac")
    _need_cli("jar")
    classes, jars = tmp_path / "classes", tmp_path / "jars"
    classes.mkdir()
    jars.mkdir()
    _run(["javac", "-cp", _flyway_core_jar(), "-d", str(classes),
          os.path.join(RECIPES, "flyway", "SixtaRegisterCallback.java")], tmp_path, env)
    _run(["jar", "cf", str(jars / "sixta-register-callback.jar"), "-C", str(classes), "."], tmp_path, env)
    env["SIXTA_CHANGE_URL"] = "http://127.0.0.1:9"
    proc = _run(["flyway", *_flyway_project(tmp_path), f"-jarDirs={jars}",
                 "-callbacks=db.callback.SixtaRegisterCallback", "migrate"], tmp_path, env)  # exit 0
    assert "not registered" in proc.stdout + proc.stderr
