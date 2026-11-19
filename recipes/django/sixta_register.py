"""Tell SIXTA when `manage.py migrate` applied migrations (a sixta-ci recipe).

Where it goes: copy this file into one of your Django apps (say
shop/sixta_register.py) and connect it from that app's AppConfig.ready():

    class ShopConfig(AppConfig):
        name = "shop"

        def ready(self):
            from . import sixta_register

            sixta_register.connect()

Prefer not to add a receiver to every process? Leave ready() alone and copy
sixta_migrate.py (beside this file) to shop/management/commands/ instead: it
connects these receivers for a `manage.py sixta_migrate` run and disconnects
them when the run ends. Importing this file connects nothing.

What it does: one registration per `migrate` run that applied something, with
the run's start and finish and the SQL Django renders for each migration it
applied (what `sqlmigrate` prints; a RunPython step renders no SQL).

Configuration, all from the environment of the process that runs migrate:

    SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = this does nothing)
    SIXTA_CHANGE_TOKEN     the bearer secret, from your secret store
    SIXTA_CHANGE_DATABASE  the database's name in SIXTA, for the `default` alias
    SIXTA_REGISTER         the command, default "<this python> sixta_review.py register"

More than one database: settings.SIXTA_CHANGE_DATABASES = {"default": "orders-main"}
maps a Django alias to its SIXTA name; an alias with no name is not registered.
Set SIXTA_CHANGE_URL only where the real migration runs, not in the test job:
the test runner migrates its own scratch databases.

A failure here never fails migrate: the command exits zero whatever happens,
and anything raised in this file is caught and written as one line.
"""

import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from django.conf import settings
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.db.models.signals import post_migrate, pre_migrate

_started = {}
_connected = False


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note_start(sender, using, plan=None, **kwargs):
    # pre_migrate fires once per app before anything is applied: keep the
    # first of this run. A start left behind by a run that raised belongs to
    # another plan and is replaced.
    noted = _started.get(using)
    if noted is None or noted[1] is not plan:
        _started[using] = (_now(), plan)


def _register(sender, using, plan=None, **kwargs):
    # post_migrate fires once per app with the same plan: the first one registers.
    started, _ = _started.pop(using, (None, None))
    if started is None or not plan or not os.environ.get("SIXTA_CHANGE_URL"):
        return
    try:
        database = getattr(settings, "SIXTA_CHANGE_DATABASES", {}).get(using)
        if not database and using == "default":
            database = os.environ.get("SIXTA_CHANGE_DATABASE")
        if not database:
            return
        finished = _now()
        loader = MigrationLoader(connections[using])
        sql = []
        for migration, backwards in plan:
            # One migration at a time, as sqlmigrate renders it.
            sql.extend(loader.collect_sql([(migration, backwards)]))
        names = [f"{m.app_label}.{m.name}" for m, _ in plan]
        subject = names[0] if len(names) == 1 else f"{len(names)} Django migrations"
        command = shlex.split(os.environ.get("SIXTA_REGISTER", "")) or [sys.executable, "sixta_review.py", "register"]
        subprocess.run(
            command + ["--database", database, "--tool", "django", "--subject", subject, "--revision", names[-1],
                       "--started-at", started, "--finished-at", finished, "--file", "-"],
            input="\n".join(sql).encode(), timeout=60, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - registering must never fail the migration
        print(f"sixta-register: not registered, and the migration is unaffected: {type(exc).__name__}", file=sys.stderr)


def connect():
    """Connect the receivers. Call it from AppConfig.ready() to register every
    migrate this process runs; the sixta_migrate command calls it for its own
    run."""
    global _connected
    pre_migrate.connect(_note_start, dispatch_uid="sixta_register_start")
    post_migrate.connect(_register, dispatch_uid="sixta_register_finish")
    _connected = True


def disconnect():
    global _connected
    pre_migrate.disconnect(_note_start, dispatch_uid="sixta_register_start")
    post_migrate.disconnect(_register, dispatch_uid="sixta_register_finish")
    _started.clear()
    _connected = False


def connected():
    return _connected
