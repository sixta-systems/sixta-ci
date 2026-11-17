#!/bin/sh
# `liquibase update`, then tell SIXTA what it applied (a sixta-ci recipe).
#
# Where it goes: commit this file next to your pipeline and call it where the
# step used to call `liquibase update`, with the same arguments minus the
# command (they go before it, the way Liquibase reads global arguments):
#
#   recipes/liquibase/liquibase-update.sh --changelog-file=db/changelog/db.changelog-master.yaml
#
# The URL and credentials come from where they already do (liquibase.properties,
# LIQUIBASE_COMMAND_URL and friends); nothing here reads them.
#
# What it does: asks Liquibase for the SQL it is about to run (`update-sql`,
# Liquibase's own preview), runs `update`, and when the update succeeded
# registers that SQL with the start and the finish. Liquibase's bookkeeping
# (DATABASECHANGELOG, DATABASECHANGELOGLOCK) is not sent, and an update with
# nothing pending registers nothing. Its exit code is `liquibase update`'s,
# always.
#
# Configuration, from the step's environment:
#
#   SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = the register step does nothing)
#   SIXTA_CHANGE_TOKEN     the bearer secret, from your CI secret store
#   SIXTA_CHANGE_DATABASE  the database's name in SIXTA
#   SIXTA_REGISTER         the command, default "python3 sixta_review.py register"
#   SIXTA_LIQUIBASE_CMD    the Liquibase CLI, default "liquibase"
#   SIXTA_CHANGE_REVISION  the commit, default GITHUB_SHA or CI_COMMIT_SHA

liquibase=${SIXTA_LIQUIBASE_CMD:-liquibase}
pending=$(mktemp)
trap 'rm -f "$pending"' EXIT

preview=1
[ -n "$SIXTA_CHANGE_URL" ] || preview=0
if [ "$preview" -eq 1 ]; then
  "$liquibase" "$@" update-sql >"$pending" 2>/dev/null || preview=0
fi

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
"$liquibase" "$@" update
rc=$?
finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ "$rc" -eq 0 ] && [ "$preview" -eq 1 ]; then
  # shellcheck disable=SC2086 # SIXTA_REGISTER is a command line, split on purpose
  ${SIXTA_REGISTER:-python3 sixta_review.py register} \
    --tool liquibase --file "$pending" \
    --revision "${SIXTA_CHANGE_REVISION:-${GITHUB_SHA:-${CI_COMMIT_SHA:-}}}" \
    --started-at "$started" --finished-at "$finished" || :
fi
exit "$rc"
