# sixta-ci

[![Reviewed by SIXTA](https://connect.sixta.ai/badge.svg)](https://connect.sixta.ai/ci?utm_source=badge&utm_medium=readme)

**DBRE-grade SQL review for Django, Alembic, and Flyway (Spring Boot) migrations
and `.sql` files, in your GitLab merge requests and GitHub pull requests.**

When an MR/PR adds or changes a migration (or a `.sql` file), the CI kit renders
it to SQL (Django via `manage.py sqlmigrate`, Alembic via offline
`alembic upgrade --sql`, Flyway and other `.sql` files read directly with
Flyway `${placeholder}` substitution), sends DDL to
[SIXTA Connect](https://sixta.ai)'s `sixta_analyze_schema_change` and DML to
`sixta_analyze_query`, and reports back:

- **inline diff findings**: a GitLab Code Quality artifact or a GitHub SARIF
  code-scanning upload,
- **a full report as an MR/PR comment** (upserted, never spammy),
- **a gate**: the job fails when any finding is at or above your threshold,
- the report as a job artifact / step summary.

The CI kit does not connect to your database. It sends the rendered SQL of changed
migrations to `connect.sixta.ai`, where it is analyzed in memory and your SQL is
not stored ([data handling](https://connect.sixta.ai/privacy)). To grade against
your real table sizes, add free `.sixta.yml` hints (below), or connect a read-only
database with Connect Pro.

> The kit is one stdlib-only Python file (`sixta_review.py`) plus thin platform
> wrappers, published from `sixta-systems/sixta-ci`.

## Quick start: GitLab

1. Get a free API key at `connect.sixta.ai/portal`, store it as a **masked**
   CI/CD variable `SIXTA_API_KEY` (Settings → CI/CD → Variables).
2. Optional, for MR comments: create a project access token (`api` scope,
   Reporter role) and store it as `SIXTA_BOT_TOKEN`. `CI_JOB_TOKEN` cannot post notes.
3. Add to `.gitlab-ci.yml` — as a [CI/CD component](https://gitlab.com/explore/catalog)
   (recommended; `@0.7` auto-picks-up patch releases):

```yaml
include:
  - component: gitlab.com/sixta-systems/sixta-ci/sixta-review@0.7
    inputs:
      engine_version: "16"                      # match production; verdicts are version-dependent
      setup: pip install -r requirements.txt    # whatever makes manage.py runnable
      allow_failure: true                       # soak period; remove to enforce
```

   Or, on self-managed instances without access to the gitlab.com catalog, as a
   remote include (GitLab ≥ 16.6):

```yaml
include:
  - remote: "https://raw.githubusercontent.com/sixta-systems/sixta-ci/v1/templates/sixta-review.yml"
    inputs:
      engine_version: "16"
```

4. Make sure **merge request pipelines** are running in your project. The job
   triggers on `merge_request_event` and never runs in branch-only pipelines.
   (This is the most common "it doesn't run" cause.)

## Quick start: GitHub Actions

1. Get a free API key at `connect.sixta.ai/portal` and add it as a repository
   **secret** `SIXTA_API_KEY` (Settings → Secrets and variables → Actions).
2. Add `.github/workflows/sixta.yml`:

```yaml
name: SIXTA SQL review
on:
  pull_request:
    paths: ["**/migrations/*.py", "**/*.sql"]
permissions:
  contents: read
  pull-requests: write      # PR comment
  security-events: write    # SARIF upload
jobs:
  sixta:
    runs-on: ubuntu-latest
    services:
      postgres:             # sqlmigrate needs a DB; an empty one is fine. Omit for .sql-only repos.
        image: postgres:16
        env: { POSTGRES_DB: sixta_ci, POSTGRES_USER: sixta_ci, POSTGRES_PASSWORD: sixta_ci }
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s --health-timeout 5s --health-retries 5
    env:
      SIXTA_API_KEY: ${{ secrets.SIXTA_API_KEY }}
      DATABASE_URL: postgres://sixta_ci:sixta_ci@localhost:5432/sixta_ci
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }        # the review diffs against the PR base
      - uses: sixta-systems/sixta-ci@v1
        with:
          engine_version: "16"          # match production
          setup: pip install -r requirements.txt
      - name: Upload SARIF
        if: always()
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: sixta.sarif
```

On GitHub the kit defaults to the batch `v1` API (one request per run,
server-rendered SARIF). A full-history checkout (`fetch-depth: 0`) is required so
it can diff against the PR base.

**Plain `.sql` migrations (Flyway / Prisma / Liquibase-SQL) need no database and
no Django.** When only `.sql` files change, the kit reads them directly, so skip
the `postgres` service, `DATABASE_URL`, and the `setup` input entirely.

**Alembic** migrations (`*/versions/*.py`) render **offline** via
`alembic upgrade <down>:<rev> --sql`, so they need Alembic installed (via `setup`)
but **no database**. Point at a non-default config with `--alembic-config` /
`SIXTA_ALEMBIC_CONFIG` (default `alembic.ini`). Data migrations (`op.bulk_insert`,
`op.get_bind`) don't render offline and are flagged for human review, like
Django's `RunPython`.

Self-hosted runners need outbound HTTPS to `connect.sixta.ai` and
`raw.githubusercontent.com`.

## Spring Boot: Flyway and Liquibase

Flyway migrations are plain `.sql` files, so a Spring Boot repo needs no
database, no JVM, and no `setup` input. The kit reads the changed files (any
location works: discovery is diff-driven, not directory-driven) and applies the
Flyway conventions:

- **Versioned (`V2__add_index.sql`) and repeatable (`R__views.sql`)** migrations
  are analyzed as schema changes.
- **`${placeholder}` tokens** are substituted before analysis: values come from
  `spring.flyway.placeholders.*` in `application.properties` or
  `application.yml` when present (`.yml` parsing needs PyYAML), else the
  placeholder's own name stands in.
- **Undo files (`U2__drop_index.sql`**, and generic `*.down.sql` /
  `*.rollback.sql` companions**)** feed the rollback audit of their forward
  migration and are no longer analyzed as forward changes themselves.
- **Java-based migrations** (`**/db/migration/**/V3__Backfill.java`) render no
  SQL offline and are flagged for human review, like Django's `RunPython`.
- **The engine is auto-detected** when `engine` is `auto` (the default): the
  Flyway per-DB module (`flyway-database-postgresql` / `flyway-mysql`), the
  JDBC driver artifact, or the datasource URL in `pom.xml`, `build.gradle`, or
  `application.*` names it. Fallback: postgresql.

**Liquibase** is supported in both changelog styles:

- **SQL-format changelogs** (`--liquibase formatted sql`) are parsed statically:
  no CLI and no database needed. Only changesets that are **new or edited vs
  the MR/PR base** are analyzed (changelogs are append-mostly, so old
  changesets are never re-graded), `--property` values substitute `${...}`,
  and inline `--rollback` SQL feeds the rollback audit. A new changeset without
  `--rollback` reports the change as `missing` a rollback.
- **XML/YAML/JSON changelogs** are rendered offline via the Liquibase CLI
  (`update-sql` against an `offline:<engine>` URL, no database). Install the
  CLI in the job (via `setup`) and point `liquibase_cmd` at it if it is not on
  `PATH`; without the CLI these files are reported as skipped, never silently
  passed. The rollback audit marks the changesets executed in a throwaway offline state and renders `rollback-count-sql` (offline `future-rollback-sql` produces nothing). A master changelog
  that only `include`s others is never rendered: leaf changelogs are analyzed
  when they themselves change.

**JPA `ddl-auto` repos** get told what CI cannot see: when
`spring.jpa.hibernate.ddl-auto` is `create`/`update`/`create-drop` and no
migration tool is present, Hibernate mutates the schema at boot and schema
changes never appear in a PR as SQL. The kit logs this on every run and adds an
informational finding when it analyzed other files. The fix is one line
(`spring.jpa.properties.jakarta.persistence.schema-generation.scripts.action=create`
makes Hibernate emit a DDL script the kit picks up) or, better, adopting
Flyway/Liquibase.

**SQL inside application code** is reviewed too (no other migration linter does
this): when a changed `.java` file or MyBatis mapper carries native SQL, the kit
extracts and analyzes it as queries.

- **Annotations**: `@Query(nativeQuery = true)`, `@NativeQuery`,
  `@NamedNativeQuery`, and Spring Data JDBC / R2DBC `@Query` (always native
  there). Plain JPA `@Query` is JPQL, not SQL, and is deliberately skipped
  (feeding entity-language queries to a SQL analyzer only creates false
  positives).
- **Call sites**: `JdbcTemplate` / `NamedParameterJdbcTemplate` / `JdbcClient` /
  `DatabaseClient` query methods and `createNativeQuery(...)`, including text
  blocks, `"a" + "b"` concatenation, and String constants defined in the same
  file. Dynamically assembled SQL is out of static reach.
- **MyBatis XML mappers**: statement bodies are flattened to one analyzable
  branch (`<where>`/`<set>`/`<if>`/`<include>` resolved, `<choose>` takes its
  first `<when>`), `#{...}` parameters become placeholders, and any `${...}`
  string interpolation is flagged as an injection risk in its own right.
- Parameter syntaxes are normalized before analysis: `?1` positional, SpEL
  `:#{...}`, and MyBatis `#{...}`; plain `:name` and `?` pass through.

On GitHub, trigger the workflow with
`paths: ["**/db/migration/**", "**/db/changelog/**", "**/*.sql", "**/*.java", "**/mapper/**/*.xml"]`
and skip the `postgres` service, `DATABASE_URL`, and (for Flyway or SQL-format
Liquibase) the `setup` input entirely.

## How it works

```
MR/PR (migrations changed)
  └─ git diff against the MR/PR base  → changed migrations / .sql files
       └─ manage.py sqlmigrate <app> <migration>   → the exact SQL the DB will run
            ├─ DDL  → sixta_analyze_schema_change   (lock, blocking, safe strategy)
            ├─ DML  → sixta_analyze_query           (anti-patterns, correctness bugs)
            └─ RunPython → flagged for human review (emits no SQL, never passed silently)
                 └─ code-quality / SARIF + MR/PR comment + exit code
```

**Batch mode (`--api v1` / `SIXTA_API=v1`).** Send the whole changeset in a
single `POST /v1/analyze`: all files' statements plus a shared schema context
(captured with `pg_dump --schema-only` when a database is configured, or a
custom `--schema-cmd`) in one request. The server returns per-extraction
verdicts and ready-made GitLab code-quality JSON / GitHub SARIF, which the job
writes directly, falling back to building them locally for older servers. One
HTTP round trip per run, fewer rate-bucket hits, and schema-tier confidence on
every query. **Default: `v1` on GitHub, `mcp` on GitLab** (both overridable).

`sqlmigrate` requires a live database connection (Django resolves constraint
names through it), so both wrappers ship a throwaway `postgres:16` service. An
empty database is fine; for MySQL, override the service and `DATABASE_URL`.

## Rollback audit

In `v1` batch mode the kit also checks whether each changed migration has an
escape hatch, using the framework's own rollback support (it never invents
reverse SQL):

- **Django**: renders `manage.py sqlmigrate <app> <migration> --backwards`. A
  successful reverse render is attached to the request for analysis; Django's
  `IrreversibleError` is reported as `irreversible`.
- **Alembic**: a trivially empty `downgrade()` (absent, `pass`, or
  `raise NotImplementedError`) is reported as `missing`; a real one is rendered
  offline via `alembic downgrade <rev>:<down> --sql` and attached.
- **Plain `.sql` / Flyway**: companion undo file check. `V<version>__name.sql`
  looks for `U<version>__*.sql` in the same directory; a bare `foo.sql` looks
  for `foo.rollback.sql` or `foo.down.sql`. The companion's contents are
  attached; no companion means `missing`. Repeatable migrations (`R__*.sql`)
  skip the audit: their rollback is the previous version of the file.

The server analyzes an attached rollback like any other migration (a rollback
that is itself a lock bomb gets flagged before the incident, not during it) and
raises a "no rollback prepared" finding when none exists. That finding defaults
to informational severity because roll-forward-only is a legitimate policy
(Prisma, for example, ships no down migrations by design), so it never gates by
default. Set `require_rollback: true` (CLI `--require-rollback`, env
`SIXTA_REQUIRE_ROLLBACK`) to raise it to gate-able severity.

If a reverse render fails for any other reason, the migration is sent without
rollback info (unchecked, noted in the job log) and the review continues: the
audit never fails the run. MCP mode skips the audit entirely, since the MCP
tools carry no rollback parameter; the rollback audit is `v1` only.

## Author attribution

In `v1` batch mode the kit names the change author alongside the batch
(`context.operator`), so SIXTA can credit the review to that person's private
review record on connect.sixta.ai. GitLab: `GITLAB_USER_EMAIL`, honored only on
a real runner (`GITLAB_CI`). GitHub Actions: the change author's commit email,
read from `HEAD^2` on pull_request runs (the checkout is GitHub's synthetic
merge commit, whose own author is not the change author) and from `HEAD` on
push runs. **The server stores only a salted hash of the identity, never the
raw value** (see the SIXTA data handling statement); outside CI nothing is
resolved, so local runs are never attributed. Opt out per run with
`SIXTA_NO_ATTRIBUTION=1` (or `true`/`yes`/`on`), per key on the portal's Keys
page, or account-wide with "Pause my record" there.

## Outcome write-back

In keyed `v1` batch mode the kit also closes the loop on each verdict: after
the gate decision it reports what became of every analyzed change
(`POST /v1/outcome`, one small `{change_id, kind}` event per change, no SQL):

- `gate_failed` / `gate_passed`: the change's own gate disposition from this
  run. A harmless change riding in a failing pipeline still reports
  `gate_passed`; only changes whose own severity meets the gate report
  `gate_failed`.
- `acted_upon`: on a re-run of the same PR/MR, a previously failing change
  whose finding is now gone (the file was edited and its extractions all clear
  the gate). The kit remembers failing change ids inside its own upserted
  PR/MR comment (an invisible HTML marker), so this works on ephemeral CI
  runners with no extra storage; without a comment token it still reports the
  gate events, just not `acted_upon`.

These dispositions feed the review record on connect.sixta.ai (the saves your
gate actually banked). Reporting is advisory and fire-and-forget: it requires
an API key, runs after the report surfaces are written, makes a single attempt
per event, and can never change the pipeline's exit code. Opt out per run with
`SIXTA_OUTCOMES=0` (or `false`/`no`/`off`).

## Inputs

Shared across both platforms (GitLab job inputs / GitHub Action `with:`):

| Input | Default | Notes |
|---|---|---|
| `engine` / `engine_version` | `auto` / none | `auto` detects from build/config files, falling back to postgresql. **Set the version to match production.** |
| `gate` | `high` | Fail at ≥ this severity: `critical`, `high`, `medium`, `low`, `none`. |
| `fail_mode` | `open` | SIXTA unreachable → `open`: warn & pass; `closed`: fail. Findings always gate. A rejected **configured** API key (HTTP 401/403) always fails in CI, regardless of mode; anonymous runs follow `fail_mode`, and local pre-commit runs warn and proceed. |
| `api` | `v1` (GitHub) / `mcp` (GitLab) | `v1` batches the whole run into one `POST /v1/analyze`. |
| `schema_cmd` | none | `v1` only: command whose stdout is the shared schema DDL (default `pg_dump` when a DB is configured). |
| `require_rollback` | `false` | `v1` only: raise the "no rollback prepared" finding to gate-able severity. See "Rollback audit". |
| `badge` | `true` | Append the "reviewed by SIXTA" footer badge to the PR/MR comment. Turning it off is a Connect Pro setting, confirmed via the `v1` API. See "The badge". |
| `setup` / `manage_py` | `pip install -r requirements.txt` / `manage.py` | Reuse your test job's environment. Leave `setup` empty for `.sql`-only repos. |
| `sixta_url` | `https://connect.sixta.ai/mcp` | SIXTA endpoint. |

GitLab-only: `image`, `postgres_image`, `allow_failure`, `stage`, `script_ref`.
GitHub-only: `working_directory` (monorepo subdir), `python_version`, `sarif`
(output path), `script_ref`. The GitHub PR comment uses the built-in
`GITHUB_TOKEN` (grant `pull-requests: write`); the SARIF upload needs
`security-events: write`.

## The badge

Every PR/MR comment ends with a small "reviewed by SIXTA" badge. It is always
neutral: verdicts live in the report above it, never in the footer.

There is also a per-repository README badge with live counters ("0 unsafe
migrations approved"), updated after every gated `v1` run. The job step summary
(GitHub) or job log (GitLab) prints a ready-made snippet with your repo's badge
URL after each run; paste it into your README. The URL is keyed by an
unguessable random slug the server mints for your repo, so the counters only
become public if you choose to publish the badge. Your repository name never
appears in the URL.

Turning the comment footer off (`badge: false`) is a Connect Pro setting. The
server confirms the entitlement in the `v1` response, so on GitLab set
`api: v1` as well.

## Table-size hints (`.sixta.yml`)

Without a database connection, SIXTA can't know your table sizes. A repo-level
`.sixta.yml` turns conditional verdicts into concrete duration estimates and risk
escalation (see [.sixta.yml.example](.sixta.yml.example)). Connect Pro reads real
sizes from your database directly, so it needs no hints. Statements touching a hinted
table are analyzed in their own call so the hints apply precisely. JSON content
(or `.sixta.json`) works without PyYAML.

## Local pre-commit hook

Same verdicts before the push, via the [pre-commit](https://pre-commit.com) framework:

```yaml
repos:
  - repo: https://github.com/sixta-systems/sixta-ci
    rev: v0.7.0
    hooks:
      - id: sixta-review
```

or the plain git hook: `cp hooks/pre-commit .git/hooks/ && chmod +x .git/hooks/pre-commit`.
Local mode needs `SIXTA_API_KEY` in your shell and a runnable Django env with a
reachable database. `SIXTA_SKIP=1 git commit` bypasses it.

## Demo

[`example/`](example/) is a tiny Django project with a deliberately risky
migration, a plain `CREATE INDEX` (blocks writes; SIXTA suggests
`CONCURRENTLY`) plus a `RunSQL` backfill containing a silent `= NULL` bug, and a
`RunPython` migration to show the manual-review flag. Its
[.gitlab-ci.yml](example/.gitlab-ci.yml) and
[.github/workflows/sixta.yml](example/.github/workflows/sixta.yml) are the
consumer setups for each platform.
[`example/spring-flyway/`](example/spring-flyway/) is the Spring Boot
equivalent: Flyway versioned/repeatable/undo migrations with a `${placeholder}`,
plus a Java-based migration that triggers the manual-review flag.
[`example/spring-liquibase/`](example/spring-liquibase/) shows both Liquibase
styles: a formatted-SQL changelog (with `--property` and `--rollback`) and an
XML leaf behind a master index.

## Development

```bash
pip install -e ".[dev]"
pytest            # unit + stub-server integration tests (GitLab + GitHub), no network
```

Both wrappers fetch `sixta_review.py` (stdlib-only, single file) from this repo
at `script_ref`. Releases are git tags; the template + action defaults pin them.

### Releasing (keep GitHub and GitLab in sync)

GitHub (`origin`) is where users and the Action fetch from; GitLab (`gitlab`)
publishes the CI/CD Catalog component. `main` and the `v*` tags must match on
both; the bare `X.Y.Z` tag is GitLab-only (it triggers the catalog release).

1. Bump `__version__` and every pinned ref (action + template `script_ref`
   defaults, README examples, pre-commit `rev`, catalog `@X.Y`).
2. Merge to `main`, then `git push origin main && git push gitlab main`.
3. `git tag -a vX.Y.Z && git tag -a X.Y.Z` (bare tag = catalog trigger).
4. `git push origin vX.Y.Z && git push gitlab vX.Y.Z X.Y.Z`.
5. Move the major tag: `git tag -f v1 && git push -f origin v1 && git push -f gitlab v1`.
6. Publish the GitHub release notes on `vX.Y.Z`; confirm the GitLab tag
   pipeline's `create-release` job published the catalog entry.
