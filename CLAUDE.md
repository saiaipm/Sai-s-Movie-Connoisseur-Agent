# Sai's Streaming Companion — working notes

Conversational multi-agent assistant for Indian OTT movie discovery, film
metadata, and a Google Sheets movie journal. Google ADK + Streamlit.

Spec: `Movie Connoisseur Agent PRD.md` (v1.1 — §8 lists where v1.0 was wrong).
Setup and usage: `README.md`.

## Environment

- **No system Python.** The machine has only the Microsoft Store stubs. `uv`
  manages the toolchain — every command is `uv run ...`.
- The project lives under **OneDrive**, which breaks uv's hardlinking
  (os error 396). `link-mode = "copy"` in `pyproject.toml` handles it.
- **Stop the Streamlit server before `uv sync`** — a running app locks
  `.venv/.../dist-info` and the sync fails with "Access is denied".

## Commands

```bash
uv run pytest                                   # 82 offline tests, no keys needed
uv run streamlit run app.py                     # the app
uv run python scripts/chat_cli.py --scenarios   # replay the 3 PRD workflows
uv run python scripts/smoke_test.py --sheets    # live tool check (writes a sheet row)
uv run python scripts/verify_providers.py       # re-check OTT provider IDs
```

## Architecture

`app.py` → `chat.MovieChat` (sync wrapper over the async ADK runner, owns one
event loop) → `agents.coordinator_agent` → three specialists → `tools/`.

Routing is ADK's own `sub_agents` transfer, not a hand-written classifier. The
coordinator has **no tools of its own** by design — giving it any would let it
answer instead of routing.

## Two sheets, one spreadsheet

`Movie_Journal` (watched) and `Watchlist` (want to watch) are separate
worksheets, both auto-created. `_open_worksheet(title, headers)` is the shared,
cached opener — headers are passed as a tuple so it stays hashable.

The watchlist add flow is **confirm-then-write**, enforced by instruction rather
than by ADK's tool-confirmation machinery: the agent calls `search_movies`,
presents the match, and only calls `add_to_watchlist` after the user agrees.
That is why `search_movies` is in `journal_agent.tools`. A framework-level
confirmation gate would need the Streamlit layer to render and answer pending
confirmations — deliberately not built.

## Conventions that matter

- **Tools never raise.** Every tool returns `{"status": "success"|"error", ...}`.
  ADK function tools need this; a raised exception kills the turn. Tests assert
  it (`test_missing_api_key_is_reported`, `test_details_request_appends...`).
- **Tool signatures are the LLM's API.** Type hints and docstrings become the
  function schema, so argument names and docstring wording change behaviour.
  Plain types with defaults (`str = ""`, `int = 0`) — not `Optional[...]`.
- **Tests stay offline.** `tests/` must pass with no keys and no network. The
  `no_network` fixture in `test_tools.py` enforces it where a call could leak.

## Live-verified facts that contradict intuition

- **JioHotstar is 2336.** Disney+ Hotstar (122) and JioCinema (220) both merged
  into it in 2025 and now return zero results. Old names are kept as input
  aliases in `config.OTT_PROVIDERS`.
- **`api.themoviedb.org` is blocked by Indian ISPs** (TLS reset). `tools/tmdb.py`
  fails over to `api.tmdb.org`, TMDB's own alias, and caches whichever answers.
  A 401 deliberately does *not* trigger failover — a bad key isn't a network
  problem.
- **Gemini free tier: 5 req/min on flash, 15 on flash-lite.** A single user turn
  costs 2–3 model calls, so non-lite flash throttles almost immediately. This is
  why the default is `gemini-3.1-flash-lite`, not raw capability.
- **`gemini-2.5-flash` (the PRD's model) is retired** — 404 on new API keys. The
  models `list` endpoint still returns it, so listing is not proof of access;
  probe with an actual `generate_content` call.

## Model providers

`MODEL_PROVIDER` = `nvidia` (default) | `gemini` | `openai`. The first and last
go through LiteLLM (now a core dependency, since NIM is the default). NIM is
OpenAI-compatible, so both use LiteLLM's `openai/` prefix and differ only by
`api_base`.

Default model is `nvidia/nvidia-nemotron-nano-9b-v2` — free, 5/6 on the routing
probe. `gemini-3.1-flash-lite` scores 6/6 but is quota-limited. Benchmarks and
rejected candidates are in README.

**Provider choice is per-session, like write access, and gated on the same
`trusted` flag** (deployment `WRITE_ENABLED`, or signed in as owner). The rule
lives in `config.resolve_session_provider()` — deliberately in config, not
`app.py`, so it is testable without importing Streamlit. An untrusted session is
pinned to `FREE_PROVIDER` whatever the secrets say, so a visitor can never spend
money; the sidebar hiding the dropdown is cosmetic, not the guarantee.
`config.default_model_for()` prevents one provider's model name leaking to
another (`gpt-4o-mini` against NIM would 404).

The whole architecture depends on function calling — routing between agents *is*
a tool call (`transfer_to_agent`). **Never adopt a model without running
`scripts/chat_cli.py --scenarios` first.** Failure modes seen in practice:
corrupted tool names (`gpt-oss-20b`), 10-minute timeouts (`llama-3.3-70b`), and
models answering from memory instead of calling a tool.

### Reasoning models: two traps

- **`/no_think` breaks tool calling.** It does suppress Nemotron's visible
  reasoning, but the model then makes zero tool calls. Reasoning must stay on.
- **Filter thought parts, don't disable thinking.** ADK marks reasoning as parts
  with `thought=True`; `chat.py` skips them. Without that filter the reply text
  is the model's internal monologue. This bit us once — don't remove it.

## Write access

**Writing is opt-in: `WRITE_ENABLED` must be explicitly true, and `DEMO_MODE`
is simply `not WRITE_ENABLED`.** This inversion is deliberate — a deployment
that configures nothing must be read-only, never wide open. Do not "simplify"
it back to a `DEMO_MODE` flag that defaults to false.

Two routes to write access: `WRITE_ENABLED` (deployment-wide, used locally) or
signing in as `OWNER_EMAIL` (per-session, used on the public deploy).

### Permission is per-session — do not make it global

One Streamlit process serves every visitor. Write permission therefore lives in
**ADK session state** (`journal.WRITE_ENABLED_STATE_KEY`), set by `MovieChat`
and read by `journal.writes_allowed(tool_context)` on each call. A module-level
flag would leak the signed-in owner's access to concurrent anonymous visitors.

This is why:
- `agents.build_agent_tree(write_enabled)` is a factory, not module singletons —
  the toolset and instructions differ per visitor
- the three write tools take `tool_context: ToolContext = None`; ADK injects it
  and **excludes it from the LLM schema**, so the model cannot forge permission
- `config.WRITE_ENABLED` is only the fallback for callers with no session
  (scripts, tests)

`tests/test_write_permission.py` pins all of this down, including the leak case
(deployment allows writes, session does not) and that `tool_context` never
appears in a function declaration.

`tests/conftest.py` pins writes on for the suite so tests do not depend on
whether a developer's `.env` enables them. The reload-based tests in
`test_demo_mode.py` stub `dotenv.load_dotenv`, because reloading config would
otherwise pull `.env` back in and defeat `monkeypatch.delenv`.

There is exactly one Google Sheet (the owner's) and no BYO — a deliberate
decision, not an omission.

Write protection is deliberately layered:
1. Write tools are withheld from `journal_agent.tools` — `add_to_journal`,
   `add_to_watchlist`, `remove_from_watchlist`
2. Each of those returns an error if `DEMO_MODE` is set, even if called directly
3. `generate_shareable_summary` skips its `Shared_Status` write

**Any new write tool must get both layers.** `tests/test_demo_mode.py` keeps a
`WRITE_TOOLS` set and asserts no agent in the tree holds one in demo mode — add
new write tools to that set.

Demo mode also **forces `MODEL_PROVIDER` to `nvidia`** (the free one) and
discards a model name belonging to another provider, so a stray secret in the
Streamlit dashboard cannot start billing the owner. `PROVIDER_WAS_FORCED`
records when this happened and the sidebar surfaces it.

Keep all of this. Tests in `tests/test_demo_mode.py` assert them, including that
the sheet is never even opened for writing in demo mode.

## Deployment caution

Streamlit Community Cloud apps are **public and unauthenticated** — any visitor
spends the owner's API quota. `MAX_MESSAGES_PER_SESSION` caps this (defaults to
10 when `DEMO_MODE` is on). Cloud installs from `requirements.txt`, not
`pyproject.toml`, so that file must be kept in sync with `[project.dependencies]`.

Never commit `.env`, `service_account.json` or `.streamlit/secrets.toml` (all
gitignored).

**Streamlit secrets gotchas, both hit in production:**

- The service account JSON must be wrapped in `'''`, not `"""`. TOML basic
  strings (`"""`) expand the `\n` escapes inside `private_key` into real
  newlines, producing invalid JSON. `service_account_info()` catches this and
  explains it rather than surfacing a bare `JSONDecodeError`.
- `get_secret` falls back to `st.secrets`, which reads a local
  `.streamlit/secrets.toml` if one exists. That file is therefore a **live
  local config source**, not just a clipboard for deployment — a stale copy can
  shadow what you expect. Environment variables still take precedence.

Nothing may raise at import time on a misconfigured deploy: `build_model()`
failures are captured as `agents.MODEL_ERROR` and rendered by the UI, because a
raised exception there kills the app before it can explain itself.
