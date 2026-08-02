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
from movie_connoisseur.tools.omdb import fetch_external_ratings
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


# Building the model must not raise at import time. A missing key is a
# configuration problem the UI can explain — if it escapes from here it becomes
# an unreadable stack trace before the app can render anything at all.
MODEL_ERROR = ""
try:
    MODEL = build_model()
except (RuntimeError, ValueError) as exc:
    MODEL_ERROR = str(exc)
    # Placeholder so the agent tree still constructs. Nothing can call it:
    # missing_credentials() reports the problem and the UI stops first.
    MODEL = config.MODEL_NAME


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

DISCOVERY_DESCRIPTION = (
    "Finds movies to watch: searches by OTT platform, genre, release year, "
    "language or rating, and answers which platforms are supported. Use for "
    "browsing and recommendations rather than questions about one known film."
)

DISCOVERY_INSTRUCTION = """\
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
"""

CRITIC_DESCRIPTION = (
    "Answers questions about a specific known movie: plot, cast, director, "
    "crew, runtime, age rating, and how it was received — the TMDB community "
    "score plus IMDb, Rotten Tomatoes and Metacritic. Also covers where it "
    "streams in India, and helps the user decide whether to watch it."
)

CRITIC_INSTRUCTION = """\
You give the user the full picture on a specific film.

Call `fetch_movie_details` with the title — it resolves the title itself, so
you do not need a separate lookup. Use `fetch_movie_credits` only when the user
wants cast or crew beyond the top five names.

If the title is ambiguous (a remake, or several films share the name), call
`search_movies` and ask which one they mean, giving the release years.

**Critic scores** — `fetch_movie_details` returns only TMDB's community score.
For IMDb, Rotten Tomatoes or Metacritic, call `fetch_external_ratings`. Reach
for it whenever the user asks about reviews, critics, IMDb, Rotten Tomatoes,
scores, or how well a film was received.
- Pass the `imdb_id` from `fetch_movie_details` when you already have it; it is
  exact. Otherwise just pass the title.
- Any of the three can come back empty, which is normal rather than an error —
  Metacritic in particular is missing for most Indian films. Say a score is not
  available; never guess one or fill it in from memory.
- The result also carries the film's year, language and country. Use those if
  you mention them. Do not state a year or language the tools did not give you.

Presenting a film:
**Title (Year)** — ⭐ rating/10
*Director · runtime · genres · certification*
A two or three sentence take on the plot, spoiler-free.
**Starring:** top cast
**Streaming in India:** platforms, or "not currently streaming in India"

When you have fetched critic scores, add a line, omitting any that are absent:
**Critics:** IMDb 8.8/10 · Rotten Tomatoes 87% · Metacritic 74

Note the scales differ — IMDb is out of 10, Rotten Tomatoes and Metacritic out
of 100. Never present them as if they were the same scale.

When asked whether it is worth watching, give a real opinion grounded in the
ratings, genre and cast the tools returned — not a hedge. Where the scores
disagree, say so: TMDB's smaller voter base often under-rates Indian cinema
relative to IMDb.

If the user wants to log it as watched, or save it to their watchlist, hand off
to journal_agent.
"""

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

JOURNAL_DESCRIPTION_READONLY = (
    "Reads the user's personal movie records in Google Sheets: their diary "
    "of watched films, their watchlist of films to watch later, and "
    "shareable summaries."
)

JOURNAL_DESCRIPTION_FULL = (
    "Manages the user's personal movie records in Google Sheets: logging a "
    "watched film with a rating and review, reading back watch history, "
    "adding and removing films on their watchlist, and formatting entries "
    "to share with friends."
)

_JOURNAL_PREAMBLE = """\
You keep the user's movie journal and watchlist in their Google Sheet.

The journal is what they have **already watched**. The watchlist is what they
**want to watch**. Keep them straight — "I watched X" is a log, "I want to see
X" is a watchlist add.

"""

_JOURNAL_EPILOGUE = """
**Reading back** — call `get_journal_history(limit, filter_rating)`. Use
filter_rating only when the user asks for their favourites or highly rated ones.

**Sharing** — call `generate_shareable_summary(log_ids, limit)` and return its
`summary` field verbatim inside a code block so it can be copied cleanly. Do not
rewrite or reformat that card.

If asked about a film's details rather than their own records, hand off to
critic_agent.
"""

# Read-only tools, safe for any visitor.
READ_TOOLS = [get_journal_history, get_watchlist, generate_shareable_summary]

# Tools that mutate the owner's spreadsheet. Withheld unless writes are allowed.
WRITE_TOOLS = [add_to_journal, add_to_watchlist, remove_from_watchlist]

COORDINATOR_INSTRUCTION = """\
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
"""


def build_agent_tree(write_enabled: bool | None = None, model=None) -> LlmAgent:
    """Build a Coordinator with its three specialists.

    A factory rather than module-level singletons because both write permission
    and model choice are per-visitor once sign-in is involved: the signed-in
    owner gets the write tools and may pick a provider, everyone else gets
    neither. Streamlit serves many sessions from one process, so this must not
    be decided by module globals.

    Args:
        write_enabled: whether this tree may modify the owner's spreadsheet.
            Defaults to the deployment-wide setting.
        model: a model name or LiteLlm object from ``build_model``. Defaults to
            the deployment-wide model.
    """
    if write_enabled is None:
        write_enabled = config.WRITE_ENABLED
    if model is None:
        model = MODEL

    discovery_agent = LlmAgent(
        name="discovery_agent",
        model=model,
        description=DISCOVERY_DESCRIPTION,
        instruction=DISCOVERY_INSTRUCTION,
        tools=[fetch_ott_movies, search_movies, list_ott_providers],
    )

    critic_agent = LlmAgent(
        name="critic_agent",
        model=model,
        description=CRITIC_DESCRIPTION,
        instruction=CRITIC_INSTRUCTION,
        tools=[
            fetch_movie_details,
            fetch_movie_credits,
            search_movies,
            fetch_external_ratings,
        ],
    )

    journal_agent = LlmAgent(
        name="journal_agent",
        model=model,
        description=(
            JOURNAL_DESCRIPTION_FULL if write_enabled else JOURNAL_DESCRIPTION_READONLY
        ),
        instruction=(
            _JOURNAL_PREAMBLE
            + (
                _JOURNAL_LOGGING_INSTRUCTION + "\n" + _WATCHLIST_INSTRUCTION
                if write_enabled
                else _JOURNAL_READONLY_INSTRUCTION
            )
            + _JOURNAL_EPILOGUE
        ),
        tools=(
            # search_movies is needed for the confirm-before-adding step.
            READ_TOOLS + WRITE_TOOLS + [search_movies]
            if write_enabled
            else list(READ_TOOLS)
        ),
    )

    return LlmAgent(
        name="movie_connoisseur",
        model=model,
        global_instruction=GLOBAL_INSTRUCTION,
        description=(
            "Routes movie requests to the discovery, critic or journal specialist."
        ),
        instruction=COORDINATOR_INSTRUCTION,
        sub_agents=[discovery_agent, critic_agent, journal_agent],
    )


# The deployment-default tree. Per-session trees are built by the UI.
coordinator_agent = build_agent_tree()
discovery_agent, critic_agent, journal_agent = coordinator_agent.sub_agents

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
