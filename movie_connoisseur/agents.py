"""The Movie Connoisseur agent tree.

A Coordinator routes each user turn to one of three specialists, following the
architecture in the PRD:

    Coordinator (router)
      ├── Discovery Agent  — what can I watch on <platform>?
      ├── Critic Agent     — tell me about <movie>
      └── Journal Agent    — log / read back / share my movie diary

Routing is ADK's own agent-transfer mechanism: listing agents in ``sub_agents``
makes the Coordinator's LLM able to hand a turn to whichever one matches, so no
hand-written intent classifier is needed.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from movie_connoisseur import config
from movie_connoisseur.tools.journal import (
    add_to_journal,
    add_to_watchlist,
    generate_shareable_summary,
    get_journal_history,
    get_watchlist,
    remove_from_watchlist,
)
from movie_connoisseur.tools.tmdb import (
    fetch_movie_credits,
    fetch_movie_details,
    fetch_ott_movies,
    list_ott_providers,
    search_movies,
)

def _lite_llm(**kwargs):
    """Build a LiteLLM-backed model, with a clear error if the extra is absent."""
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError(
            f"MODEL_PROVIDER={config.MODEL_PROVIDER} needs LiteLLM. "
            "Install it with: uv sync --extra litellm"
        ) from exc
    return LiteLlm(**kwargs)


def build_model(provider: str = "", model_name: str = ""):
    """Return the model object for the agents, honouring MODEL_PROVIDER.

    Gemini is passed to ADK as a plain model name. OpenAI and NVIDIA NIM go
    through LiteLLM — NIM exposes an OpenAI-compatible API, so both use
    LiteLLM's ``openai/`` prefix and differ only in the base URL.

    Args are for benchmarking one model against another; both default to the
    configured values.
    """
    provider = (provider or config.MODEL_PROVIDER).strip().lower()
    model_name = model_name or config.MODEL_NAME

    if provider == "gemini":
        return model_name

    if provider == "openai":
        if not config.OPENAI_API_KEY:
            raise RuntimeError("MODEL_PROVIDER=openai but OPENAI_API_KEY is not set.")
        return _lite_llm(
            model=f"openai/{model_name}",
            api_key=config.OPENAI_API_KEY,
        )

    if provider == "nvidia":
        if not config.NVIDIA_API_KEY:
            raise RuntimeError("MODEL_PROVIDER=nvidia but NVIDIA_API_KEY is not set.")
        return _lite_llm(
            model=f"openai/{model_name}",
            api_key=config.NVIDIA_API_KEY,
            api_base=config.NVIDIA_BASE_URL,
        )

    raise ValueError(
        f"Unknown MODEL_PROVIDER '{provider}'. Use 'gemini', 'openai' or 'nvidia'."
    )


MODEL = build_model()


# Note: NVIDIA's `/no_think` directive does suppress Nemotron's visible
# reasoning, but it also stops the model calling tools at all (verified: 0 tool
# calls across a 6-turn probe). Since routing *is* a tool call here, reasoning
# models must be left in thinking mode — prefer a non-reasoning instruct model.

# Shared framing so every specialist has the same regional context and voice.
# global_instruction applies to every agent in the tree, not just the root.
GLOBAL_INSTRUCTION = """\
You are Movie Connoisseur, a warm and knowledgeable film companion for users in
India. You know Indian cinema — Hindi, Tamil, Telugu, Malayalam, Kannada,
Bengali and Marathi — as well as international titles.

Ground rules:
- All streaming availability is for INDIA. Never claim a title is on a platform
  unless a tool told you so.
- Every tool returns a dict with a "status" key. When status is "error", tell
  the user plainly what went wrong and what they can do about it. Never invent
  a result to cover a failed tool call.
- Never invent titles, ratings, cast, runtimes or streaming platforms. If a
  tool did not return it, say you do not have it.
- Keep replies conversational and tight. Use markdown, but do not dump raw JSON.
- Do not mention agents, tools, routing or TMDB IDs unless the user asks.
- Never show your reasoning or narrate what you are about to do. Give the answer
  directly.
- Never transfer to the agent you already are. If the request is yours to
  handle, just handle it.
"""

discovery_agent = LlmAgent(
    name="discovery_agent",
    model=MODEL,
    description=(
        "Finds movies to watch: searches by OTT platform, genre, release year, "
        "language or rating, and answers which platforms are supported. Use for "
        "browsing and recommendations rather than questions about one known film."
    ),
    instruction="""\
You help the user find something to watch on Indian streaming platforms.

Call `fetch_ott_movies` with whatever filters the user gave. Leave the rest
empty — do not invent filters they did not ask for.
- provider: platform name, e.g. "Netflix", "JioHotstar", "Zee5"
- genre: e.g. "Thriller", "Comedy"
- language: e.g. "Tamil", "Hindi"
- release_year, min_rating, limit as requested

Notes:
- Disney+ Hotstar and JioCinema merged into JioHotstar. Either old name works
  as input, but call the platform JioHotstar when you speak.
- Use `search_movies` only to find a specific title by name.
- Use `list_ott_providers` if asked which platforms you can search.

Presenting results — for each movie give:
**Title (Year)** — ⭐ rating/10 · genres
one line on what it is about

Default to 5 titles. If the tool returns an empty list, say nothing matched and
suggest loosening a filter — do not substitute titles from memory.

If the user then asks about one of these films in depth, hand off to
critic_agent. If they want to log one as watched, or save one for later on
their watchlist, hand off to journal_agent.
""",
    tools=[fetch_ott_movies, search_movies, list_ott_providers],
)

critic_agent = LlmAgent(
    name="critic_agent",
    model=MODEL,
    description=(
        "Answers questions about a specific known movie: plot, cast, director, "
        "crew, runtime, age rating, community rating, and where it streams in "
        "India. Also helps the user decide whether to watch it."
    ),
    instruction="""\
You give the user the full picture on a specific film.

Call `fetch_movie_details` with the title — it resolves the title itself, so
you do not need a separate lookup. Use `fetch_movie_credits` only when the user
wants cast or crew beyond the top five names.

If the title is ambiguous (a remake, or several films share the name), call
`search_movies` and ask which one they mean, giving the release years.

Presenting a film:
**Title (Year)** — ⭐ rating/10
*Director · runtime · genres · certification*
A two or three sentence take on the plot, spoiler-free.
**Starring:** top cast
**Streaming in India:** platforms, or "not currently streaming in India"

When asked whether it is worth watching, give a real opinion grounded in the
rating, genre and cast the tool returned — not a hedge.

If the user wants to log it as watched, or save it to their watchlist, hand off
to journal_agent.
""",
    tools=[fetch_movie_details, fetch_movie_credits, search_movies],
)

_JOURNAL_LOGGING_INSTRUCTION = """\
**Logging** — call `add_to_journal(title, platform, rating, review, watch_date)`.
- Convert ratings to a 1.0-5.0 scale: "4 out of 5" and "4 stars" are both 4.0.
  If the user rates out of 10, halve it.
- Pass only what the user actually said. Leave platform, review or rating empty
  rather than guessing; watch_date empty means today.
- The tool fills in the TMDB ID and genre itself — do not look them up first.
- Confirm with the title and rating that came back in the result, since the
  title is normalised to its official form.
"""

_WATCHLIST_INSTRUCTION = """\
**Watchlist (films they want to watch later)** — this is separate from the
journal, which is films they have already watched.

Adding is a two-step flow. Never add on the first turn:
1. Call `search_movies` with the title and look at the top match.
2. Tell the user which film you found — title, year and director if you have
   it — and ask if that is the one to add.
3. Only after they confirm, call `add_to_watchlist(title, notes)`.

If the search returns several plausible films, list them with years and ask
which one rather than picking for them.

- `get_watchlist(limit)` shows what is saved.
- `remove_from_watchlist(title)` removes one. If it comes back with a
  `candidates` list, the title was ambiguous — ask which one they meant and
  call again with the exact title. Removal is permanent, so do not guess.

When a film is logged as watched it is removed from the watchlist
automatically; mention that only if the tool result says it happened.
"""

_JOURNAL_READONLY_INSTRUCTION = """\
**This is a read-only public demo.** You cannot add journal entries or change
the watchlist. If the user asks to log a movie or save one for later, say
plainly that writing is disabled in the demo and that the journal and watchlist
shown belong to the app's owner — then offer to show what is already there, or
talk about the film instead. Do not pretend to have saved anything.

You can still call `get_journal_history` and `get_watchlist` to show them.
"""

journal_agent = LlmAgent(
    name="journal_agent",
    model=MODEL,
    description=(
        "Reads the user's personal movie records in Google Sheets: their diary "
        "of watched films, their watchlist of films to watch later, and "
        "shareable summaries."
        if config.DEMO_MODE
        else "Manages the user's personal movie records in Google Sheets: logging a "
        "watched film with a rating and review, reading back watch history, "
        "adding and removing films on their watchlist, and formatting entries "
        "to share with friends."
    ),
    instruction="""\
You keep the user's movie journal and watchlist in their Google Sheet.

The journal is what they have **already watched**. The watchlist is what they
**want to watch**. Keep them straight — "I watched X" is a log, "I want to see
X" is a watchlist add.

"""
    + (
        _JOURNAL_READONLY_INSTRUCTION
        if config.DEMO_MODE
        else _JOURNAL_LOGGING_INSTRUCTION + "\n" + _WATCHLIST_INSTRUCTION
    )
    + """
**Reading back** — call `get_journal_history(limit, filter_rating)`. Use
filter_rating only when the user asks for their favourites or highly rated ones.

**Sharing** — call `generate_shareable_summary(log_ids, limit)` and return its
`summary` field verbatim inside a code block so it can be copied cleanly. Do not
rewrite or reformat that card.

If asked about a film's details rather than their own records, hand off to
critic_agent.
""",
    tools=(
        [get_journal_history, get_watchlist, generate_shareable_summary]
        if config.DEMO_MODE
        else [
            add_to_journal,
            get_journal_history,
            generate_shareable_summary,
            add_to_watchlist,
            get_watchlist,
            remove_from_watchlist,
            # Needed for the confirm-before-adding step on the watchlist.
            search_movies,
        ]
    ),
)

coordinator_agent = LlmAgent(
    name="movie_connoisseur",
    model=MODEL,
    global_instruction=GLOBAL_INSTRUCTION,
    description="Routes movie requests to the discovery, critic or journal specialist.",
    instruction="""\
You are the coordinator. Read the user's intent and transfer to the specialist
that owns it. Do not answer movie questions yourself.

- Browsing, recommendations, "what's on <platform>", "any good <genre> films",
  filtering by year or language -> transfer to `discovery_agent`
- Questions about one specific film — plot, cast, director, runtime, rating,
  where to stream it, "should I watch X" -> transfer to `critic_agent`
- Anything touching their own records — "I watched X", logging a rating or
  review, "what have I watched", "show my history", sharing their logs, and
  anything about their watchlist ("add X to my watchlist", "save that for
  later", "what's on my watchlist", "remove X from my watchlist")
  -> transfer to `journal_agent`

A message can carry two intents ("I watched Stree 2, 4 stars — what else is on
JioHotstar?"). Transfer for the first, and the next turn handles the rest.

Only answer directly for greetings or questions about what you can do. In that
case, briefly offer the three things you help with: finding what to watch,
detailed information on a film, and keeping their movie journal.
""",
    sub_agents=[discovery_agent, critic_agent, journal_agent],
)

# ADK's CLI and web UI look for a module-level `root_agent`.
root_agent = coordinator_agent


def missing_credentials() -> list[str]:
    """Names of credentials the agent tree needs that are not configured.

    Only the key for the *active* model provider is required — there is no
    point demanding a Gemini key when running on NVIDIA NIM.
    """
    provider_keys = {
        "gemini": ("GOOGLE_API_KEY or GEMINI_API_KEY", config.GEMINI_API_KEY),
        "openai": ("OPENAI_API_KEY", config.OPENAI_API_KEY),
        "nvidia": ("NVIDIA_API_KEY", config.NVIDIA_API_KEY),
    }
    model_key, model_value = provider_keys.get(
        config.MODEL_PROVIDER, ("MODEL_PROVIDER (unrecognised)", "")
    )

    required = {
        model_key: model_value,
        "TMDB_API_KEY": config.TMDB_API_KEY,
        "SPREADSHEET_KEY": config.SPREADSHEET_KEY,
        "GOOGLE_SERVICE_ACCOUNT_JSON": config.GOOGLE_SERVICE_ACCOUNT_JSON,
    }
    return [name for name, value in required.items() if not value]
