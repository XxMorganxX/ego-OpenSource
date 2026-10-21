# KB Inference

A FastAPI HTTP server that runs your locally downloaded **Gemma** model (served by
**Ollama**) through a **LangChain** conversation manager. The manager lets you set a
**system prompt** and carry **conversation context** across turns, with independent
**sessions keyed by id**. It also serves a local **embedding model** (`qwen3-embedding:0.6b`)
from the same Ollama instance via `POST /embed`.

**As part of ego-OS** you don't run this by hand: `kb-run` (or the macOS launchd helper
`KB_Core/scripts/kb-dev.sh`) starts it through `KB_Core/scripts/kb-svc.sh` on port **8001**
(`KB_INFERENCE_PORT`), in its own `KB_Inference/.venv`, with every setting below taken from
`KB_Core/.env` (the stack's chat model is `gemma4:12b-it-q8_0`, which `kb-run` and
`kb-dev.sh` pull for you). The rest of this page covers running the server standalone,
where the defaults in the table below apply (port 8000).

```
                                 ┌─> conversation.py (ChatOllama)      ──> OLLAMA_MODEL
client ──HTTP──> server.py (FastAPI) ─┤                                       (Ollama)
                                 └─> embeddings.py (OllamaEmbeddings) ──> qwen3-embedding:0.6b
```

## Requirements

- [Ollama](https://ollama.com) running locally with both models pulled:
  ```bash
  ollama serve                       # if not already running
  ollama pull gemma4:12b-it-q8_0     # chat model the stack uses (or your OLLAMA_MODEL)
  ollama pull qwen3-embedding:0.6b   # embedding model
  ollama list                        # should show both
  ```
- Python 3.11+ (uv can fetch it for you)

## Install

In a virtual environment (Homebrew and recent Debian/Ubuntu Pythons refuse a global
`pip install`):

```bash
uv venv && uv pip install -r requirements.txt     # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Run the tests the way CI does: `uv run --no-project --with-requirements requirements.txt python -m pytest`.

The server reads its settings from the environment only (see the table below); it does not
load a `.env` file itself. To keep them in a file, copy `.env.example` to `.env` and start it
with `uvicorn server:app --env-file .env` (host and port then come from uvicorn's own flags).

## Run

```bash
.venv/bin/python server.py
# or:
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
```

The server binds `127.0.0.1` by default and is meant to be reached from this machine only:
see [Network exposure](#network-exposure) before changing that.

## Configuration (env vars)

| Variable                | Default                     | Purpose                          |
| ----------------------- | --------------------------- | -------------------------------- |
| `OLLAMA_MODEL`          | `gemma4:12b-it-q8_0`        | Chat model tag to run            |
| `OLLAMA_BASE_URL`       | `http://localhost:11434`    | Ollama server URL                |
| `OLLAMA_TEMPERATURE`    | `0.3`                       | Sampling temperature (low, for stable/repeatable output) |
| `OLLAMA_SEED`           | `0`                         | Sampling seed (reproducible output for a given prompt) |
| `OLLAMA_EMBED_MODEL`    | `qwen3-embedding:0.6b`      | Embedding model tag (served alongside the chat model) |
| `OLLAMA_EMBED_DIMENSIONS` | _(native)_                | Optional Matryoshka truncation of the vector length |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful assistant.` | System prompt for new sessions |
| `KB_EMBED_MAX_CONCURRENCY` | `4`                      | Max concurrent `/embed` requests (separate from chat) |
| `KB_INFERENCE_HOST` / `PORT` | `127.0.0.1` / `8000`   | Server bind address (`HOST` is still read as a fallback). Non-loopback is refused without `KB_INFERENCE_ALLOW_REMOTE=1` |
| `KB_INFERENCE_TOKEN`    | _(unset)_                   | When set, every endpoint requires `Authorization: Bearer <token>` |
| `KB_INFERENCE_ALLOW_REMOTE` | `0`                     | `1` accepts non-loopback binds and clients; requires `KB_INFERENCE_TOKEN` |
| `KB_INFERENCE_ALLOWED_HOSTS` | _(none)_               | Extra `Host` header names to accept, comma-separated (`*` is rejected) |
| `KB_INFERENCE_DEBUG_SESSIONS` | `0`                   | `1` serves `GET /sessions` and `GET /sessions/{id}` |
| `KB_INFERENCE_RETRY_AFTER` | `30`                     | Seconds sent in `Retry-After` when Ollama is unreachable or busy |

## HTTP API

| Method   | Path                     | Description                                            |
| -------- | ------------------------ | ----------------------------------------------------- |
| `GET`    | `/health`                | Ollama connectivity + chat & embedding model availability |
| `POST`   | `/chat`                  | One chat turn → `{"session", "reply"}`                |
| `POST`   | `/chat/stream`           | Same, streamed as Server-Sent Events                  |
| `POST`   | `/embed`                 | Embed text → `{"model", "dim", "count", "embeddings"}` |
| `GET`    | `/sessions`              | List active session ids (debug only, see below)       |
| `GET`    | `/sessions/{id}`         | System prompt + full history (debug only, see below)  |
| `POST`   | `/sessions/{id}/system`  | Set the system prompt for a session                   |
| `DELETE` | `/sessions/{id}`         | Reset history (`?purge=true` drops the session)       |

When Ollama itself is unreachable (stopped, restarting) or answers 503 (busy), `/chat`
and `/embed` return **503** with a `Retry-After` header instead of 500. KB_Core treats
that as a pause of the `local` provider and re-queues the note; any other model error
is still a 500, which fails that note.

### Examples

```bash
# Health
curl -s localhost:8000/health

# Set a system prompt, then chat (context is remembered per session id)
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session":"alice","system_prompt":"You are a terse pirate.","message":"Who are you?"}'

curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session":"alice","message":"What did I just ask you?"}'   # remembers prior turn

# Stream tokens (SSE)
curl -N localhost:8000/chat/stream -H 'content-type: application/json' \
  -d '{"session":"alice","message":"Count to five slowly."}'

# Embed one or many texts (1024-dim vectors from qwen3-embedding:0.6b)
curl -s localhost:8000/embed -H 'content-type: application/json' \
  -d '{"input":"the quick brown fox"}'
curl -s localhost:8000/embed -H 'content-type: application/json' \
  -d '{"input":["first note","second note"]}'

# Inspect (needs KB_INFERENCE_DEBUG_SESSIONS=1) / reset a session
curl -s localhost:8000/sessions/alice
curl -s -X DELETE localhost:8000/sessions/alice

# With KB_INFERENCE_TOKEN set, every call carries the token:
curl -s localhost:8000/health -H "Authorization: Bearer $KB_INFERENCE_TOKEN"
```

## Network exposure

The server hands a GPU-resident model to anyone who can reach it, and its in-memory
sessions hold every prompt it was fed. Every request passes three checks in
order (there are no `/docs` or `/openapi.json` routes):

1. **Host header** must be `localhost`, `127.0.0.1` or `::1`, plus any names in
   `KB_INFERENCE_ALLOWED_HOSTS`. Anything else gets `400`. This blocks DNS rebinding: a web
   page that points its own name at `127.0.0.1` still sends that name as `Host`.
2. **Client address** must be loopback (`403` otherwise) unless
   `KB_INFERENCE_ALLOW_REMOTE=1`. This also holds when the server is started with
   `uvicorn --host 0.0.0.0`, which bypasses the bind check below.
3. **Bearer token**: when `KB_INFERENCE_TOKEN` is set, a missing or wrong
   `Authorization: Bearer …` gets `401`. Unset means no token check, which is the default for
   a single-machine install.

`python server.py` refuses to bind a non-loopback address unless
`KB_INFERENCE_ALLOW_REMOTE=1`, and `KB_INFERENCE_ALLOW_REMOTE=1` is refused unless
`KB_INFERENCE_TOKEN` is set. A deliberate remote setup therefore needs all of:

```bash
KB_INFERENCE_HOST=0.0.0.0
KB_INFERENCE_ALLOW_REMOTE=1
KB_INFERENCE_TOKEN=<long random string>          # e.g. openssl rand -hex 32
KB_INFERENCE_ALLOWED_HOSTS=inference.example.internal  # the name clients will use
```

Each client also needs `KB_INFERENCE_TOKEN`. KB_Core's client sends it whenever the variable
is set. Prefer an SSH tunnel or a private network to a public listener.

`GET /sessions` and `GET /sessions/{id}` return system prompts and full conversation
history, which is whatever text callers sent. KB_Core never reads them back: its client is
stateless and purges each session. They return `404` unless
`KB_INFERENCE_DEBUG_SESSIONS=1`. `DELETE /sessions/{id}` (purge) is always available.

## Using the LangChain context manager directly (no HTTP)

`conversation.py` is usable on its own. `ConversationManager.session(...)` is a Python
context manager that sets the system prompt and keeps conversation context:

```python
from conversation import ConversationManager

manager = ConversationManager()

with manager.session("demo", system_prompt="You are a terse assistant. One sentence.") as chat:
    print(chat.send("What is Ollama?"))
    print(chat.send("Who makes the model I'm running?"))  # remembers the prior turn
    print(chat.history)                                   # list of {role, content}
```

Run the built-in smoke test:

```bash
python conversation.py
```

## Notes

- History is in-memory and lives for the process lifetime — restart clears all sessions.
- Each session serializes its own turns, so concurrent requests to the *same* session
  id won't interleave; different session ids run independently.
```
