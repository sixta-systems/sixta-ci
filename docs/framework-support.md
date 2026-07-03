# Migration-framework support plan (SIXTA CI kit)

Extend the kit beyond Django + `.sql` to code/DSL migrations from the ORMs whose
migrations are code, not SQL. Companion to the server-side
`docs/ci-protocol-plan.md` §4 (client kits) in the sixta-connect repo.

## Where we are today
Supported now: **Django** (rendered via `manage.py sqlmigrate`) and **any `.sql`
migration file** — which already covers Flyway, Liquibase-SQL changelogs, Prisma,
golang-migrate, dbmate, Sqitch, Atlas versioned SQL, and the Supabase CLI. The
gap is ORM/DSL migrations that don't ship SQL.

## Principles (what makes this cheap)
- **The server (`/v1/analyze`) is frozen.** It analyzes any SQL; a new framework
  never touches it. All work is a client-side *extraction step* in this kit.
- **DB flavor is not a variable.** MySQL / MariaDB / Percona / TiDB-as-MySQL →
  `engine: mysql`; Postgres / Supabase → `engine: postgresql`. The framework is
  the only new axis. (TiDB's DDL semantics are the exception — see below.)
- **Each framework is an additive, independently shippable unit.**

## Extraction mechanisms
- **A — offline render** (a CLI prints SQL without applying it): the `sqlmigrate`
  pattern. **Alembic** (`alembic upgrade <range> --sql`), **Liquibase**
  (`updateSQL` in offline mode), **Laravel** (`migrate --pretend`).
- **B — replay-with-logging** (run against a throwaway DB, capture the SQL):
  **Rails**, **Sequelize**, possibly **Phinx**. R&D-heavy; note the MySQL
  implicit-commit-on-DDL gotcha (rollback-based capture doesn't work on MySQL).
- **C — static extract** (SQL is literal in the file): **TypeORM**
  (`queryRunner.query("…")`).

## TiDB caveat
TiDB does online, non-blocking DDL (distributed state machine, no table locks) —
fundamentally unlike MySQL's metadata-lock / INSTANT-INPLACE-COPY model, so our
MySQL verdicts would be *wrong* for TiDB. Claiming TiDB support requires a
distinct engine/analysis mode (server-side), not just `engine: mysql`.

## Phasing
| Phase | Framework | Mechanism | Status |
|---|---|---|---|
| 0 | registry refactor (`extract_migration` dispatch, `is_migration_file`) | — | **DONE** (existing suite green) |
| 1 | **Alembic** | A (`--sql`) | **DONE on `main`** — detection + offline render + data-op flag + 17 tests. Not released until the next tag. |
| 2 | **Liquibase** (XML/YAML/JSON changelogs) | A (`updateSQL` offline) | build-ready; promote if Spring Boot uses Liquibase |
| 3 | **CakePHP / Phinx** | A dry-run is flaky → likely B | **gated** on a representative real-world sample (dry-run reliability unknown) |
| 4 | Flyway | already `.sql` | document + market as already-supported (Spring-Boot-Flyway services need nothing) |
| — | Laravel (A `--pretend`), TypeORM (C), Rails (B spike), **TiDB engine mode** (server-side) | | later / gated |

## What we're proceeding with now
- **Phase 0** registry + **Phase 1 Alembic**: clear ROI, and Alembic proves the
  registry pattern for CakePHP/Liquibase to follow.
- **Not now:** Phinx (needs a representative sample to size the dry-run risk),
  the Liquibase-vs-Flyway priority for Spring (pending real-world demand
  signal), TiDB (pending a committed timeline), Rails (own spike).

## Open decisions
Auto-detect framework vs explicit input · data-migration flag-only vs `kind:"code"`
model review · Rails-now vs TypeORM-first · forward-only vs also down-migrations ·
Jenkins support priority.

## Per-framework deliverables (repeatable)
detect + render · stub-server tests (no network) · a demo app under `example/` · a
CI snippet in the README and on `/ci` · a truthful landing "supported" line.

## Release discipline
New frameworks land on `main` but are **not** in users' hands until a new tag is
cut and `v1` is moved forward. The landing/`/ci` "supported" copy updates only
when released — never ahead of a tag.
