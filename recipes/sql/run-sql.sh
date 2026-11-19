#!/bin/sh
# Run a SQL file in a pipeline step, then tell SIXTA it ran (a sixta-ci recipe).
#
# Where it goes: commit this file next to your pipeline and call it where the
# step used to call psql or mysql. The first argument is the SQL file; the
# rest is the command that runs it, unchanged:
#
#   recipes/sql/run-sql.sh db/changes/2026-11-add-index.sql \
#     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/changes/2026-11-add-index.sql
#
#   recipes/sql/run-sql.sh change.sql sh -c 'mysql --defaults-file=ci.cnf orders < change.sql'
#
# What it does: notes the time, runs your command, and when it succeeded
# registers the file's statements with the start and the finish. Its exit code
# is your command's, always: the registration cannot change it, and a command
# that failed is not registered.
#
# Configuration, from the step's environment:
#
#   SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = the register step does nothing)
#   SIXTA_CHANGE_TOKEN     the bearer secret, from your CI secret store
#   SIXTA_CHANGE_DATABASE  the database's name in SIXTA
#   SIXTA_REGISTER         the command, default "python3 sixta_review.py register"
#   SIXTA_CHANGE_TOOL      what ran it, default the command's own name (psql, mysql)
#   SIXTA_CHANGE_REVISION  the commit, default GITHUB_SHA or CI_COMMIT_SHA

file=$1
shift
if [ -z "$file" ] || [ $# -eq 0 ]; then
  echo "usage: run-sql.sh FILE COMMAND [ARG...]" >&2
  exit 64
fi

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
"$@"
rc=$?
finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ "$rc" -eq 0 ]; then
  # shellcheck disable=SC2086 # SIXTA_REGISTER is a command line, split on purpose
  ${SIXTA_REGISTER:-python3 sixta_review.py register} \
    --tool "${SIXTA_CHANGE_TOOL:-$(basename "$1")}" \
    --file "$file" --subject "$(basename "$file")" \
    --revision "${SIXTA_CHANGE_REVISION:-${GITHUB_SHA:-${CI_COMMIT_SHA:-}}}" \
    --started-at "$started" --finished-at "$finished" || :
fi
exit "$rc"
