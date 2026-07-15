# Migration-framework support plan (SIXTA CI kit)

Extend the kit beyond Django + `.sql` to code/DSL migrations from the ORMs whose
migrations are code, not SQL. Companion to the server-side
`docs/ci-protocol-plan.md` §4 (client kits) in the sixta-connect repo.

## Where we are today (v0.4.1, released 2026-07-15)
Supported now: **Django** (`manage.py sqlmigrate`), **Alembic** (offline
`upgrade --sql`), **Flyway** with full conventions (V/R/U naming,
`${placeholder}` substitution, undo files feeding the rollback audit,
Java-based migrations flagged), **Liquibase** in both styles (formatted SQL
parsed statically with new-changeset-vs-base diffing; XML/YAML/JSON rendered
offline via the CLI), **any `.sql` migration file** (Prisma, golang-migrate,
dbmate, Sqitch, Atlas versioned SQL, Supabase CLI), **native SQL embedded in
Java** (`@Query(nativeQuery)`, Spring Data JDBC/R2DBC, JdbcTemplate/JdbcClient,
MyBatis mappers with `${}` injection flagging), plus **JPA ddl-auto detection**
and **engine auto-detection** from build/config files. Design + phasing:
`docs/spring-boot-support.md`. The remaining gap is ORM/DSL migrations that
don't ship SQL (PHP/Ruby/JS ecosystems).

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
  (`queryRunner.query("…")`). Proven at scale by the Java/MyBatis extractor
  (v0.4.0): comment-aware scanning, constant resolution, and skip-don't-guess
  on dynamic assembly are the reusable patterns.

## TiDB caveat
TiDB does online, non-blocking DDL (distributed state machine, no table locks) —
fundamentally unlike MySQL's metadata-lock / INSTANT-INPLACE-COPY model, so our
MySQL verdicts would be *wrong* for TiDB. Claiming TiDB support requires a
distinct engine/analysis mode (server-side), not just `engine: mysql`.

## Phasing
| Phase | Framework | Mechanism | Status |
|---|---|---|---|
| 0 | registry refactor (`extract_migration` dispatch, `is_migration_file`) | — | **SHIPPED** (v0.2.0) |
| 1 | **Alembic** | A (`--sql`) | **SHIPPED** (v0.2.0) |
| 2 | **Liquibase** (formatted SQL + XML/YAML/JSON) | static + A (`update-sql` offline) | **SHIPPED** (v0.4.0; real-CLI verified — offline runs need a throwaway state CSV, rollback = `changelog-sync` + `rollback-count-sql`) |
| 3 | **CakePHP / Phinx** | A dry-run is flaky → likely B | **gated** on a representative real-world sample (dry-run reliability unknown) |
| 4 | **Flyway** (full conventions, not just `.sql`) | static | **SHIPPED** (v0.4.0) |
| 5 | **Java-embedded SQL + MyBatis** | C | **SHIPPED** (v0.4.0) |
| 6 | **JPA ddl-auto detection** + engine auto-detect | static | **SHIPPED** (v0.4.0) |
| — | Laravel (A `--pretend`), TypeORM (C), Rails (B spike), **TiDB engine mode** (server-side) | | later / gated |

## What's next
- **Phinx (CakePHP)** stays first in line, still gated on a representative
  sample to size the dry-run reliability risk.
- **TypeORM** is the natural next mechanism-C target: the Java extractor's
  patterns (string scanning, constant resolution, skip-don't-guess) transfer
  directly to `queryRunner.query("…")`.
- **Not now:** TiDB (pending a committed timeline; needs a server-side engine
  mode, see the caveat above), Rails (own spike), Laravel (no demand signal).

## Open decisions
Data-migration flag-only vs `kind:"code"` model review · Rails-now vs
TypeORM-first · Jenkins support priority. Resolved by shipping: framework
auto-detect (content-sniffing discovery + engine auto-detect, v0.4.0) and
down-migrations (the rollback audit covers undo files, `--rollback`
changesets, and offline rollback renders).

## Per-framework deliverables (repeatable)
detect + render · stub-server tests (no network) · a demo app under `example/` · a
CI snippet in the README and on `/ci` · a truthful landing "supported" line.

## Release discipline
New frameworks land on `main` but are **not** in users' hands until a new tag is
cut and `v1` is moved forward. The landing/`/ci` "supported" copy updates only
when released — never ahead of a tag.
