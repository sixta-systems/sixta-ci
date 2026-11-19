"""`manage.py sixta_migrate`: migrate, then tell SIXTA what was applied
(a sixta-ci recipe).

Where it goes: copy this file to <your app>/management/commands/sixta_migrate.py
and sixta_register.py (beside this file) to <your app>/sixta_register.py. Leave
AppConfig.ready() alone: this command connects the receivers when it starts
and disconnects them when it ends, however it ends, so a plain
`manage.py migrate` registers nothing. That holds in one long-lived process
too: a `call_command("migrate")` after a `call_command("sixta_migrate")`
(a test runner, a worker) is not registered. If ready() does connect them,
this command leaves them as it found them.

It takes every option `migrate` takes and returns what `migrate` returns.
Configuration is in sixta_register.py's header.
"""

from django.core.management.commands.migrate import Command as MigrateCommand


class Command(MigrateCommand):
    help = "Runs migrate, then registers what it applied with SIXTA."

    def handle(self, *args, **options):
        from ... import sixta_register

        mine = not sixta_register.connected()
        if mine:
            sixta_register.connect()
        try:
            return super().handle(*args, **options)
        finally:
            if mine:
                sixta_register.disconnect()
