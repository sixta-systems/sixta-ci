# Spring / Spring Boot support plan (SIXTA CI kit)

Make Spring Boot repos on GitHub and GitLab first-class citizens: their DDL
(migrations) and SQL get SIXTA analysis in every PR/MR. Companion to
`framework-support.md` (registry, mechanisms A/B/C, release discipline all
apply) and to the server-side `docs/ci-protocol-plan.md` §4.

## Where we are today
Flyway migrations are plain `.sql` files, so the existing raw-`.sql` handler
already analyzes a changed `V42__add_index.sql` — this is `framework-support.md`
Phase 4 ("document + market as already-supported"). But "the file happens to end
in `.sql`" is not Spring support:

- No Flyway convention awareness: repeatable (`R__`) vs versioned (`V`) semantics,
  undo scripts (`U__`) as the rollback-audit source, `${placeholder}` tokens
  (currently break the parse), `{vendor}` subdirectories, `spring.flyway.locations`
  overrides.
- No engine/dialect detection from the build or config files.
- Liquibase XML/YAML/JSON changelogs (no SQL on disk) aren't rendered.
- `schema.sql` / `data.sql` init scripts aren't discovered.
- SQL embedded in Java (`@Query(nativeQuery = true)`, `JdbcTemplate`, MyBatis
  mappers) is invisible.
- JPA `ddl-auto` repos (no SQL artifacts at all) get silence instead of guidance.

## Detection (shared by all phases)
A `spring` entry in the framework registry, detected from files already in the
diff or repo root:

| Signal | Tells us |
|---|---|
| `pom.xml` / `build.gradle(.kts)`: `flyway-core` or `spring-boot-starter-flyway` (Boot 4 renamed the trigger dep) | Flyway |
| `flyway-database-postgresql` / `flyway-mysql` (Flyway 10+ per-DB modules) | Flyway **and** the engine |
| `liquibase-core` / `spring-boot-starter-liquibase` | Liquibase |
| `mybatis-spring-boot-starter` | MyBatis mappers present |
| JDBC driver artifact (`org.postgresql:postgresql`, `com.mysql:mysql-connector-j`) | `engine` |
| `application.{properties,yml,yaml}` (all profiles): `spring.flyway.locations`, `spring.liquibase.change-log`, `spring.sql.init.*`, `spring.jpa.hibernate.ddl-auto`, datasource URL | locations, init mode, engine fallback |

Engine resolution order: Flyway DB module → JDBC driver → datasource URL →
`--engine` flag. Ambiguity fails loudly, same as today.

## Phase S1 — Flyway done right (mechanism: static, no runtime needed)
- Discover migrations under `src/{main,test}/resources/db/migration/**` plus any
  configured locations, including `{vendor}` subdirs.
- Parse the naming convention: `V<version>__<desc>.sql` (versioned),
  `R__<desc>.sql` (repeatable — analyze on every checksum change),
  `U<version>__<desc>.sql` (undo).
- Substitute `${placeholder}` tokens with dummy literals before sending
  (values from `spring.flyway.placeholders.*` when present).
- Rollback audit: a changed `V<n>__x.sql` probes for the matching `U<n>__x.sql`
  and attaches it, mirroring the Django/Alembic rollback pattern. Absent undo
  file → the existing "no rollback" disposition.
- `schema.sql` / `data.sql` (+ `schema-<platform>.sql` variants — the platform
  suffix is another engine signal) analyzed as migrations when changed.
- Java-based Flyway migrations (`V*__*.java`): no offline SQL — emit the
  manual-review finding, exactly like Django `RunPython` / Alembic data ops.

**Effort: ~3–5 days.** Ships alone; this is the release that makes
"Spring Boot supported" a truthful landing line for the Flyway majority.

## Phase S2 — Liquibase (mechanism A: offline render)
Promotes the existing `framework-support.md` Phase 2, now un-gated: Spring Boot
is the main reason to want it.
- Locate the master changelog (`spring.liquibase.change-log`, default
  `classpath:/db/changelog/db.changelog-master.yaml`), follow
  `include`/`includeAll`.
- XML/YAML/JSON changelogs: render with `liquibase update-sql` in offline mode
  (`offline:postgresql|mysql` URL — no database needed). Spring CI runners
  always have a JVM, so requiring the Liquibase CLI is acceptable; document the
  install line for the GitHub Action and GitLab template.
- Formatted-SQL changelogs (`--liquibase formatted sql`): parse statically —
  split on `--changeset author:id`, extract inline `--rollback` SQL and feed it
  to the rollback audit (a nice fit: Liquibase makes rollbacks first-class).
- Property substitution `${...}`: same dummy-literal pass as Flyway.

**Effort: ~1 week.** Independent of S1.

## Phase S3 — SQL embedded in Java (mechanism C: static extract)
The differentiator: no mainstream linter reads SQL out of Spring application
code today (sqlfluff/Atlas/Squawk all stop at migration files).
- Extract string SQL from: `@Query(nativeQuery = true)`, Spring Data JDBC
  `@Query` (always native), `@NamedNativeQuery`,
  `JdbcTemplate`/`NamedParameterJdbcTemplate`/`JdbcClient` call sites,
  `entityManager.createNativeQuery`. Handle text blocks, constants, and simple
  concatenation; punt on dynamic assembly.
- **Skip JPQL**: `@Query` without `nativeQuery = true` is JPQL/HQL (entity
  names, not tables) — feeding it to a SQL analyzer produces false positives.
- MyBatis XML mappers (`mybatis.mapper-locations`, conventionally
  `resources/mapper/**/*.xml`): extract statement bodies, best-effort flatten
  dynamic tags (`<if>`, `<where>`, `<foreach>`), and flag `${...}` string
  interpolation as an injection smell in its own right.
- Shared parameter normalization before sending: `?`, `?1`, `:name`, `:#{...}`,
  `#{...}`, `${...}` → dummy literals.
- Sent as `kind: "query"` extractions; findings land on the Java/XML source
  line like any other.

**Effort: ~2–3 weeks.** Ship in slices (JdbcTemplate + `@Query` first, MyBatis
second); each slice is independently releasable.

## Phase S4 — JPA `ddl-auto` repos (docs + detection, no rendering)
The most common small-project setup has no SQL artifacts at all: Hibernate
generates DDL from `@Entity` classes at runtime.
- Detect `ddl-auto=create|update` with no migration tool present and emit one
  informational finding: schema changes in this repo are invisible to CI review,
  with a pointer to the fix.
- Document the one-liner that makes Hibernate emit a DDL file the kit picks up
  (`jakarta.persistence.schema-generation.scripts.action=create`), and the
  longer-term recommendation (adopt Flyway; Boot's own docs agree).
- Predicting SQL from entities/derived query methods is the server-side
  model-review track (ci-protocol-plan §4 `kind:"code"`), out of scope here.

**Effort: ~1–2 days.**

## What already works with zero changes
- Hibernate N+1 detection: the server's `sixta_detect_n_plus_one` already parses
  `Hibernate:` SQL logs, so a Spring Boot test job can pipe its log today.
- Output formats: GitHub SARIF and GitLab Code Quality are live in the kit for
  both platforms.
- Engines: Spring shops are overwhelmingly Postgres/MySQL — both supported.
  (Oracle/SQL Server Spring shops are out of scope until the server grows
  engines.)

## Per-phase deliverables (house pattern)
detect + render · stub-server tests (no network) · a Spring Boot demo app under
`example/` (Flyway + one `@Query` + one MyBatis mapper so every phase has a
fixture) · CI snippets in the README and on `/ci` · landing "supported" copy
only when the tag is cut.

## Effort summary
| Phase | Scope | Estimate |
|---|---|---|
| S1 | Flyway conventions + init scripts + engine detection | 3–5 days |
| S2 | Liquibase (offline render + formatted SQL) | ~1 week |
| S3 | Java-embedded SQL + MyBatis | 2–3 weeks, sliceable |
| S4 | ddl-auto detection + docs | 1–2 days |

S1 alone is a truthful "Spring Boot (Flyway) supported" release in under a
week. S1+S2 covers essentially every Spring repo that has reviewable DDL.
S3 is the moat, on its own timeline.
