# sixta-ci

**DBRE-grade SQL review for Django and Alembic migrations and `.sql` files, in
your GitLab merge requests and GitHub pull requests.**

When an MR/PR adds or changes a migration (or a `.sql` file), the CI kit renders
it to SQL (Django via `manage.py sqlmigrate`, Alembic via offline
`alembic upgrade --sql`, and `.sql` files read directly), sends DDL to
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
3. Add to `.gitlab-ci.yml` (GitLab ≥ 16.6, remote includes with inputs):

```yaml
include:
  - remote: "https://raw.githubusercontent.com/sixta-systems/sixta-ci/v1/templates/sixta-review.yml"
    inputs:
      engine_version: "16"                      # match production; verdicts are version-dependent
      setup: pip install -r requirements.txt    # whatever makes manage.py runnable
      allow_failure: true                       # soak period; remove to enforce
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
  attached; no companion means `missing`.

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

## Inputs

Shared across both platforms (GitLab job inputs / GitHub Action `with:`):

| Input | Default | Notes |
|---|---|---|
| `engine` / `engine_version` | `postgresql` / none | **Set the version to match production.** |
| `gate` | `high` | Fail at ≥ this severity: `critical`, `high`, `medium`, `low`, `none`. |
| `fail_mode` | `open` | SIXTA unreachable → `open`: warn & pass; `closed`: fail. Findings always gate. |
| `api` | `v1` (GitHub) / `mcp` (GitLab) | `v1` batches the whole run into one `POST /v1/analyze`. |
| `schema_cmd` | none | `v1` only: command whose stdout is the shared schema DDL (default `pg_dump` when a DB is configured). |
| `require_rollback` | `false` | `v1` only: raise the "no rollback prepared" finding to gate-able severity. See "Rollback audit". |
| `setup` / `manage_py` | `pip install -r requirements.txt` / `manage.py` | Reuse your test job's environment. Leave `setup` empty for `.sql`-only repos. |
| `sixta_url` | `https://connect.sixta.ai/mcp` | SIXTA endpoint. |

GitLab-only: `image`, `postgres_image`, `allow_failure`, `stage`, `script_ref`.
GitHub-only: `working_directory` (monorepo subdir), `python_version`, `sarif`
(output path), `script_ref`. The GitHub PR comment uses the built-in
`GITHUB_TOKEN` (grant `pull-requests: write`); the SARIF upload needs
`security-events: write`.

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
    rev: v0.3.2
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

## Development

```bash
pip install -e ".[dev]"
pytest            # unit + stub-server integration tests (GitLab + GitHub), no network
```

Both wrappers fetch `sixta_review.py` (stdlib-only, single file) from this repo
at `script_ref`. Releases are git tags; the template + action defaults pin them.
