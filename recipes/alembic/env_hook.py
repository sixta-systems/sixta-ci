# Tell SIXTA when `alembic upgrade` (or downgrade) applied revisions
# (a sixta-ci recipe).
#
# Where it goes: paste this block into your Alembic env.py, above
# run_migrations_online(), and wrap the migration run in it:
#
#     with connectable.connect() as connection:
#         context.configure(connection=connection, target_metadata=target_metadata)
#         with sixta_registering(connection):          # <- the one added line
#             with context.begin_transaction():
#                 context.run_migrations()
#
# What it does: one registration per run that moved the database's revision,
# with the run's start and finish, the revision it ended on, and the
# statements Alembic executed, as text. Bind parameters are never read, and
# reads (SELECT, PRAGMA, SHOW) are not sent. The post happens after the
# migration's transaction has ended, so it never holds a lock open. A run that
# raises, an offline run (--sql) and a run that applied nothing register
# nothing.
#
# Configuration, from the environment of the process that runs alembic:
#
#     SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = this does nothing)
#     SIXTA_CHANGE_TOKEN     the bearer secret, from your secret store
#     SIXTA_CHANGE_DATABASE  the database's name in SIXTA
#     SIXTA_REGISTER         the command, default "<this python> sixta_review.py register"
#
# A failure here never fails the migration: the command exits zero whatever
# happens, and anything raised in the hook is caught and written as one line.

import contextlib
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from alembic import context
from sqlalchemy import event


@contextlib.contextmanager
def sixta_registering(connection):
    if not os.environ.get("SIXTA_CHANGE_URL"):
        yield
        return

    def now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def capture(conn, cursor, statement, parameters, execution_context, executemany):
        if not statement.lstrip().upper().startswith(("SELECT", "PRAGMA", "SHOW")):
            statements.append(statement)  # the text only: parameters are never read

    statements = []
    started = now()
    before = context.get_context().get_current_heads()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    try:
        after = context.get_context().get_current_heads()
        if after == before:
            return  # nothing was applied
        revision = ",".join(after) or "base"
        command = shlex.split(os.environ.get("SIXTA_REGISTER", "")) or [sys.executable, "sixta_review.py", "register"]
        subprocess.run(
            command + ["--tool", "alembic", "--subject", f"alembic revision {revision}", "--revision", revision,
                       "--started-at", started, "--finished-at", now(), "--file", "-"],
            input=";\n".join(statements).encode(), timeout=60, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - registering must never fail the migration
        print(f"sixta-register: not registered, and the migration is unaffected: {type(exc).__name__}", file=sys.stderr)
