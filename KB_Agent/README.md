# kb-agent

Runs on your Mac. Watches your Obsidian vault, pushes changed notes to the hosted index,
and executes the index's enrichment jobs through **your own** `claude -p` login. Nothing
on your machine listens on the network; the agent only makes outbound HTTPS calls.

    uv tool install "ego-os-agent @ git+https://github.com/XxMorganxX/Ego-OpenSource.git#subdirectory=KB_Agent"
    kb-agent init --gateway https://kb-agent.example.com --tenant you \
        --cf-client-id <service token client id> --cf-client-secret <service token client secret> \
        --vault ~/Obsidian          # prompts for the agent secret from the owner
    kb-agent status

Install from this repository's URL, as above (or the URL the owner sends you), never by
bare name. The distribution is `ego-os-agent` (it depends on `ego-os-core`); the command it
installs is `kb-agent`. This project does not publish either distribution to PyPI; both
install from source, from this repository. A bare `uv tool install ego-os-agent` (or
`kb-agent`) would resolve the name on PyPI instead, which is not this project. The name
before `@` must be `ego-os-agent`: uv refuses an install whose name does not match the
package metadata.

`kb-agent init` installs a launchd agent (`com.kb.agent`) so it runs at login.

## What the agent sends

Every request goes over HTTPS to the gateway URL given at `init`, and carries these headers:

| Header | Value |
|---|---|
| `X-KB-Tenant` | your tenant slug |
| `X-KB-Timestamp` | the current Unix time, in seconds |
| `X-KB-Nonce` | 32 random hex characters, new for every request |
| `X-KB-Signature` | the request signature (see below) |
| `X-KB-Agent-Version` | the kb-agent version |
| `X-KB-Agent-Id` | a random id made once at `init` and kept in `config.toml` (`install_id`). It lets the owner tell two installs apart. It is not derived from the machine, the user name or the hostname. |
| `CF-Access-Client-Id`, `CF-Access-Client-Secret` | the Cloudflare Access service token, when one was given at `init` |

The signature is `hex(HMAC-SHA256(agent secret, "{timestamp}\n{nonce}\n{METHOD}\n{path}\n{query}\n{sha256 hex of the body}"))`,
where `query` is the raw query string without the `?` (empty when there is none). The
gateway refuses a timestamp more than five minutes off its clock and a nonce it has already
accepted, so a captured request cannot be sent again.

Request bodies carry:

- **Notes** (`POST /v1/notes`): for each new or changed Markdown note, its path relative to
  the vault, its full text, its modification and creation times, and its stable id (the
  `id:` the agent stamps into the note's front matter). A deleted note sends only its id.
  Files matched by `exclude` are never read or sent.
- **Manifest** (`POST /v1/manifest`): the ids of every note currently in the vault, so the
  index can drop notes deleted while the agent was off.
- **Job results** (`POST /v1/jobs/<id>`): the output of each enrichment job your `claude -p`
  ran, or a short error and retry time when it failed.

`GET /v1/jobs` and `GET /v1/status` send no body.
