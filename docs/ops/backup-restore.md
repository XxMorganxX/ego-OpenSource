# Backups and restore

`kb-backup run` backs up every database in the tenant registry: the owner's (`kb_mcp`) and
every tenant's `kb_t_<slug>`, **disabled tenants included**. For each database it:

1. dumps it as the app role with `pg_dump --format=custom --no-owner --no-privileges`,
2. encrypts the dump with [`age`](https://github.com/FiloSottile/age) to a public key,
3. writes the result to `$KB_BACKUP_DIR/<database>/<YYYYMMDDTHHMMSSZ>.dump.age`.

The default `KB_BACKUP_DIR` is `~/Library/Application Support/kb/backups/` on macOS and
`$XDG_DATA_HOME/kb/backups/` (normally `~/.local/share/kb/backups/`) elsewhere. Directories
are `0700` and files `0600`. The plaintext dump exists only as a temporary file while the
backup runs, and is deleted even when the run fails.

The **private** key never lives on the host. A copied disk or a copied backup directory is
unreadable without it, and so is every restore.

**What `kb-backup` does not cover.** Copy these by hand into your password manager:

- `KB_Core/tenants.toml` (it holds every tenant's agent secret),
- `KB_Core/.env`,
- `KB_Core/policy.toml`,
- `KB_Core/prompts.local/`,
- `KB_Core/tenants/`.

Conventions match [tenant-onboarding.md](tenant-onboarding.md). In particular, the CLIs
do not read `.env` themselves, so run them through the `kbenv` helper:

```bash
kbenv() { (cd "$KB_INFRA"/KB_Core && set -a && . ./.env && set +a && "$@"); }
```

## 1. One-time setup

1. Install `age` on the host. The backup service shells out to it.

   ```bash
   brew install age
   ```

2. Generate the key pair. Ideally do this on another machine. If you generate it here,
   move the file off right away.

   ```bash
   age-keygen -o ~/kb-backup-key.txt
   ```

   Write it to your home directory, never into the repo's working tree, where a stray
   `git add` could commit it. The command prints `Public key: age1…`. Store
   `~/kb-backup-key.txt` in your password manager and delete the local file. **Never leave
   it on the host.** `age-keygen -y ~/kb-backup-key.txt` re-derives the public key from
   the file.

3. Put the **public** key in `KB_Core/.env`:

   ```bash
   KB_BACKUP_AGE_RECIPIENT=age1…
   ```

   Without it, `kb-backup run` exits 2 and prints instructions to set it.

4. Optional settings:
   - `KB_BACKUP_DIR`: where backups go.
   - `KB_BACKUP_INTERVAL`: seconds between runs (default `86400`). It is read when
     `kb-dev install` writes the plist.
   - `KB_PG_BIN`: a directory holding `pg_dump` and `pg_restore`. The default is `PATH`,
     then `/opt/homebrew/opt/postgresql@16/bin`. The client tools' major version must be
     at least the server's (PG 16).

5. Load the service:

   ```bash
   kb-dev install
   ```

## 2. Run it once by hand

```bash
cd "$KB_INFRA"/KB_Core && bash scripts/kb-svc.sh backup
```

This runs exactly what the launchd service runs: it sources `.env`, waits for Postgres,
and uses the app-role DSN. A clean run prints
`[kb-backup] wrote <n> file(s) under <dir>; pruned <m>` and exits 0.

If one database fails, the run prints `[kb-backup] <database>: FAILED: <reason>` and keeps
going with the others. The reason is usually `pg_dump exited <code>` or `age exited <code>`. The summary still prints, and the run exits 1.

Inspect the output:

```bash
ls -la ~/Library/"Application Support"/kb/backups/
ls -la ~/Library/"Application Support"/kb/backups/kb_mcp/
```

There is one directory per database, with one `.dump.age` file per run. To prove a file
decrypts, without restoring anything, copy the key from your password manager to a
temporary file, test the file, and delete the key at once:

```bash
age --decrypt --identity ~/kb-backup-key.txt <file>.dump.age | pg_restore --list | head
rm -f ~/kb-backup-key.txt
```

## 3. The daily `backup` service

The `backup` service (`com.kb.backup`) is a one-shot with `StartInterval` =
`KB_BACKUP_INTERVAL` (default 86400 s) and `RunAtLoad`. So it runs:

- whenever it is loaded or kickstarted: every `kb-dev install`, `kb-dev start`, bare
  `kb-dev restart`, and login,
- then every 24 h after that.

It does **not** run at a fixed time of night. Output goes to
`~/Library/Logs/kb/backup.log` (`kb-dev logs backup`). Nothing alerts on a failed backup
yet, so check that log.

## 4. Retention

After each run, `kb-backup` prunes each database directory it just backed up:

- it keeps the **newest 14 files**,
- from the older files, it also keeps one file per ISO week (the newest in that week),
  for up to **8 weeks**, newest weeks first,
- it deletes everything else.

Retention counts **files, not days**. Every extra run (a restart, a run by hand) spends a
daily slot, so a day of many restarts shortens the daily window.

A tenant removed with `kb-tenant delete` is no longer in the registry, so its directory
is **never pruned**. Delete `KB_BACKUP_DIR/kb_t_<slug>/` by hand once you no longer need
to keep it.

## 5. Restore

```bash
kb-backup restore <file> --into <app-role dsn> --identity <key file>
```

The command works in three steps:

1. It decrypts the file in memory with `age`, lists the archive, drops the extensions'
   entries from that list (the admin role owns the extensions, and the target already has
   them), and pipes the archive into
   `pg_restore --no-owner --no-privileges --clean --if-exists --use-list <list> --dbname <dsn>`.
   An archive whose list has no table data is refused before anything is restored.
2. It applies any pending migrations (the same runner the daemons use at startup).
3. It verifies the result (§5.1).

- **It does not create the database.** The target must already exist.
- **`--clean` replaces everything in the target.** Every object in the dump is dropped,
  then recreated.
- `--into` is a full DSN, not a database name, and it must be the **app role's** DSN
  (`kb-tenant dsn <slug>`, or `kb-tenant dsn owner`), never the admin role's. With
  `--no-owner` the restored objects belong to whoever connects, and `--no-privileges` does
  not replay the dump's grants, so an admin-role restore leaves the daemons without access.

The private key has to be on the host for the restore. Copy it from your password manager
to a temporary file (`~/kb-backup-key.txt` below), and delete it when you finish.

### 5.1 What a successful restore looks like

`kb-backup restore` shows `pg_restore`'s stderr (passwords redacted) on its own stderr.
After `pg_restore`, it applies any pending migrations, then verifies that the schema is at
this code's latest migration and that the `nodes_notify_update` trigger exists on `nodes`.

- **Exit 0**, with `restored <file> into database <db>; verified; applied <n>
  migration(s)` (followed by `: <names>` when any were applied): every `pg_restore` error
  was on the benign list (today one entry, the pre-0049 trigger error below), and the
  verification passed.
- **Exit 1**, with `[kb-backup] restore into <db>: FAILED: <reason>`: the restore is
  incomplete or unverified. Read the captured errors, and do not put the database back
  into service. A dump made by newer code than the code restoring it fails the head check
  by design (`restore with the code version that made the dump`).

Whatever the exit status, check the change-notification trigger yourself. The dashboard's
live updates depend on it:

```sql
SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1;   -- must list nodes_notify_update
```

**Backups taken before migration 0049.** These dumps carry the old DDL for
`nodes_notify_update`, which `pg_restore` cannot create under its empty `search_path`. It
prints `operator does not exist: public.ltree = public.ltree` on
`CREATE TRIGGER nodes_notify_update`. The restore heals this itself: the migrations it
applies afterwards include 0049, which recreates the trigger. Such dumps stay in retention
for roughly 10 weeks after 0049 is deployed (14 daily files, then up to 8 weekly ones).

If a real restore (§5.3) failed with `trigger nodes_notify_update is missing`, recreate the
trigger from the captured error output with `search_path = public`. Run this block after
the §5.3 subshell and before its `rm` deletes `$TMPDIR/kb-restore.err`; it targets the
same live database the restore did:

```bash
(
  cd "$KB_INFRA"/KB_Core && set -a && . ./.env && set +a
  DSN="$(.venv/bin/python .venv/bin/kb-tenant dsn <slug>)"     # or: kb-tenant dsn owner
  CMD="$(sed -n 's/^Command was: \(CREATE TRIGGER .*\)$/\1/p' "$TMPDIR/kb-restore.err")"
  psql "$DSN" -c "SET search_path = public; $CMD"
)
```

Then re-run the `pg_trigger` check above. The subshell keeps `$DSN` off the command line and out of your shell history.

In the §5.2 drill the fallback is not needed: the drill database is dropped at its step 4
anyway, and a missing trigger there is itself the finding. To rehearse the fallback in a
drill, put its two inner lines (`CMD=…` and `psql "$DSN" …`) inside the drill's subshell
between steps 3 and 4, where `$DSN` is the `kb_t_restore_drill` DSN.

**Never paste a DSN.** `kb-tenant dsn` prints the DSN *with its password* (your app
role's for `owner`, the tenant's own role's for a tenant). Consume it
inside a subshell, as every block below does. Never paste it into a command line, a
document, or a chat.

### 5.2 Drill (after setup, then every few months)

The drill restores the newest backup of one database into a throwaway database created
just for it. It never touches a live database or the registry. Pick `<database>`
(`kb_mcp` or a `kb_t_<slug>`) and its newest `<file>`. Then:

```bash
(
  cd "$KB_INFRA"/KB_Core && set -a && . ./.env && set +a
  # 1. An empty target, provisioned exactly as kb-tenant provisions a tenant database:
  #    its own role and password (no registry entry, so name no real tenant restore_drill).
  KB_DRILL_PW="$(openssl rand -hex 32)"; export KB_DRILL_PW
  .venv/bin/python -c 'import os; from kb_core.tenant_cli import admin_dsn, create_database
create_database(admin_dsn(), "kb_t_restore_drill", password=os.environ["KB_DRILL_PW"])'
  DSN="postgresql://kb_t_restore_drill:$KB_DRILL_PW@localhost:${DB_HOST_PORT:-5433}/kb_t_restore_drill"
  # 2. Restore, keeping pg_restore's errors.
  .venv/bin/python .venv/bin/kb-backup restore "$HOME/Library/Application Support/kb/backups/<database>/<file>" \
      --into "$DSN" --identity ~/kb-backup-key.txt 2> "$TMPDIR/kb-restore.err"
  echo "kb-backup restore exit: $?"               # 0 = clean (§5.1); otherwise read the .err file
  # 3. Check the result.
  psql "$DSN" -Atc "SELECT count(*) FROM nodes"
  psql "$DSN" -Atc "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1"
  # 4. Drop the drill database and its role.
  .venv/bin/python -c 'from kb_core.tenant_cli import admin_dsn, drop_database
drop_database(admin_dsn(), "kb_t_restore_drill")'
)
rm -f ~/kb-backup-key.txt "$TMPDIR/kb-restore.err"
```

Compare the result against the live database. Use the subshell form, so the credentialed
DSN never reaches your shell history (use `dsn owner` for `kb_mcp`):

```bash
kbenv sh -c 'psql "$(.venv/bin/python .venv/bin/kb-tenant dsn <slug>)" -Atc "SELECT count(*) FROM nodes"'
kbenv sh -c 'psql "$(.venv/bin/python .venv/bin/kb-tenant dsn <slug>)" -Atc "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1"'
```

The `nodes` count should match as of the backup's timestamp. The trigger lists should
match exactly, and `nodes_notify_update` must be present.

### 5.3 Recover a live database

**A. The database exists but its contents are bad.**

The steps are the same for a tenant and for the owner. Stop the fleet first: `--clean`
drops tables that the shared services hold connections to.

```bash
kb-dev stop                                   # every service; Postgres keeps running
(
  cd "$KB_INFRA"/KB_Core && set -a && . ./.env && set +a
  DSN="$(.venv/bin/python .venv/bin/kb-tenant dsn <slug>)"     # or: kb-tenant dsn owner
  .venv/bin/python .venv/bin/kb-backup restore "<file>" --into "$DSN" \
      --identity ~/kb-backup-key.txt 2> "$TMPDIR/kb-restore.err"
  echo "kb-backup restore exit: $?"
  # The trigger must be back before anything reconnects (§5.1; fallback there if not).
  psql "$DSN" -Atc "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1" | grep -x nodes_notify_update
)
rm -f ~/kb-backup-key.txt "$TMPDIR/kb-restore.err"
kb-dev start
```

Run `kb-dev start` only after the exit status is 0 and the `grep` printed
`nodes_notify_update`. Use `kb-dev start`, not `kb-dev restart`: `restart` only kickstarts
loaded services, and a fleet stopped with `kb-dev stop` has none.

**B. The database is gone.**

Stop the fleet with `kb-dev stop`, as in A. Nothing may reload until the restore is done.
Pools and tenant daemons that connect to the empty database would migrate it themselves,
partway through the recovery. Then:

1. If `kb-tenant delete` also removed the registry entry, re-register the tenant first:
   `kbenv .venv/bin/python .venv/bin/kb-tenant create <slug> --email <email> --no-create-db`.
   This mints a **new** agent secret. Send it to the tenant, and redo the edge steps in
   [tenant-onboarding.md](tenant-onboarding.md) §2.2 **only**. Do **not** run `kb-dev install`
   yet.
2. Recreate the tenant's role and an empty database with the password in its registry
   entry: `kbenv .venv/bin/python .venv/bin/kb-tenant create-db <slug>` (no migrations;
   the restore applies them).
3. Restore into it and check the trigger, exactly as in A (the subshell block).
4. Run `kb-dev start`. It finds the tenant's agents unloaded (or their plists missing,
   after an offboarding) and runs `install` itself, which writes and loads them along with
   the rest of the fleet.
