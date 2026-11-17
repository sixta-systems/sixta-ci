#!/bin/sh
# `flyway migrate` from the command line, then tell SIXTA what it applied
# (a sixta-ci recipe). For Flyway inside a JVM service (Spring Boot), use
# SixtaRegisterCallback.java beside this file instead.
#
# Where it goes: commit this file next to your pipeline and call it where the
# step used to call `flyway migrate`, with the same arguments minus the command:
#
#   recipes/flyway/flyway-migrate.sh -configFiles=conf/flyway.toml
#
# What it does: runs `flyway -outputType=json migrate`, prints Flyway's result,
# and when the run succeeded registers one change per migration it applied:
# the script's name, its version, the statements in its SQL file, and the
# run's start and finish. A Java-based migration is registered by its name and
# version, with no statement text. A run that applied nothing registers
# nothing, and neither does a run that failed (the callback recipe registers
# the migrations that finished before a failure; this wrapper cannot see them).
# Its exit code is `flyway migrate`'s, always.
#
# Configuration, from the step's environment:
#
#   SIXTA_CHANGE_URL       base URL of your SIXTA backend (unset = the register step does nothing)
#   SIXTA_CHANGE_TOKEN     the bearer secret, from your CI secret store
#   SIXTA_CHANGE_DATABASE  the database's name in SIXTA
#   SIXTA_REGISTER         the command, default "python3 sixta_review.py register"
#   SIXTA_FLYWAY_CMD       the Flyway CLI, default "flyway"

flyway=${SIXTA_FLYWAY_CMD:-flyway}
result=$(mktemp)
trap 'rm -f "$result"' EXIT

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
"$flyway" -outputType=json "$@" migrate >"$result"
rc=$?
finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat "$result"

if [ "$rc" -eq 0 ]; then
  # shellcheck disable=SC2086 # SIXTA_REGISTER is a command line, split on purpose
  ${SIXTA_REGISTER:-python3 sixta_review.py register} \
    --tool flyway --flyway-json "$result" \
    --started-at "$started" --finished-at "$finished" || :
fi
exit "$rc"
