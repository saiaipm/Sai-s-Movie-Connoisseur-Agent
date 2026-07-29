# Movie Connoisseur

A conversational multi-agent assistant for discovering movies on Indian OTT
platforms, pulling rich film metadata, and keeping a personal movie journal in a
Google Sheet. Built on the Google Agent Development Kit (ADK) with a Streamlit
frontend.

See [Movie Connoisseur Agent PRD.md](Movie%20Connoisseur%20Agent%20PRD.md) for the
full product spec.

## Status

| Phase | Scope | State |
| :---- | :---- | :---- |
| 1 | Tool construction (TMDB + Google Sheets) | Done, verified against both live APIs |
| 2 | Multi-agent assembly (Coordinator, Discovery, Critic, Journal) | Done, all 3 PRD workflows verified |
| 3 | Streamlit frontend (`app.py`) | Done, verified in browser |
| 4 | Deploy to Streamlit Community Cloud | Ready to deploy |

## Layout

```
app.py               Streamlit frontend — chat tab + journal tab
movie_connoisseur/
  config.py          credentials, model choice, OTT provider IDs, genre maps, sheet schema
  agents.py          the four agents and their instructions
  chat.py            synchronous wrapper around the ADK runner
  tools/
    tmdb.py          Discovery and Critic tools (TMDB REST API)
    journal.py       Journal tools (Google Sheets via gspread)
scripts/
  chat_cli.py        terminal REPL for talking to the agent tree
  smoke_test.py      exercises every tool against the live APIs
  verify_providers.py checks the OTT provider IDs against TMDB's current India list
tests/
  test_tools.py      offline tests for the tools
  test_agents.py     offline tests for agent wiring and error handling
```

## Setup

This project uses [uv](https://docs.astral.sh/uv/). It manages the Python
toolchain itself, so no separate Python install is needed.

```bash
uv sync
```

Then copy `.env.example` to `.env` and fill in the four credentials:

- `GEMINI_API_KEY` — from [Google AI Studio](https://aistudio.google.com/apikey)
- `TMDB_API_KEY` — TMDB v3 key, from Settings → API on themoviedb.org
- `GOOGLE_SERVICE_ACCOUNT_JSON` — path to the service account key file locally,
  or the raw JSON on one line for Streamlit Cloud
- `SPREADSHEET_KEY` — the long ID in your sheet's URL

### Google Sheets access

1. In Google Cloud Console, enable the **Google Sheets API** and the **Google
   Drive API**.
2. Create a service account and download its JSON key to the project root as
   `service_account.json` (already gitignored).
3. Open the target spreadsheet and **share it with the service account's email**
   (the `client_email` field in the JSON) with Editor access. This step is the
   usual cause of a `SpreadsheetNotFound` error.

The `Movie_Journal` worksheet and its header row are created automatically on
the first write, so an empty spreadsheet is fine.

## Verifying Phase 1

Offline tests, no credentials needed:

```bash
uv run pytest
```

Live check against TMDB (and optionally Google Sheets — note that `--sheets`
writes one throwaway row):

```bash
uv run python scripts/smoke_test.py --sheets
```

Confirm the hardcoded OTT provider IDs still match TMDB's India catalogue:

```bash
uv run python scripts/verify_providers.py
```

## Running the app

```bash
uv run streamlit run app.py
```

Opens at `http://localhost:8501` with two tabs:

- **Chat** — talk to the agent tree. Each reply is tagged with the specialist
  that handled it, and a "Tool calls" expander shows what it actually called
  with which arguments.
- **Journal** — a live read of the Google Sheet with totals, average rating and
  most-used platform, plus a link straight to the spreadsheet.
- **Watchlist** — films saved to watch later.

The sidebar reports credential status, the active model and TMDB host, offers
example prompts, and resets the conversation.

### Or from the terminal

```bash
uv run python scripts/chat_cli.py
```

`--scenarios` replays the three PRD workflows; `--debug` shows which agent
handled each turn and which tools it called.

## Choosing a model

Default: **`nvidia/nvidia-nemotron-nano-9b-v2`** on NVIDIA NIM, because it is
free to run — which is what makes a public deployment viable at all.

Every candidate was benchmarked on the same six-turn routing probe (crossing all
three specialists, including "tell me about the first one" and "log that one"),
not chosen from documentation:

| Model | Routing | Notes |
| :---- | :---- | :---- |
| `nvidia/nvidia-nemotron-nano-9b-v2` | **5/6** | Free. Default. |
| `gemini-3.1-flash-lite` | **6/6** | Best accuracy, but quota-limited |
| `openai/gpt-oss-20b` | 4/6 | Fast (1.3s) but emits corrupted tool names |
| `openai/gpt-oss-120b` | — | Works, but ~60s per call |
| `meta/llama-3.3-70b-instruct` | — | Timed out (>10 min) on the free tier |

Nemotron's one miss: asked "is Fight Club worth watching?" it answered from
memory instead of handing off to the critic and calling a tool. Gemini handed
off correctly. If accuracy matters more than cost, switch.

Switch provider in `.env`:

```
MODEL_PROVIDER=gemini
```

Valid providers are `nvidia` (default), `gemini` and `openai`, each with its own
default model. Set `MODEL_NAME` to override.

Gemini's free tier caps non-lite flash models at 5 requests/minute; since one
user turn costs 2–3 model calls, `gemini-3.6-flash` throttles almost
immediately. `gemini-3.1-flash-lite` gets 15/min.

### Two things to know about reasoning models

Both were found by testing, and both are handled in the code:

- **Reasoning must stay on.** NVIDIA's `/no_think` directive does suppress
  Nemotron's visible chain-of-thought — but it also stops the model calling
  tools at all (0 tool calls across a 6-turn probe). Since routing *is* a tool
  call here, disabling reasoning breaks the whole architecture.
- **Chain-of-thought must be filtered, not disabled.** ADK marks reasoning as
  parts with `thought=True`. `chat.py` skips those; without that filter the
  user sees "Okay, the user asked for…" instead of an answer.

## Deploying a public demo

The full app writes to the owner's Google Sheet. A public Streamlit app has **no
authentication** — anyone with the URL would be writing to that sheet and
spending the owner's API quota.

**Writing is opt-in.** `WRITE_ENABLED` must be explicitly true; everything else
is read-only. This is deliberately the inverse of the obvious design, so that a
deployment where you forget to configure anything fails *closed*:

| | Local (`WRITE_ENABLED=true`) | Public (unset) |
| :---- | :---- | :---- |
| Discovery, Critic | full | full |
| Read journal & watchlist | yes | yes |
| **Write to sheet** | **yes** | **no** |
| `Shared_Status` updates | yes | skipped |
| Message cap | unlimited | 10 per session |
| Model provider | your choice | **forced to free (NIM)** |

Write protection is enforced twice: write tools are withheld from the agent's
toolset *and* refuse if called anyway. Same codebase, no fork.

Setting `DEMO_MODE=true` pins read-only even if `WRITE_ENABLED` is set
somewhere, as a belt-and-braces override.

The read-only state is shown in the sidebar, in the chat pane (the sidebar
collapses on mobile), and on both data tabs.

### Steps

1. Push to GitHub. `.env`, `service_account.json` and `.streamlit/secrets.toml`
   are gitignored — confirm with `git status` before the first push.
2. On [share.streamlit.io](https://share.streamlit.io), create an app pointing
   at this repo, main file `app.py`.
3. In **Settings → Secrets**, paste the contents of
   `.streamlit/secrets.toml.example` with real values. **Do not add
   `WRITE_ENABLED`** — leaving it out is what keeps the app read-only.

Cloud installs from `requirements.txt`, not `pyproject.toml`.

Note that NVIDIA NIM's free credits are finite. When they run out the app will
error rather than silently misbehave — but it will stop working, so a recorded
walkthrough is worth having as a durable backup.

## Tools

All tools return a dict with a `status` key of `"success"` or `"error"` rather
than raising, which is the contract ADK function tools need.

**Discovery** — `fetch_ott_movies(provider, genre, release_year, language, min_rating, limit)`,
`search_movies(query, limit)`, `list_ott_providers()`

`provider` and `genre` accept either a name (`"Netflix"`, `"Thriller"`) or a
TMDB ID (`"8"`, `"53"`), reconciling the two forms used in the PRD.

**Critic** — `fetch_movie_details(title_or_id)`, `fetch_movie_credits(movie_id, cast_limit)`

`fetch_movie_details` accepts a title or an ID, and returns the plot, director,
top cast, formatted runtime (`2h 21m`), Indian age certification, community
rating, and which Indian platforms currently stream it.

**Journal** — `add_to_journal(title, platform, rating, review, watch_date)`,
`get_journal_history(limit, filter_rating)`,
`generate_shareable_summary(log_ids, limit)`

`add_to_journal` looks up the TMDB ID and genre from the title automatically, so
the agent does not need a separate lookup call first. `generate_shareable_summary`
sets `Shared_Status` to `TRUE` on the rows it includes.

**Watchlist** — `add_to_watchlist(title, notes)`, `get_watchlist(limit)`,
`remove_from_watchlist(title)`

Films the user *wants* to watch, kept in a separate `Watchlist` worksheet
(`Watchlist_ID, Added_Date, Movie_Title, TMDB_ID, OTT_Platform, Genre, Notes`),
created automatically on first use. It is deliberately not a flag on
`Movie_Journal` — "want to watch" and "have watched" have different lifecycles.

Behaviour worth knowing:

- **Adding is confirmed first.** The agent searches, shows what it found with
  the year, and only adds after the user agrees — so "add Maharaja" surfaces the
  1998, 2024 and other versions rather than guessing.
- **Adding is idempotent.** A title already saved returns `already_present`
  rather than duplicating.
- **Removal is permanent**, so an ambiguous title returns a `candidates` list
  instead of deleting the wrong row.
- **Logging a film as watched removes it from the watchlist**, and never fails
  the log if that cleanup errors.

## Known deviations from the PRD

**The PRD's OTT provider IDs are out of date.** Verified against TMDB's live
India list on 2026-07-29:

| PRD | Reality |
| :---- | :---- |
| Disney+ Hotstar `122` | Retired — no results in India |
| JioCinema `220` | Retired — no results in India |
| — | Both merged into **JioHotstar `2336`** |

`config.py` now uses `2336`, keeping "Hotstar", "Disney+ Hotstar" and
"JioCinema" as aliases so users can still ask for them by the old names.
Netflix `8`, Prime Video `119`, Zee5 `232` and SonyLIV `237` were confirmed
correct. Re-run `scripts/verify_providers.py` whenever results look wrong.

**TMDB's main API domain is blocked by some Indian ISPs.** `api.themoviedb.org`
gets its TLS connections reset; `api.tmdb.org` — TMDB's own alias for the same
API — is not blocked. `tools/tmdb.py` tries the hosts in order and caches
whichever answers, so no VPN is needed and nothing breaks on a normal network.
Set `TMDB_BASE_URL` in `.env` to pin one host and skip the probe.

**Other differences:**

- The PRD specifies `fetch_ott_movies(provider_id, genre_id, release_year)` but
  its sample dialogue calls it with `genre="Thriller"`. The implementation
  accepts both, and adds `language`, `min_rating` and `limit`.
- `google-adk` installs at 2.x, not the 1.x the PRD was written against. This
  does not affect Phase 1 (the tools are plain functions) but the Phase 2 agent
  wiring will follow the 2.x API.
