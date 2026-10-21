# Tenant onboarding

How to host another person (a **tenant**) on your host. Each tenant gets:

- their own Postgres database, `kb_t_<slug>`, owned by their own Postgres login (role
  `kb_t_<slug>`). Only that role can connect to it; it cannot connect to your database or
  to another tenant's, and your services' app role cannot connect to theirs,
- an identity at the edge: the Cloudflare Access email they sign in with,
- a `kb-agent` on **their** Mac that pushes their Obsidian vault and runs every LLM job
  through **their own** Claude login.

The host runs no LLM work for tenants.
Backups and restore: [backup-restore.md](backup-restore.md).

**Privacy.** Tell every tenant this before they install anything: each tenant's full note
text is stored in plaintext in their database on the host, and the operator (anyone with
this user account, root, or the backup private key) can read it.

## Conventions

- `<dashboard-host>`, `<mcp-host>` and `<agent-host>` are the public hostnames you chose on
  your zone for the dashboard, the MCP endpoint and the agent gateway. `<slug>` is the
  tenant's short name (`^[a-z][a-z0-9_]{1,30}$`, not `owner`), `<email>` their Access email.
- `$KB_INFRA` is the directory you cloned the repo into (for example
  `export KB_INFRA=~/src/ego-OS`). `kb-dev` means `KB_Core/scripts/kb-dev.sh`.
- Launchd labels use the prefix `com.kb`. If `KB_SERVICE_PREFIX` is set in `.env`, use
  that prefix instead.
- **Secrets never go into a document, a ticket, or a chat log.** Values in `<angle brackets>` are
  placeholders. That covers `POSTGRES_PASSWORD`, `KB_APP_PASSWORD`, agent secrets, and
  service-token secrets.
- `kb-tenant` and `kb-backup` do **not** read `KB_Core/.env` themselves, and the registry
  path (`tenants.toml`) is relative to the working directory. Run them through this
  helper so they see the same database credentials, `KB_GATEWAY_URL`, `KB_BACKUP_DIR`, and
  registry as the services. The helper runs in a subshell, so your login shell never
  holds the credentials. Every `kb-tenant` and `kb-backup` command in these runbooks
  carries it, and you must never run them any other way. Run from another directory, they
  read and write the wrong `tenants.toml`. `create --no-create-db`, for one, would write a
  fresh, secret-bearing `tenants.toml` into whatever directory you happen to be in:

  ```bash
  kbenv() { (cd "$KB_INFRA"/KB_Core && set -a && . ./.env && set +a && "$@"); }
  ```

## 1. Prerequisites (once per host)

### 1.1 Software and backups

- The deployed code has the entry points:
  `ls "$KB_INFRA"/KB_Core/.venv/bin/{kb-tenant,kb-backup,kb-agent-gateway}`.
- Backups are set up and have run once. See [backup-restore.md](backup-restore.md) §1–2.
  `kb-tenant delete` refuses to run without a backup from the last 24 h.

### 1.2 Edge: the gateway hostname

Agents reach the host at `<agent-host>`. People never use that hostname. Create the
Access application **before** the hostname resolves, so it is never reachable without
Access (the same rule `<mcp-host>` followed).

1. **Zero Trust → Access → Applications → Add → Self-hosted**, domain `<agent-host>`.
   Give it exactly one policy, with action **Service Auth** and Include → *Service Token*.
   You add each tenant's token in §2.2. Do not add an email Allow policy to this app.
   ⚠️ Unverified: the dashboard may refuse to save a Service Auth policy whose Include
   names no token. If it does, create the first tenant's service token (§2.2 step 1)
   first, or a placeholder token you delete once a real one is in the policy.
2. Add the ingress to your tunnel's cloudflared config (the system tunnel's is
   `/etc/cloudflared/config.yml`), above the catch-all `- service: http_status:404` rule:

   ```yaml
     - hostname: <agent-host>
       service: http://127.0.0.1:8040
   ```

   Then reload the tunnel and route the hostname to it:

   ```bash
   sudo launchctl kickstart -k system/com.cloudflare.cloudflared
   cloudflared tunnel route dns <tunnel-id> <agent-host>   # once; <tunnel-id> = `tunnel:` in the config
   ```

   Use `127.0.0.1`, not `localhost`: cloudflared otherwise dials `[::1]`, and the gateway
   binds IPv4 loopback only.

### 1.3 `KB_Core/.env`

Add these lines:

```bash
KB_DASHBOARD_AUTH=cf-access
KB_CF_ACCESS_AUD_DASHBOARD=<the dashboard Access app's Application Audience (AUD) tag>
KB_GATEWAY_URL=https://<agent-host>
KB_BACKUP_AGE_RECIPIENT=<age public key, age1…>
```

- `KB_DASHBOARD_AUTH=cf-access` makes the dashboard API verify the Access JWT itself and
  map the email to a tenant. It also needs `KB_CF_ACCESS_TEAM_DOMAIN`, which is already set
  for `mcp-public`. The AUD tag comes from the **dashboard's** app (`<dashboard-host>`), not the
  MCP app (`KB_CF_ACCESS_AUD`).
  **Before turning it on**, confirm that `vite preview`'s `/api` proxy forwards the
  `Cf-Access-Jwt-Assertion` header and the `CF_Authorization` cookie to the backend. If it does not, every dashboard API call fails, the owner's included. To
  back out, remove the line and run `kb-dev restart backend`.
  There is no local bypass: once this is on, the owner's plain
  `http://localhost:8091` gets `401` from the API too, so use `https://<dashboard-host>`.
- `KB_GATEWAY_URL` is read only by `kb-tenant create`, which prints it in the bundle.
- Optional settings: `KB_GATEWAY_PORT` (default `8040`; if you change it, change the
  ingress too), `KB_TENANTS_CONFIG` (default `tenants.toml` in `KB_Core/`), and
  `KB_ADMIN_DATABASE_URL` (see §2.1). For `KB_BACKUP_DIR` and `KB_BACKUP_INTERVAL`, see
  [backup-restore.md](backup-restore.md).

Then load the two shared services, `agent-gateway` (:8040) and `backup`:

```bash
kb-dev install
kb-dev status          # agent-gateway running; backup ran once at load (RunAtLoad)
```

`kb-dev install` rewrites and reloads **every** agent, the owner's included. Expect a
short blip, and possibly a healthcheck "unreachable" alert while services come back.

### 1.4 The registry file

`kb-tenant` maintains `KB_Core/tenants.toml`. The file is gitignored, written `0600`, and
holds every tenant's `agent_secret`. **`kb-backup` does not back it up** (it dumps
databases only), so copy it into your password manager by hand after every `create`,
`rotate-secret`, `enable`, `disable`, and `delete`. A stale copy is dangerous: restoring a
copy taken before a `disable` silently re-enables a revoked tenant. The same goes for `KB_Core/tenants/<slug>/` if you put
per-tenant policy or prompt overlays there. A tenant's own policy file (`policy = "..."` in
its registry entry) gets no prompt overlay unless its `[prompts]` section asks for one, and a
relative `overlay_dir` there resolves beside that file, never against `KB_Core/`, so it can
never pick up the owner's `prompts.local/`.

If you lose the registry, the databases survive. To recover, re-register each tenant
with `kbenv .venv/bin/python .venv/bin/kb-tenant create <slug> --email <email> --no-create-db`
(which skips database creation), then send the tenant the new secret.

Running services re-read `tenants.toml` within 10 s of a change. If a hand edit fails
validation, they keep the previous registry and log
`WARNING: tenants.toml reload failed, keeping previous registry: …`.
`kbenv .venv/bin/python .venv/bin/kb-tenant list` fails loudly on an invalid file.

## 2. Add a tenant

### 2.1 Provision

```bash
kbenv .venv/bin/python .venv/bin/kb-tenant create <slug> --email <email>
```

- To give the tenant more addresses, repeat `--email`. Emails are lowercased, and each one
  can belong to only one tenant (the owner counts as a tenant here).
- What it does, in order:
  1. Closes your own database to tenant logins: grants `CONNECT` on it to your app role
     (the user in `KB_DATABASE_URL`) and revokes it from `PUBLIC`. Idempotent; it prints
     `<database>: CONNECT limited to <role> and superusers`. Any other role that reached
     your database only through `PUBLIC` loses access here, so grant it `CONNECT` by hand
     if you rely on one.
  2. Creates the login role `kb_t_<slug>` (no superuser, no `CREATEDB`/`CREATEROLE`, no
     inheritance) with a random password, then the database `kb_t_<slug>` (`OWNER` = that
     role, `TEMPLATE template0`) with `CONNECT` revoked from `PUBLIC`, and the `ltree`,
     `vector`, and `pg_trgm` extensions. It refuses if the role or the database already
     exists.
  3. Applies every migration as the tenant's role, so it owns every table it uses.
  4. Creates `KB_Core/tenants/<slug>/`.
  5. Writes the registry entry (`0600`), including the role's `db_password`, as the last
     step. If any step fails, it drops the half-made database and role. When the registry has no `[owner] emails` yet, the same write pins
     them from `KB_CF_ACCESS_EMAILS` (lowercased) and says so: from then on the registry,
     not the env list, names the owner, so an address later added to `KB_CF_ACCESS_EMAILS`
     never silently becomes the owner. To change the owner's addresses, edit `[owner]
     emails` in `tenants.toml`.
- Steps 1–2 (and `create-db`, `rotate-db-password`, `delete`) use the **admin**
  connection: `KB_ADMIN_DATABASE_URL`, or else `POSTGRES_USER`/`POSTGRES_PASSWORD` on
  `localhost:$DB_HOST_PORT`, which is the bootstrap role. It needs `CREATEROLE` and
  `CREATEDB` (the docker bootstrap superuser has both). Creating and dropping roles and
  databases are the only times anything connects as that role.
- Every service reaches a tenant's database only as that tenant's role: `Registry.dsn_for`
  takes host, port and options from `KB_DATABASE_URL` and swaps in the tenant's login, and
  refuses a tenant that has none rather than fall back to your credentials. The tenant
  never needs the password: their agent talks only to the gateway.
- Options: `--vault-layer` (default `Obsidian`; the top-level path layer the tenant's
  notes land under) and `--no-create-db` (registry entry only; make the role and empty
  database afterwards with `kb-tenant create-db <slug>`, which applies no migrations).

It prints `created kb_t_<slug>: <n> migration(s) applied`, then:

```
=== onboarding bundle (send over a private channel) ===
tenant        = <slug>
agent_secret  = <64 hex characters>
gateway       = https://<agent-host>
install       = uv tool install "ego-os-agent @ git+ssh://git@github.com/<org>/<repo>@<deployed commit sha>#subdirectory=KB_Agent"
init          = kb-agent init --gateway https://<agent-host> --tenant <slug> --cf-client-id <service token client id> --vault ~/path/to/vault   (prompts for the agent_secret and the CF client secret)
edge          = add the emails to the MCP + dashboard Access policies; create an Access service token named <slug> (docs/ops/tenant-onboarding.md)
```

Copy the `agent_secret`. The `install` line names the repository: `kb-tenant create` reads it
from `git remote get-url origin` of the checkout it runs in, and pins it to that
checkout's `HEAD` (`@<sha>`) so the tenant's agent matches the deployed server. That
commit must be pushed to the remote before the tenant installs. An https remote is
printed as ssh, and no password or token embedded in any remote is ever printed. If it
cannot read a remote, the line says `git+<repo-url>`, and you fill that in. The line never names a bare package:
this project does not publish the `ego-os-agent` or `ego-os-core` distributions to PyPI
(both install from source; the agent's command is still `kb-agent`), and a bare
`uv tool install ego-os-agent` (or `kb-agent`) would resolve the name on PyPI instead. **Never
send a tenant an install command without the `@ git+…#subdirectory=KB_Agent` part.** The
`init` line leaves the service token as placeholders: fill them in from §2.2. Send these
two lines together with §3.

### 2.2 Edge

1. **Zero Trust → Access → Service Auth → Service Tokens → Create service token**, named
   `<slug>`. Copy the Client ID and Client Secret into the bundle; the secret is shown only
   once. When the token's duration ends, the edge refuses the agent until you issue a
   new token.
2. Add that token to the `<agent-host>` app's Service Auth policy
   (Include → Service Token).
3. Add `<email>` to the Allow policies of both the MCP app (`<mcp-host>`) and the
   dashboard app (`<dashboard-host>`), or to an Access group both policies use.

### 2.3 Load the tenant's daemons

```bash
kb-dev install
kb-dev status          # four new rows: t.<slug>.ingest / reanalyze / resolve / consolidate
```

This writes and loads `com.kb.t.<slug>.{ingest,reanalyze,resolve,consolidate}`. Each runs
`kb-svc.sh <name>` with `KB_TENANT=<slug>`, which means:

- the tenant's own database and policy,
- `inference.tenant.toml`: every completion goes to provider `agent`; embeddings stay
  local,
- `sources.tenant.toml`: source `vault` only,
- no push receiver and no local vault.

Logs go to `~/Library/Logs/kb/t.<slug>.<name>.log`. `kb-dev start` also runs `install`
whenever a tenant's plist is missing.

You do **not** need `kb-dev restart backend mcp-public agent-gateway`. Those services
re-read `tenants.toml` within 10 s.

### 2.4 Send the bundle

Over a private channel, send:

- the slug,
- the `agent_secret`,
- the service token's Client ID and Client Secret,
- `https://<agent-host>`,
- §3 of this document.

## 3. What the tenant does (on their Mac)

1. Install `uv`:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install Claude Code and sign in. Every enrichment job runs on this login and counts
   against their own Claude plan.

   ```bash
   claude auth login
   ```

3. Install the agent. This needs collaborator access to the repository:

   ```bash
   uv tool install "ego-os-agent @ git+ssh://git@github.com/<org>/<repo>.git#subdirectory=KB_Agent"
   ```

4. Configure the agent and install it as a login service:

   ```bash
   kb-agent init --gateway https://<agent-host> --tenant <slug> \
       --cf-client-id <service token client id> \
       --vault ~/Obsidian/<vault> [--exclude "Private/**"]
   ```

   `init` prompts for the agent secret (`agent secret (from the owner):`), so it stays out
   of `ps` and the shell history; `--secret <agent_secret>` also works, but lands in the
   history. It needs `claude` on the shell's `PATH`: it records that binary's absolute path
   in the config (`claude_path`) and puts its directory first on the LaunchAgent's `PATH`,
   because launchd's own `PATH` does not include nvm, volta, asdf or npm-prefix installs.
   It prints `wrote …/config.toml`, then `installed …/com.kb.agent.plist (runs at login;
   logs in ~/Library/Logs/kb-agent)`.

   `init` writes `~/Library/Application Support/kb-agent/config.toml` (`0600`) and installs
   the LaunchAgent `com.kb.agent`, which runs at login. `--exclude` takes a glob and can be
   repeated; excluded notes never leave the Mac. The other options are `--concurrency`
   (concurrent `claude` jobs, 1–3, default 2) and `--no-launchd`. If Claude Code is
   reinstalled somewhere else later, re-run `init` with the same flags.

   The Cloudflare service-token secret is prompted for too (or read from
   `KB_AGENT_CF_CLIENT_SECRET`); the gateway must be `https://` (plain `http://` is only
   accepted for a loopback test gateway). `--secret`, if used, lands in the shell's
   history. Afterwards, the `0600` config file is the only place that needs them. Remove
   the line from history, for example by editing `~/.zsh_history`, or start the command
   with a space when `HIST_IGNORE_SPACE` is set.

   If the vault lives under `~/Documents`, `~/Desktop` or iCloud Drive, macOS privacy
   controls (TCC) gate the LaunchAgent's first read. Allow the prompt if one appears (it
   names the Python interpreter, not `kb-agent`); if none does, add that interpreter under
   System Settings → Privacy & Security → Full Disk Access. Until then every scan logs
   `scan could not read directory <vault root>: Operation not permitted` in `agent.log`.

   Each job runs `claude -p` on this Mac, and Claude Code keeps those transcripts (prompt
   and reply, so note text) under `~/.claude/projects/` here, as for any other `claude`
   use. They never leave the Mac through the agent.

5. Check the agent:

   ```bash
   kb-agent status
   ```

   The output should include `claude   logged in as <email>` and
   `gateway  ok · open jobs … · inbox pending … · last seen …`.

6. In claude.ai, open Settings → Connectors and add a custom connector,
   `https://<mcp-host>/mcp`. When Access prompts, sign in with `<email>`.

7. Open `https://<dashboard-host>` and sign in with the same email.

The first sync pushes the whole vault, and enrichment jobs follow. Progress shows on the
dashboard's Ingestion page. `kb-agent sync` scans and pushes changed notes now;
`kb-agent sync --full` also posts the manifest, which prunes notes deleted from the vault
(the agent withholds the manifest whenever the scan reported an error). `kb-agent
uninstall` removes the LaunchAgent and leaves the config and state in place.

## 4. Rotate an agent secret

```bash
kbenv .venv/bin/python .venv/bin/kb-tenant rotate-secret <slug>
```

This prints `<slug>: new agent_secret = …`, and the gateway refuses the old secret within
10 s. The tenant then re-runs `kb-agent init` with the same flags and enters the new
secret at its prompt (§3 step 4).
Copy the updated `tenants.toml` into your password manager (§1.4).

### 4.1 Rotate a tenant's database password

```bash
kbenv .venv/bin/python .venv/bin/kb-tenant rotate-db-password <slug>
```

This sets a new password on role `kb_t_<slug>` and writes it to `tenants.toml`. Running
services keep connections opened with the old password and open new ones from the
registry they loaded at start, so restart the fleet afterwards (`kb-dev restart`). The
tenant does nothing. Copy the updated `tenants.toml` into your password manager (§1.4).

## 5. Revoke and offboard

### 5.1 Revoke (reversible)

```bash
kbenv .venv/bin/python .venv/bin/kb-tenant disable <slug>
```

Within 10 s, the gateway refuses the agent (401), and the MCP server and the dashboard
refuse the email. To undo, run `kbenv .venv/bin/python .venv/bin/kb-tenant enable <slug>`. Also revoke the access at the
edge:

- revoke the `<slug>` service token,
- remove `<email>` from the MCP and dashboard Allow policies.

`disable` does **not** stop the tenant's four daemons. `kb-dev install` and `kb-dev start`
skip disabled tenants but do not unload them, so the daemons keep running against the
tenant's database until you remove them (§5.2 step 2).

### 5.2 Delete (irreversible)

Do these steps **in this order**:

1. **Revoke** as in §5.1.
2. **Stop and remove the tenant's daemons while the tenant is still in the registry:**

   ```bash
   for n in ingest reanalyze resolve consolidate; do
     launchctl bootout "gui/$(id -u)/com.kb.t.<slug>.$n"
     rm -f ~/Library/LaunchAgents/com.kb.t.<slug>.$n.plist
   done
   ```

   `kb-dev` finds tenant daemons only through `tenants.toml`. After `kb-tenant delete`,
   the tenant's agents are invisible to `kb-dev stop`, `uninstall`, and `status`. They stay
   loaded, crash-loop every 10 s on `unknown tenant`, and launchd reloads their plists at
   every login. The loop above also cleans up if that has already happened.
   `kb-dev stop` takes no service names: it stops the whole fleet (disabled tenants
   included) and leaves every plist on disk, so it is not a substitute for this loop.
3. **Take a fresh backup.** `kb-tenant delete` refuses without a backup of
   `kb_t_<slug>` from the last 24 h under `KB_BACKUP_DIR`. Disabled tenants are still
   backed up.

   ```bash
   cd "$KB_INFRA"/KB_Core && bash scripts/kb-svc.sh backup
   ```

4. **Delete.** **This is irreversible: the database is gone for good.** Only a restore from
   `kb-backup` brings it back ([backup-restore.md](backup-restore.md) §5). `--no-backup`
   skips the backup check; do not use it for a real tenant.

   ```bash
   kbenv .venv/bin/python .venv/bin/kb-tenant delete <slug> --yes
   ```

   `delete` works in this order:
   1. Validates the whole registry before touching anything. It refuses if
      `tenants.toml` is invalid, and takes the database name from the parsed entry, which is
      always `kb_t_<slug>`.
   2. Checks for the backup.
   3. Terminates connections to the database.
   4. Runs `DROP DATABASE kb_t_<slug> WITH (FORCE)`. `FORCE` means a reconnecting client
      cannot block the drop. Then `DROP ROLE kb_t_<slug>`.
   5. Removes the registry entry.

   On success it prints `dropped database and role kb_t_<slug>; registry entry removed;
   backups under … kept`.
5. **Clean up what `delete` leaves behind:**
   - The tenant's backups under `KB_BACKUP_DIR/kb_t_<slug>/`. Retention prunes only
     databases still in the registry, so these files stay until you delete them by hand.
     Delete them once you no longer need to keep them.
   - `KB_Core/tenants/<slug>/` and `~/Library/Logs/kb/t.<slug>.*.log`.
   - Copy the updated `tenants.toml` into your password manager.

## 6. Troubleshooting

### `kb-agent status` on the tenant's Mac

| Line | Meaning | Fix |
|---|---|---|
| `claude   NOT logged in; run: claude auth login` | The agent still syncs notes but claims no jobs. | Run `claude auth login`. On the server, jobs expire after 600 s (`[provider.agent] timeout` in `inference.tenant.toml`). Provider `agent` then pauses (see below). |
| `claude   claude CLI not found at <path>; re-run kb-agent init` | The `claude` binary recorded at `init` is gone (Claude Code moved or was uninstalled). Jobs are deferred, not failed. | Reinstall Claude Code if needed, then re-run `kb-agent init` with the same flags. |
| `gateway  401: clock skew of <n> min between this Mac and the server; fix the clock (…)` | The request timestamp is more than 300 s off the server clock. | Turn on automatic date and time on the Mac. |
| `gateway  401: secret or tenant rejected (ask the owner to check tenants.toml)` | Wrong secret or slug, the secret was rotated, or the tenant is disabled or deleted. | Check `kbenv .venv/bin/python .venv/bin/kb-tenant list`, then re-run `kb-agent init` with the current secret. |
| `gateway  error 403: …` | The Cloudflare edge refused the request before it reached the gateway. | The service token is wrong, expired, revoked, or missing from the Service Auth policy (§2.2). |
| `gateway  error 429: …` | The rate limit was hit: 120 requests/min per tenant on notes and manifest. | None; the agent retries. |
| `gateway  ok · open jobs … · inbox pending … · last seen …` | Healthy. | None. |

On a 401, the server's `agent-gateway.log` gives the real reason:
`denied <METHOD> <path>: <reason>`, where `<reason>` is one of
`unknown or disabled tenant '<slug>'`, `bad signature`, `timestamp skew <n>s`,
`bad timestamp`, `missing or bad nonce`, or `replayed nonce` (the same signed request sent
twice; the agent never does this, so it points at a replay or a proxy that retries
requests). The tenant only ever sees a neutral `401`.

### Logs

- On the tenant's Mac: `~/Library/Logs/kb-agent/agent.log` (rotated at 5 MB, three
  backups kept) and `~/Library/Logs/kb-agent/launchd.log` (anything printed before logging
  starts, such as a config error at launch).
- On the host:
  - `~/Library/Logs/kb/t.<slug>.{ingest,reanalyze,resolve,consolidate}.log`
    (`kb-dev logs t.<slug>.ingest`),
  - `~/Library/Logs/kb/agent-gateway.log` (`kb-dev logs agent-gateway`),
  - `~/Library/Logs/kb/backup.log`.

Neither side logs note content or prompt text.

### Provider `agent` paused (the tenant's Ingestion page)

When the tenant's machine does not answer, their tenant daemons record a `provider_pause`
row for provider `agent` in **their** database only. The tenant's Ingestion page then
shows a banner:

```
agent paused · <reason> · resumes <time> — queued work waits and restarts automatically
```

| `<reason>` | Cause | Resumes |
|---|---|---|
| `no agent answered job <id> within 600s` | The Mac is asleep or offline, the agent is stopped, or Claude is logged out. | After `KB_AGENT_PAUSE_MINUTES` (default 15). |
| `agent reported provider outage for job <id>: …` | The tenant's Claude quota is exhausted. | At the `retry_after` time the agent reported. |

No one needs to act. Queued nodes wait, and enrichment restarts on its own once the agent
answers. MCP search keeps answering from the existing index the whole time.

### Daemons crash-looping on `unknown tenant`

The slug is no longer in `tenants.toml`, but its agents are still loaded. Run the loop in
§5.2 step 2.
