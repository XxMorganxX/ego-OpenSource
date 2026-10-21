# Recipe: email (optional)

Written to the agent. Any process that writes Markdown into the notes folder is
a source; email is one example, and an entirely optional one. Only build this
if the user asks for it.

## The contract

ego-OS doesn't ship a mail connector: the user brings the exporter (their mail
client's export, a script against their provider's API, an IMAP fetcher) and it
writes Markdown into the notes folder in the shape below. The engine treats
whatever lands there like any other note.

### Files the exporter writes

Anything that can export mail can feed the knowledge base. Contract:

- One file per **thread** (not per message) under `NOTES/Email/<YYYY>/`,
  named from the date and subject, for example
  `2026-09-07 Re- design review.md`.
- Frontmatter with at least:

  ```yaml
  ---
  date: 2026-09-07
  from: Avery Quinn <avery@example.com>
  subject: Re: design review
  tags: [email]
  ---
  ```

- Body: the thread in reading order, quoted replies stripped, signatures and
  disclaimers dropped. Enrichment quality tracks body quality.
- Idempotent: re-running the export rewrites the same file for the same thread
  (same name), so the daemon updates rather than duplicates.

## What the engine does with email

Email is ingested like any other note. Nothing decays by default: the shipped
policy has no decay rules. A thread from last quarter is rarely as load-bearing
as a design note, so to make email fade over weeks, add a `[[decay]]` rule to
`KB_Core/policy.toml` (see the [policy setup guide](../../../KB_Core/docs/policy-setup.md)):

```toml
[[decay]]
name = "email"
match = { tags = ["email"] }   # every memory carrying the `email` tag
half_life_days = 21
floor = 0.2
```

A matched, dated memory's influence in concept clustering and entity promotion
then decays from 1.0 toward `floor`; retrieval is not affected. Keep the `email`
tag and the `date:` field (undated memories never decay) on whatever the
exporter writes so the rule applies. Validate with `uv run kb-policy check` from
`KB_Core/`, then restart the services.

## Check

```bash
NOTES=/absolute/path/to/notes      # the guide's KB_OBSIDIAN_VAULT
ls "$NOTES/Email" | head
grep -E 'pull obsidian: seen=' "$HOME/kb-run.log" | tail -2
```

Then search for a subject line through the user's MCP client and confirm the
thread comes back.
