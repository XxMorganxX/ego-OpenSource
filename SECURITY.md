# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's private vulnerability
reporting: open this repository's **Security** tab and choose **Report a
vulnerability**. Do not open a public issue, pull request or discussion for a
suspected vulnerability.

Include what you can of:

- the component (`KB_Core`, `KB_MCP`, `KB_Retrieval`, `KB_Inference`, `KB_Frontend`,
  `KB_Agent`, the setup scripts) and the version or commit,
- the configuration that matters (for example which listeners you exposed, and which
  auth mode they use),
- steps to reproduce, and the impact you expect.

Please don't include real personal notes, email or note/memory ids in a report: a
minimal synthetic reproduction is enough.

This is a small project maintained on a best-effort basis. You will get an
acknowledgement once the report has been read, and a fix or a decision before
anything is disclosed publicly. Fixes ship in the next release, credited to you
unless you ask otherwise.

## Supported versions

Only the latest release (and the current default branch) receives security fixes.

## Scope and design assumptions

ego-OS is built for one user on one machine. By default every service binds to
`127.0.0.1` and the local listeners carry no authentication; that is intended, not a
vulnerability. Reports are most useful where that boundary or an explicit opt-in can
be crossed, for example:

- reaching a service from another host or another origin (DNS rebinding, CSRF,
  a bind that is not loopback),
- bypassing the authenticated public MCP listener, the dashboard's access check,
  the ingest receiver's HMAC or the agent gateway's request signing,
- one tenant reading or changing another tenant's data,
- ingested content (a note, an email, a webhook payload) that makes a connected agent
  or the enrichment pipeline act beyond what the owner asked for,
- secrets or personal data written somewhere they should not be (logs, backups,
  world-readable files).

Exposing a service publicly is covered in [docs/exposure.md](docs/exposure.md).
