# Registering a database change with SIXTA (for coding agents)

Where it goes: paste the section below into the file your coding agent reads
for standing instructions (`AGENTS.md`, `CLAUDE.md`, a Cursor rule), in the
repository whose migrations the agent may apply. The person or pipeline that
starts the agent provides `SIXTA_CHANGE_URL`, `SIXTA_CHANGE_TOKEN` and
`SIXTA_CHANGE_DATABASE` in its environment; where they are unset the command
does nothing, so the section is safe to leave in place everywhere.

---

## After you change a database, tell SIXTA

When you apply a migration or run DDL or a data fix against a shared database
(staging, production, any database other people use), register it with SIXTA
as the next step, once the change has succeeded:

```bash
python3 sixta_review.py register \
  --tool "<what you ran it with: psql, flyway, alembic, ...>" \
  --actor "<your agent name, and the person you are working for>" \
  --file <the SQL file you applied> \
  --started-at "<when you started it, RFC 3339 UTC, e.g. 2026-11-17T10:00:00Z>" \
  --finished-at "<when it finished>" \
  --revision "$(git rev-parse --short HEAD)"
```

Use `--statement "<SQL>"` (repeatable) instead of `--file` when you ran
statements that are in no file. Add `--pull-request owner/repo#N` when the
change came from a pull request. Note the start time before you run the
change, so the times you register are the real ones.

Rules:

- Register only what actually ran, after it ran. Do not register a change
  that failed or that you only planned.
- Run it once. It prints one line and always exits zero; if the line says the
  change was not registered, report that line to the person you are working
  for and carry on. Do not retry in a loop and do not treat it as a failure of
  the change.
- Never write the token into a command line, a file, a commit or your
  report. It is read from `SIXTA_CHANGE_TOKEN` in the environment.
- Registering asks nothing and approves nothing: it is a note of what ran. It
  is not permission to run a change you were not asked to run.
- `--dry-run` prints exactly what would be sent and sends nothing, if you or
  the person you work for want to see it first.
