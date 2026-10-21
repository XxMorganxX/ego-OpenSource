#!/bin/bash
# First-init role provisioning (docker-entrypoint-initdb.d — runs ONCE, on an
# empty kb_pgdata volume). Services connect as the app role, never the
# bootstrap superuser; pytest connects as kb_test to kb_mcp_test.
set -euo pipefail
app_user="${KB_APP_USER:-kb_app}"
app_pass="${KB_APP_PASSWORD:?KB_APP_PASSWORD is required}"
test_pass="${KB_TEST_PASSWORD:-kb_test}"

# Passwords are interpolated directly into single-quoted SQL literals below, so
# KB_APP_PASSWORD / KB_TEST_PASSWORD must not themselves contain a single quote —
# that breaks CREATE ROLE mid-statement. This script only runs on first cluster
# init, so a failed run does NOT retry on its own: fix the password and
# `docker compose down -v` (wipes the volume) before bringing the cluster back up.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<-SQL
    CREATE ROLE ${app_user} LOGIN PASSWORD '${app_pass}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    ALTER DATABASE ${POSTGRES_DB} OWNER TO ${app_user};
    CREATE ROLE kb_test LOGIN PASSWORD '${test_pass}' NOSUPERUSER CREATEDB NOCREATEROLE;
SQL
# Two test databases: the suites' default, and the `_b` second tenant the
# tenant-isolation tests need. kb_test can create a database but not the vector
# extension, so both are made here, as the superuser, with their extensions.
for db in kb_mcp_test kb_mcp_test_b; do
    createdb -U "$POSTGRES_USER" -O kb_test "$db"
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" \
         -c "CREATE EXTENSION IF NOT EXISTS ltree; CREATE EXTENSION IF NOT EXISTS vector;"
done
