"""`manage.py sixta_migrate`: migrate, then tell SIXTA what was applied
(a sixta-ci recipe).

Where it goes: copy this file to <your app>/management/commands/sixta_migrate.py
and sixta_register.py (beside this file) to <your app>/sixta_register.py. Do
not import sixta_register from AppConfig.ready(): this command connects its
receivers for its own run only, so a plain `manage.py migrate` (the test
runner's included) registers nothing.

It takes every option `migrate` takes and returns what `migrate` returns.
Configuration is in sixta_register.py's header.
"""

from django.core.management.commands.migrate import Command as MigrateCommand


class Command(MigrateCommand):
    help = "Runs migrate, then registers what it applied with SIXTA."

    def handle(self, *args, **options):
        from ... import sixta_register  # noqa: F401 - importing it connects the receivers

        return super().handle(*args, **options)
