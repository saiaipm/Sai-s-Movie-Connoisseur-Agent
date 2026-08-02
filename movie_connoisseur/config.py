"""Configuration, credential loading and static lookup tables.

Credentials resolve in this order so the same code runs locally and on
Streamlit Community Cloud:

1. Process environment (``.env`` is loaded automatically when present).
2. ``st.secrets`` when running inside a Streamlit app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def get_secret(name: str, default: str = "") -> str:
    """Return a credential from the environment, falling back to Streamlit secrets."""
    value = os.environ.get(name)
    if value:
        return value

    try:
        import streamlit as st

        # Accessing st.secrets outside a Streamlit runtime raises, hence the guard.
        return str(st.secrets[name])
    except Exception:
        return default


# --- Credentials ----------------------------------------------------------

TMDB_API_KEY = get_secret("TMDB_API_KEY")

# OMDb supplies the IMDb, Rotten Tomatoes and Metacritic ratings that TMDB does
# not carry. Optional: without it those columns simply stay empty.
OMDB_API_KEY = get_secret("OMDB_API_KEY")
OMDB_BASE_URL = get_secret("OMDB_BASE_URL", "https://www.omdbapi.com/")
GEMINI_API_KEY = get_secret("GEMINI_API_KEY") or get_secret("GOOGLE_API_KEY")
SPREADSHEET_KEY = get_secret("SPREADSHEET_KEY")

# google-genai warns on every client init when both names are set. They hold the
# same key here, so drop the redundant one to keep the logs readable.
if os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
    if os.environ["GOOGLE_API_KEY"] == os.environ["GEMINI_API_KEY"]:
        del os.environ["GEMINI_API_KEY"]


# --- Model -----------------------------------------------------------------

# "nvidia" (default), "gemini", or "openai". nvidia and openai go through
# LiteLLM. NVIDIA NIM is the default because it is free to run, which is what
# makes a public deployment viable.
MODEL_PROVIDER = get_secret("MODEL_PROVIDER", "nvidia").strip().lower()

OPENAI_API_KEY = get_secret("OPENAI_API_KEY")

NVIDIA_API_KEY = get_secret("NVIDIA_API_KEY")
NVIDIA_BASE_URL = get_secret("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# The PRD specifies gemini-2.5-flash, which Google has since retired for new API
# keys (404 NOT_FOUND).
#
# gemini-3.6-flash is the closest current equivalent, but the free tier allows
# only 5 requests/minute for it — and one user turn costs 2-3 model calls, so
# the app throttles almost immediately. The flash-lite tier allows 15/minute,
# which is what makes free-tier use practical. Override with MODEL_NAME.
# Chosen by benchmarking the full 6-turn routing probe, not from documentation:
#   nvidia-nemotron-nano-9b-v2  5/6 routing, clean tool calls   <- default
#   gemini-3.1-flash-lite       6/6 routing, but quota-limited
#   openai/gpt-oss-20b          4/6, emits corrupted tool names
#   openai/gpt-oss-120b         works but ~60s per call
#   meta/llama-3.3-70b-instruct timed out (>10 min) on the free tier
DEFAULT_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "openai": "gpt-4o-mini",
    "nvidia": "nvidia/nvidia-nemotron-nano-9b-v2",
}

MODEL_NAME = get_secret("MODEL_NAME") or DEFAULT_MODELS.get(
    MODEL_PROVIDER, DEFAULT_MODELS["gemini"]
)

# Human-readable labels for the owner's provider picker.
PROVIDER_LABELS = {
    "nvidia": "NVIDIA NIM (free)",
    "gemini": "Google Gemini (free tier, rate-limited)",
    "openai": "OpenAI (billed)",
}


def provider_key(provider: str) -> str:
    """The API key configured for a provider, or empty if there is none."""
    return {
        "nvidia": NVIDIA_API_KEY,
        "openai": OPENAI_API_KEY,
        "gemini": GEMINI_API_KEY,
    }.get(provider, "")


def available_providers() -> list[str]:
    """Providers that have a key configured, free one first.

    Only these are offered in the picker — listing a provider whose key is
    absent would just produce a runtime error when selected.
    """
    return [p for p in ("nvidia", "gemini", "openai") if provider_key(p)]


def resolve_session_provider(trusted: bool, chosen: str = "") -> str:
    """The model provider for one session.

    ``trusted`` is the same flag that governs write access: either the
    deployment enabled it (local development) or the visitor signed in as the
    owner. Untrusted sessions are pinned to the free provider, so a visitor can
    never cause a request to a billed one whatever the secrets contain.

    Lives here rather than in the UI so it is testable without Streamlit.
    """
    if not trusted:
        return FREE_PROVIDER
    if chosen and chosen in available_providers():
        return chosen
    return MODEL_PROVIDER


def default_model_for(provider: str) -> str:
    """The model to use for a provider when nothing specific was requested."""
    if provider == MODEL_PROVIDER and MODEL_NAME:
        return MODEL_NAME
    return DEFAULT_MODELS.get(provider, DEFAULT_MODELS["nvidia"])

# Providers that cost money or burn a personal quota. The public demo is locked
# to the free one so a stray secret or typo in the Streamlit dashboard cannot
# start billing the owner.
FREE_PROVIDER = "nvidia"


# --- Write access & usage guard --------------------------------------------


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Fail-safe: writing to the owner's spreadsheet must be turned ON explicitly.
# A public Streamlit deployment has no authentication, so a deployment that
# forgets to configure this must end up read-only, never wide open. This is
# deliberately the inverse of the obvious design.
WRITE_ENABLED = _truthy(get_secret("WRITE_ENABLED", "false"))

# An explicit DEMO_MODE=true always wins, so a deployment can be pinned
# read-only even if WRITE_ENABLED is set somewhere.
if _truthy(get_secret("DEMO_MODE", "false")):
    WRITE_ENABLED = False

# Demo mode is simply the absence of write access.
DEMO_MODE = not WRITE_ENABLED

# The Google account allowed to write when signed in. This is how the deployed
# app grants the owner full access without opening the sheet to everyone:
# WRITE_ENABLED stays off, and permission is earned per-session by signing in.
OWNER_EMAIL = get_secret("OWNER_EMAIL").strip()

# Force the public demo onto the free provider, whatever the secrets say.
# Runs after MODEL_PROVIDER/MODEL_NAME are resolved above.
PROVIDER_WAS_FORCED = False
if DEMO_MODE and MODEL_PROVIDER != FREE_PROVIDER:
    PROVIDER_WAS_FORCED = True
    MODEL_PROVIDER = FREE_PROVIDER
    MODEL_NAME = get_secret("MODEL_NAME") or DEFAULT_MODELS[FREE_PROVIDER]
    if not MODEL_NAME.startswith(("nvidia/", "meta/", "mistralai/", "openai/gpt-oss")):
        # A model name meant for another provider would 404 against NIM.
        MODEL_NAME = DEFAULT_MODELS[FREE_PROVIDER]

# Cap messages per browser session so one visitor cannot drain the API quota.
# 0 disables the cap. Defaults to 10 in demo mode, unlimited locally.
MAX_MESSAGES_PER_SESSION = int(
    get_secret("MAX_MESSAGES_PER_SESSION", "10" if DEMO_MODE else "0") or 0
)

# Either an inline JSON blob (Streamlit Cloud) or a path to the key file (local).
GOOGLE_SERVICE_ACCOUNT_JSON = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")


def service_account_info() -> dict:
    """Parse the service account credential into the dict gspread expects.

    Accepts an inline JSON string or a filesystem path to the downloaded key.
    """
    raw = GOOGLE_SERVICE_ACCOUNT_JSON.strip()
    if not raw:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Provide the service account "
            "JSON inline or a path to the key file."
        )

    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Almost always a TOML quoting mistake: `"""` in secrets.toml is a
            # *basic* string and expands the `\n` escapes inside private_key
            # into real newlines, which are illegal inside a JSON string.
            # `'''` is a literal string and leaves them intact.
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON "
                f"({exc.msg} at line {exc.lineno}). If you pasted it into "
                "Streamlit secrets, wrap it in ''' triple-single-quotes, not "
                '""" triple-double-quotes — the latter mangles the \\n escapes '
                "in private_key."
            ) from exc

    key_path = Path(raw)
    if not key_path.is_absolute():
        key_path = PROJECT_ROOT / key_path
    if not key_path.exists():
        raise FileNotFoundError(f"Service account key file not found: {key_path}")
    return json.loads(key_path.read_text(encoding="utf-8"))


# --- TMDB ------------------------------------------------------------------

# Several Indian ISPs reset TLS connections to api.themoviedb.org. api.tmdb.org
# is TMDB's own alias for the same API and is not blocked, so it is kept as a
# fallback and tried automatically when the primary host cannot be reached.
TMDB_HOSTS: list[str] = [
    "https://api.themoviedb.org/3",
    "https://api.tmdb.org/3",
]

# Set TMDB_BASE_URL in .env to pin one host and skip the failover probe.
TMDB_BASE_URL = get_secret("TMDB_BASE_URL")
if TMDB_BASE_URL:
    TMDB_HOSTS = [TMDB_BASE_URL.rstrip("/")]

TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"
WATCH_REGION = "IN"
DEFAULT_LANGUAGE = "en-US"
REQUEST_TIMEOUT_SECONDS = 15

# TMDB watch-provider IDs for the Indian region, verified against the live list
# on 2026-07-29 with `uv run python scripts/verify_providers.py`.
#
# These differ from the PRD's table, which predates a 2025 consolidation:
# Disney+ Hotstar (122) and JioCinema (220) merged into JioHotstar (2336) and
# neither old ID returns results in India any more. The retired names are kept
# as aliases so a user asking for "Hotstar" still gets the right catalogue.
OTT_PROVIDERS: dict[str, int] = {
    "netflix": 8,
    "amazon prime video": 119,
    "prime video": 119,
    "prime": 119,
    "amazon": 119,
    "jiohotstar": 2336,
    "jio hotstar": 2336,
    "hotstar": 2336,
    "disney+ hotstar": 2336,
    "disney plus hotstar": 2336,
    "disney+": 2336,
    "jiocinema": 2336,
    "jio cinema": 2336,
    "jio": 2336,
    "zee5": 232,
    "zee": 232,
    "sonyliv": 237,
    "sony liv": 237,
    "apple tv": 350,
    "apple tv+": 350,
    "apple tv plus": 350,
    "aha": 532,
    "sun nxt": 309,
    "sunnxt": 309,
    "manoramamax": 482,
    "manorama max": 482,
    "mx player": 515,
    "mxplayer": 515,
}

# Canonical display names, keyed by provider ID.
PROVIDER_NAMES: dict[int, str] = {
    8: "Netflix",
    119: "Amazon Prime Video",
    2336: "JioHotstar",
    232: "Zee5",
    237: "SonyLIV",
    350: "Apple TV",
    309: "Sun NXT",
    532: "aha",
    482: "ManoramaMax",
    515: "MX Player",
}

# TMDB movie genre IDs. Static because /genre/movie/list rarely changes; the
# genre resolver still falls back to the live endpoint for anything unmapped.
MOVIE_GENRES: dict[str, int] = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "musical": 10402,
    "mystery": 9648,
    "romance": 10749,
    "science fiction": 878,
    "sci-fi": 878,
    "scifi": 878,
    "tv movie": 10770,
    "thriller": 53,
    "war": 10752,
    "western": 37,
}

# Television uses a different genre list from film. Verified against
# /genre/tv/list: no name is shared with a different ID, so the two tables can
# never disagree — but several film genres have no television equivalent at all.
#
# TMDB collapses Action and Adventure into one television genre, and Science
# Fiction and Fantasy into another, so those aliases point at the combined ID.
TV_GENRES: dict[str, int] = {
    "action & adventure": 10759,
    "action and adventure": 10759,
    "action": 10759,
    "adventure": 10759,
    "animation": 16,
    "anime": 16,
    "comedy": 35,
    "sitcom": 35,
    "crime": 80,
    "documentary": 99,
    "docuseries": 99,
    "drama": 18,
    "family": 10751,
    "kids": 10762,
    "children": 10762,
    "mystery": 9648,
    "news": 10763,
    "reality": 10764,
    "reality tv": 10764,
    "sci-fi & fantasy": 10765,
    "sci-fi and fantasy": 10765,
    "sci-fi": 10765,
    "scifi": 10765,
    "science fiction": 10765,
    "fantasy": 10765,
    "soap": 10766,
    "talk": 10767,
    "talk show": 10767,
    "war & politics": 10768,
    "war and politics": 10768,
    "war": 10768,
    "politics": 10768,
    "western": 37,
}

TV_GENRE_NAMES: dict[int, str] = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}

# Film genres with no television counterpart on TMDB. Asking for a "thriller
# series" has to be answered honestly rather than silently mapped to something
# adjacent.
FILM_ONLY_GENRES = {"thriller", "horror", "romance", "history", "music", "musical"}

GENRE_NAMES: dict[int, str] = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
}

# ISO 639-1 codes for the languages this agent is most often asked about.
LANGUAGE_CODES: dict[str, str] = {
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "malayalam": "ml",
    "kannada": "kn",
    "bengali": "bn",
    "marathi": "mr",
    "punjabi": "pa",
    "gujarati": "gu",
    "english": "en",
    "korean": "ko",
    "japanese": "ja",
    "spanish": "es",
    "french": "fr",
}


# --- Google Sheets ---------------------------------------------------------

WORKSHEET_NAME = "Movie_Journal"

JOURNAL_HEADERS: list[str] = [
    "Log_ID",
    "Watch_Date",
    "Movie_Title",
    "TMDB_ID",
    "OTT_Platform",
    "Genre",
    "User_Rating",
    "User_Review",
    "Shared_Status",
    # Added in v1.2. Ratings keep their native scales: TMDB and IMDb are out of
    # 10, Rotten Tomatoes and Metacritic out of 100. Any of them can be blank —
    # measured coverage on Indian films is IMDb 100%, RT 83%, Metacritic 33%.
    "IMDb_ID",
    "TMDB_Rating",
    "IMDb_Rating",
    "RT_Rating",
    "Metacritic",
    "Synopsis",
]

# "Want to watch" lives in its own worksheet rather than as a flag on the
# journal: the two have different lifecycles, and mixing them makes both
# queries messy. Created automatically on first use.
WATCHLIST_WORKSHEET_NAME = "Watchlist"

WATCHLIST_HEADERS: list[str] = [
    "Watchlist_ID",
    "Added_Date",
    "Movie_Title",
    "TMDB_ID",
    "OTT_Platform",
    "Genre",
    "Notes",
    # Same six as the journal, minus User_Rating — you have not watched it yet,
    # so the critics' scores are exactly what helps you choose.
    "IMDb_ID",
    "TMDB_Rating",
    "IMDb_Rating",
    "RT_Rating",
    "Metacritic",
    "Synopsis",
]

GOOGLE_API_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
