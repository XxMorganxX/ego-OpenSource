# Recipe: push anything in over HTTP (optional)

Written to the agent. The ingest daemon runs an HMAC-authenticated push
receiver on `127.0.0.1:8030`. Anything on this machine that can make an HTTP
request — a browser clipper, a chat-export script, a cron job — can add
memories without touching the notes folder.

Keep the receiver on loopback. Its only protection is the shared
`KB_INGEST_SECRET` (no per-sender identity, rate limit or revocation), so do
not publish it through a tunnel or reverse proxy.

## Endpoint

```
POST http://127.0.0.1:8030/ingest/webhook
X-KB-Timestamp: <unix seconds>
X-KB-Signature: <hex HMAC-SHA256, keyed by KB_INGEST_SECRET, of
                 "{timestamp}\nPOST\n/ingest/webhook\n{sha256 hex of the raw body}">
Content-Type: application/json
```

The timestamp must be within 5 minutes of the receiver's clock, and the same
signed request is accepted only once. Bodies over 8 MiB are refused (`413`).

Body:

```json
{
  "event_type": "upsert",
  "external_id": "clip-2026-09-07-001",
  "payload": {
    "external_id": "clip-2026-09-07-001",
    "title": "Why local-first matters",
    "content": "Full text of the clipped article or note …",
    "parent_path": "Inbox",
    "tags": ["clip", "web"],
    "uri": "https://example.com/post"
  }
}
```

- `event_type` is `upsert` (default) or `delete`. A delete needs only the
  top-level `external_id`.
- `payload.external_id` is **required** and is the stable identity: the same id
  later updates the same memory instead of creating a new one.
- `title`, `content`, `parent_path`, `slug`, `tags`, `uri` are optional.
  `parent_path` is a dotted folder path under the webhook source's tree
  (`Inbox`, `Inbox.Clips`); an empty string means the root. If `content` is
  empty the title (or id) is used as content.

The receiver answers `202 {"queued": <n>}`; the event is staged durably and
applied on the source's next drain (every 60 s by default).

## A working `curl`

Run from the repository root. The signer reads `KB_INGEST_SECRET` from `KB_Core/.env` inside
Python, so the secret never appears on a command line (`ps` shows every argv).

```bash
python3 - <<'PY'
import hashlib, hmac, json, pathlib, time, urllib.request
env = dict(l.split("=", 1) for l in pathlib.Path("KB_Core/.env").read_text().splitlines()
           if "=" in l and not l.lstrip().startswith("#"))
secret = env["KB_INGEST_SECRET"].strip().strip("\"'")
body = json.dumps({"event_type": "upsert", "external_id": "hello-1", "payload": {
    "external_id": "hello-1", "title": "Hello from the webhook",
    "content": "First pushed memory.", "parent_path": "Inbox", "tags": ["test"]}}).encode()
ts, path = str(int(time.time())), "/ingest/webhook"
canonical = f"{ts}\nPOST\n{path}\n{hashlib.sha256(body).hexdigest()}".encode()
sig = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://127.0.0.1:8030" + path, data=body, method="POST", headers={
    "Content-Type": "application/json", "X-KB-Timestamp": ts, "X-KB-Signature": sig})
print(urllib.request.urlopen(req).read().decode())
PY
```

Expected: `{"queued":<n>}` — `n` is the staged event's row id, so it is `1`
only on a fresh inbox and climbs from there. Sign the **exact bytes** you
send; re-serialising JSON after signing changes whitespace and yields `401`.
A `401` also means a stale timestamp (check the clock) or a request that was
already accepted once (sign each send afresh). Python senders can use
`kb_core.ingest.receiver.signed_headers(secret, "/ingest/webhook", body)`.

## Enrichment is off by default for this source

`KB_Core/sources.toml` ships the webhook source with `is_enrichable = false`,
so pushed memories are embedded and searchable but gain no entities or
concepts. To enrich them like notes, set

```toml
[webhook]
is_enrichable = true
```

and restart `kb-run`. With `claude-cli` routing every pushed item then spends
quota — say so before flipping it.

## Check

```bash
grep -E 'drain webhook: seen=' "$HOME/kb-run.log" | tail -2     # seen=1 changed=1 within ~60 s of the curl
```

Then search for "Hello from the webhook" through the user's MCP client.
