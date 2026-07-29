"""TMDB-backed tools for the Discovery and Critic agents.

Every public function returns a JSON-serialisable dict with a ``status`` key of
``"success"`` or ``"error"`` — the shape Google ADK expects from a function
tool, and one the model can reason about without raising.
"""

from __future__ import annotations

import functools
from typing import Any

import requests

from movie_connoisseur import config

# --- Low-level HTTP --------------------------------------------------------


class TMDBError(RuntimeError):
    """Raised when TMDB returns an error or is unreachable."""


# The host that last answered, so the failover probe runs at most once.
_active_host: str | None = None


def _host_order() -> list[str]:
    """Hosts to try, most recently working one first."""
    if _active_host:
        return [_active_host] + [h for h in config.TMDB_HOSTS if h != _active_host]
    return list(config.TMDB_HOSTS)


def active_host() -> str:
    """The TMDB host currently in use (empty until the first successful call)."""
    return _active_host or ""


def _get(path: str, **params: Any) -> dict:
    """Call a TMDB endpoint and return the decoded JSON body.

    Tries each configured host in turn so an ISP-level block on one domain
    fails over to TMDB's alias instead of breaking the tool.
    """
    global _active_host

    if not config.TMDB_API_KEY:
        raise TMDBError("TMDB_API_KEY is not set.")

    params = {k: v for k, v in params.items() if v not in (None, "", [])}
    params.setdefault("language", config.DEFAULT_LANGUAGE)
    params["api_key"] = config.TMDB_API_KEY

    unreachable = []
    for host in _host_order():
        try:
            response = requests.get(
                f"{host}{path}",
                params=params,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Network-level failure: this host may be blocked, so try the next.
            unreachable.append(f"{host} ({type(exc).__name__})")
            continue

        # The host answered, so remember it even if the response is an error —
        # a 401 is a key problem, not a connectivity one, and retrying the
        # other host would only repeat it.
        _active_host = host

        if response.status_code == 401:
            raise TMDBError("TMDB rejected the API key (401). Check TMDB_API_KEY.")
        if response.status_code == 404:
            raise TMDBError("No such resource on TMDB (404).")
        if response.status_code == 429:
            raise TMDBError("TMDB rate limit hit (429). Wait a moment and retry.")
        if not response.ok:
            raise TMDBError(f"TMDB returned HTTP {response.status_code}: {response.text[:200]}")

        return response.json()

    raise TMDBError("Could not reach TMDB. Tried: " + "; ".join(unreachable))


# --- Resolvers -------------------------------------------------------------


def resolve_provider(provider: str) -> int | None:
    """Map an OTT platform name or numeric ID to a TMDB provider ID."""
    text = str(provider).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in config.OTT_PROVIDERS:
        return config.OTT_PROVIDERS[text]
    # Tolerate "netflix india", "watch on zee5" and similar phrasings.
    for name, pid in config.OTT_PROVIDERS.items():
        if name in text:
            return pid
    return None


@functools.lru_cache(maxsize=1)
def _live_genre_map() -> dict[str, int]:
    """Fetch the current genre list from TMDB, cached for the process lifetime."""
    payload = _get("/genre/movie/list")
    return {g["name"].strip().lower(): int(g["id"]) for g in payload.get("genres", [])}


def resolve_genre(genre: str) -> int | None:
    """Map a genre name or numeric ID to a TMDB genre ID.

    Accepts the PRD's ``genre_id`` form as well as the natural-language
    ``genre="Thriller"`` form used in the sample dialogue.
    """
    text = str(genre).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in config.MOVIE_GENRES:
        return config.MOVIE_GENRES[text]
    try:
        return _live_genre_map().get(text)
    except TMDBError:
        return None


def resolve_language(language: str) -> str:
    """Map a language name to its ISO 639-1 code (pass-through for codes)."""
    text = str(language).strip().lower()
    if not text:
        return ""
    if len(text) == 2:
        return text
    return config.LANGUAGE_CODES.get(text, "")


def format_runtime(minutes: int) -> str:
    """Render a runtime in minutes as ``2h 21m``."""
    if not minutes:
        return "Unknown"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _poster_url(path: str | None) -> str:
    return f"{config.TMDB_IMAGE_BASE_URL}{path}" if path else ""


def _summarise(movie: dict) -> dict:
    """Condense a TMDB list entry into the fields the agent presents."""
    return {
        "tmdb_id": movie.get("id"),
        "title": movie.get("title") or movie.get("original_title", ""),
        "release_date": movie.get("release_date", ""),
        "release_year": (movie.get("release_date") or "")[:4],
        "rating": round(float(movie.get("vote_average") or 0), 1),
        "vote_count": movie.get("vote_count", 0),
        "language": movie.get("original_language", ""),
        "genres": [
            config.GENRE_NAMES.get(gid, str(gid)) for gid in movie.get("genre_ids", [])
        ],
        "overview": movie.get("overview", ""),
        "poster_url": _poster_url(movie.get("poster_path")),
    }


# --- Discovery Agent tools -------------------------------------------------


def fetch_ott_movies(
    provider: str = "",
    genre: str = "",
    release_year: int = 0,
    language: str = "",
    min_rating: float = 0.0,
    limit: int = 5,
) -> dict:
    """Find movies streaming on an Indian OTT platform, filtered by genre, year or language.

    Use this when the user asks what is available to watch on a platform, or
    wants recommendations narrowed by genre, release year or language.

    Args:
        provider: OTT platform name or TMDB provider ID, e.g. "Netflix",
            "Disney+ Hotstar", "Zee5" or "8". Leave empty to search all
            platforms available in India.
        genre: Genre name or TMDB genre ID, e.g. "Thriller", "Comedy" or "53".
            Leave empty for no genre filter.
        release_year: Four-digit release year, e.g. 2024. Use 0 for any year.
        language: Original language name or ISO code, e.g. "Tamil" or "ta".
            Leave empty for any language.
        min_rating: Minimum TMDB community rating out of 10, e.g. 7.0. Use 0.0
            for no minimum.
        limit: How many movies to return. Defaults to 5, capped at 20.

    Returns:
        dict with ``status`` and, on success, a ``movies`` list where each entry
        carries the title, TMDB ID, release year, rating, genres and overview.
    """
    provider_id = resolve_provider(provider) if provider else None
    if provider and provider_id is None:
        return {
            "status": "error",
            "error_message": (
                f"Unknown OTT platform '{provider}'. Supported platforms: "
                + ", ".join(sorted(config.PROVIDER_NAMES.values()))
            ),
        }

    genre_id = resolve_genre(genre) if genre else None
    if genre and genre_id is None:
        return {
            "status": "error",
            "error_message": (
                f"Unknown genre '{genre}'. Supported genres: "
                + ", ".join(sorted(config.GENRE_NAMES.values()))
            ),
        }

    language_code = resolve_language(language) if language else ""
    if language and not language_code:
        return {
            "status": "error",
            "error_message": (
                f"Unknown language '{language}'. Try Hindi, Tamil, Telugu, "
                "Malayalam, Kannada, Bengali, Marathi or English."
            ),
        }

    limit = max(1, min(int(limit or 5), 20))

    try:
        payload = _get(
            "/discover/movie",
            with_watch_providers=provider_id,
            watch_region=config.WATCH_REGION,
            with_genres=genre_id,
            primary_release_year=release_year or None,
            with_original_language=language_code or None,
            **{"vote_average.gte": min_rating or None},
            # Require a minimum vote count so obscure titles with a lone 10/10
            # do not crowd out genuinely popular results.
            **{"vote_count.gte": 50},
            sort_by="popularity.desc",
            include_adult="false",
        )
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    movies = [_summarise(m) for m in payload.get("results", [])[:limit]]

    if not movies:
        return {
            "status": "success",
            "movies": [],
            "total_results": 0,
            "message": (
                "No movies matched those filters on that platform in India. "
                "Try relaxing the year, genre or rating filter."
            ),
        }

    return {
        "status": "success",
        "provider": config.PROVIDER_NAMES.get(provider_id, provider) if provider_id else "All platforms (India)",
        "movies": movies,
        "total_results": payload.get("total_results", len(movies)),
    }


def search_movies(query: str, limit: int = 5) -> dict:
    """Search TMDB for movies by title.

    Use this to resolve a title the user mentioned into a TMDB ID before
    fetching details or logging it to the journal.

    Args:
        query: The movie title to search for, e.g. "Stree 2".
        limit: How many matches to return. Defaults to 5, capped at 20.

    Returns:
        dict with ``status`` and, on success, a ``movies`` list ordered by
        popularity, each with a ``tmdb_id`` usable by the other tools.
    """
    if not str(query).strip():
        return {"status": "error", "error_message": "A movie title is required."}

    limit = max(1, min(int(limit or 5), 20))

    try:
        payload = _get("/search/movie", query=query, include_adult="false")
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    results = payload.get("results", [])
    if not results:
        return {
            "status": "success",
            "movies": [],
            "message": f"No movie on TMDB matched '{query}'.",
        }

    return {
        "status": "success",
        "query": query,
        "movies": [_summarise(m) for m in results[:limit]],
    }


# --- Critic & Detail Agent tools -------------------------------------------


def _certification(release_dates: dict) -> str:
    """Pull the Indian age certification out of an appended release_dates block."""
    for entry in release_dates.get("results", []):
        if entry.get("iso_3166_1") == config.WATCH_REGION:
            for release in entry.get("release_dates", []):
                if release.get("certification"):
                    return release["certification"]
    # Fall back to the US rating when India has not been certified on TMDB.
    for entry in release_dates.get("results", []):
        if entry.get("iso_3166_1") == "US":
            for release in entry.get("release_dates", []):
                if release.get("certification"):
                    return f"{release['certification']} (US)"
    return "Not rated"


def _india_streaming(providers: dict) -> list[str]:
    """List the Indian platforms streaming a title (subscription + free tiers)."""
    india = providers.get("results", {}).get(config.WATCH_REGION, {})
    names = []
    for tier in ("flatrate", "free", "ads"):
        for provider in india.get(tier, []):
            name = provider.get("provider_name")
            if name and name not in names:
                names.append(name)
    return names


def fetch_movie_details(title_or_id: str) -> dict:
    """Get full details for one movie: plot, director, cast, runtime, rating and where to stream it in India.

    Accepts either a TMDB ID or a title — a title is resolved to the most
    popular match automatically.

    Args:
        title_or_id: A movie title such as "Maharaja", or a TMDB ID such as
            "109123".

    Returns:
        dict with ``status`` and, on success, the movie's plot, director,
        top-billed cast, formatted runtime, age certification, community
        rating and Indian streaming platforms.
    """
    text = str(title_or_id).strip()
    if not text:
        return {"status": "error", "error_message": "A movie title or TMDB ID is required."}

    if text.isdigit():
        movie_id = int(text)
    else:
        found = search_movies(text, limit=1)
        if found["status"] == "error":
            return found
        if not found["movies"]:
            return {
                "status": "error",
                "error_message": f"No movie on TMDB matched '{text}'.",
            }
        movie_id = found["movies"][0]["tmdb_id"]

    try:
        movie = _get(
            f"/movie/{movie_id}",
            append_to_response="credits,release_dates,watch/providers",
        )
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    credits = movie.get("credits", {})
    directors = [
        person["name"]
        for person in credits.get("crew", [])
        if person.get("job") == "Director"
    ]
    cast = [person["name"] for person in credits.get("cast", [])[:5]]

    return {
        "status": "success",
        "tmdb_id": movie.get("id", movie_id),
        "title": movie.get("title", ""),
        "original_title": movie.get("original_title", ""),
        "tagline": movie.get("tagline", ""),
        "overview": movie.get("overview", ""),
        "release_date": movie.get("release_date", ""),
        "runtime_minutes": movie.get("runtime") or 0,
        "runtime": format_runtime(movie.get("runtime") or 0),
        "genres": [g["name"] for g in movie.get("genres", [])],
        "director": ", ".join(directors) or "Unknown",
        "cast": cast,
        "language": movie.get("original_language", ""),
        "rating": round(float(movie.get("vote_average") or 0), 1),
        "vote_count": movie.get("vote_count", 0),
        "certification": _certification(movie.get("release_dates", {})),
        "streaming_in_india": _india_streaming(movie.get("watch/providers", {})),
        "poster_url": _poster_url(movie.get("poster_path")),
    }


def fetch_movie_credits(movie_id: int, cast_limit: int = 10) -> dict:
    """Get the full cast and key crew for a movie by TMDB ID.

    Use this when the user asks specifically about who acted in or made a film,
    beyond the top-billed names in fetch_movie_details.

    Args:
        movie_id: The TMDB movie ID, e.g. 109123.
        cast_limit: How many cast members to return. Defaults to 10, capped at 30.

    Returns:
        dict with ``status`` and, on success, ``cast`` (name and character) plus
        the director, writers, composer and cinematographer.
    """
    if not movie_id:
        return {"status": "error", "error_message": "A TMDB movie ID is required."}

    cast_limit = max(1, min(int(cast_limit or 10), 30))

    try:
        payload = _get(f"/movie/{movie_id}/credits")
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    crew = payload.get("crew", [])

    def by_job(*jobs: str) -> list[str]:
        return [p["name"] for p in crew if p.get("job") in jobs]

    return {
        "status": "success",
        "tmdb_id": movie_id,
        "cast": [
            {"name": p.get("name", ""), "character": p.get("character", "")}
            for p in payload.get("cast", [])[:cast_limit]
        ],
        "director": ", ".join(by_job("Director")) or "Unknown",
        "writers": by_job("Writer", "Screenplay", "Story"),
        "music": by_job("Original Music Composer", "Music"),
        "cinematography": by_job("Director of Photography"),
    }


def list_ott_providers() -> dict:
    """List the OTT platforms this agent can search in India, with their TMDB IDs.

    Use this when the user asks which platforms are supported.

    Returns:
        dict with ``status`` and a ``providers`` list of name/id pairs.
    """
    return {
        "status": "success",
        "region": config.WATCH_REGION,
        "providers": [
            {"name": name, "provider_id": pid}
            for pid, name in sorted(config.PROVIDER_NAMES.items(), key=lambda kv: kv[1])
        ],
    }
