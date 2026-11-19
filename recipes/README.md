# Recipes: tell SIXTA when a migration ran

Each file here wires one tool to `sixta_review.py register` (see "Telling
SIXTA a change ran" in the [README](../README.md)). Copy the one you need; its
header says where it goes.

| You run migrations with | Copy | It registers |
|---|---|---|
| Flyway inside a JVM service (Spring Boot) | [`flyway/SixtaRegisterCallback.java`](flyway/SixtaRegisterCallback.java) | one change per migration, from `afterEachMigrate`, posted at `afterMigrate` |
| The Flyway command line | [`flyway/flyway-migrate.sh`](flyway/flyway-migrate.sh) | one change per migration the run applied (by name and version only where the script has a `${placeholder}`) |
| Alembic | [`alembic/env_hook.py`](alembic/env_hook.py) | one change per `alembic upgrade` that moved the revision |
| Django | [`django/sixta_register.py`](django/sixta_register.py), and [`django/sixta_migrate.py`](django/sixta_migrate.py) for the command wrapper | one change per `migrate` run that applied something |
| Liquibase | [`liquibase/liquibase-update.sh`](liquibase/liquibase-update.sh) | one change per `liquibase update` that applied something |
| `psql` / `mysql` in a pipeline step | [`sql/run-sql.sh`](sql/run-sql.sh) | the SQL file the step ran |
| A coding agent | [`agent/AGENTS.md`](agent/AGENTS.md) | what the agent applied, as the step after it |

All of them share four settings, read from the environment of the process
that runs the migration:

- `SIXTA_CHANGE_URL`: the base URL of your SIXTA backend. Unset, every recipe
  does nothing.
- `SIXTA_CHANGE_TOKEN`: the bearer secret, from your secret store. Never a
  flag, never a file in the repository.
- `SIXTA_CHANGE_DATABASE`: the database's name as SIXTA knows it.
- `SIXTA_REGISTER`: the command to run, when it is not
  `python3 sixta_review.py register` in the working directory (for example
  `python3 /opt/sixta/sixta_review.py register`, or `sixta-review register`
  when the kit is pip-installed).

None of them can fail your migration: the command always exits zero, the
shell recipes return your tool's exit code, and the hooks catch anything they
raise.
