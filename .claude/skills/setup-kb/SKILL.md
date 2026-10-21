---
name: setup-kb
description: Set up ego-OS — a personal knowledge base — on this machine for the user, end to end, from a folder of their Markdown notes. USE THIS SKILL when the user says "set up my knowledge base", "install ego-OS", "get the KB running", "stand up the KB", or asks how to start using this repo. The agent does the installing, configuring, and checking; the user answers four decisions.
---

# Set up ego-OS for this user

1. Read `docs/setup/README.md` **in full** before running anything. It is the
   source of truth; this skill adds nothing to it.
2. Ask the four decisions from its "The four decisions" section in **one**
   message, and nothing else unless a check fails.
3. Execute Phases 0–8 in order. Each phase's **Check** must pass before the
   next phase starts. Follow its **If not** list before improvising.
4. Obey the guide's **Guardrails** section for the whole run — never commit
   `.env`, never print secret values, never `docker compose down -v` after
   ingest without a separate confirmation, install nothing without a yes.
5. End with the guide's **Closing report**, values filled in.

If the guide and this skill ever disagree, the guide wins; fix the skill.
