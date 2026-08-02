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


def canonical_platform(name: str) -> str:
    """Return the one agreed spelling for an OTT platform.

    "Prime Video" and "Amazon Prime Video" are the same service, but stored
    verbatim they become two rows in the platform breakdown. Resolving through
    the provider table collapses the aliases. Anything unrecognised is returned
    unchanged rather than dropped — a platform we do not know about is still
    the user's answer.
    """
    text = str(name).strip()
    if not text:
        return ""
    provider_id = resolve_provider(text)
    if provider_id is None:
        return text
    return config.PROVIDER_NAMES.get(provider_id, text)


def normalise_media_type(media_type: str) -> str:
    """Map what a user or model might say to TMDB's 'movie' or 'tv'."""
    text = str(media_type).strip().lower()
    if text in {"tv", "series", "show", "tv show", "television", "serial"}:
        return "tv"
    if text in {"movie", "film", "movies", "films"}:
        return "movie"
    return ""


@functools.lru_cache(maxsize=2)
def _live_genre_map(media_type: str = "movie") -> dict[str, int]:
    """Fetch the current genre list from TMDB, cached for the process lifetime."""
    payload = _get(f"/genre/{media_type}/list")
    return {g["name"].strip().lower(): int(g["id"]) for g in payload.get("genres", [])}


def resolve_genre(genre: str, media_type: str = "movie") -> int | None:
    """Map a genre name or numeric ID to a TMDB genre ID.

    Film and television have separate genre lists. Television collapses Action
    and Adventure into one genre, and Science Fiction and Fantasy into another,
    so the same word resolves to a different ID depending on media type — and
    Thriller, Horror and Romance do not exist for television at all.

    Accepts the PRD's ``genre_id`` form as well as the natural-language
    ``genre="Thriller"`` form used in the sample dialogue.
    """
    text = str(genre).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    table = config.TV_GENRES if media_type == "tv" else config.MOVIE_GENRES
    if text in table:
        return table[text]

    try:
        return _live_genre_map(media_type).get(text)
    except TMDBError:
        return None


def genre_name(genre_id: int, media_type: str = "movie") -> str:
    names = config.TV_GENRE_NAMES if media_type == "tv" else config.GENRE_NAMES
    return names.get(genre_id, str(genre_id))


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


def _summarise(item: dict, media_type: str = "movie") -> dict:
    """Condense a TMDB list entry into the fields the agent presents.

    Film and television disagree on field names — ``title`` versus ``name``,
    ``release_date`` versus ``first_air_date`` — so both are normalised here
    into one shape rather than leaking the difference to every caller.
    """
    media_type = item.get("media_type") or media_type
    date = item.get("release_date") or item.get("first_air_date") or ""

    return {
        "tmdb_id": item.get("id"),
        "media_type": media_type,
        "title": (
            item.get("title")
            or item.get("name")
            or item.get("original_title")
            or item.get("original_name", "")
        ),
        "release_date": date,
        "release_year": date[:4],
        "rating": round(float(item.get("vote_average") or 0), 1),
        "vote_count": item.get("vote_count", 0),
        "language": item.get("original_language", ""),
        "genres": [
            genre_name(gid, media_type) for gid in item.get("genre_ids", [])
        ],
        "overview": item.get("overview", ""),
        "poster_url": _poster_url(item.get("poster_path")),
    }


# --- Discovery Agent tools -------------------------------------------------


def fetch_ott_titles(
    provider: str = "",
    genre: str = "",
    media_type: str = "movie",
    release_year: int = 0,
    language: str = "",
    min_rating: float = 0.0,
    limit: int = 5,
) -> dict:
    """Find movies or TV series streaming on an Indian OTT platform, filtered by genre, year or language.

    Use this when the user asks what is available to watch on a platform, or
    wants recommendations narrowed by genre, release year or language.

    Args:
        provider: OTT platform name or TMDB provider ID, e.g. "Netflix",
            "JioHotstar", "Zee5" or "8". Leave empty to search all platforms
            available in India.
        genre: Genre name or TMDB genre ID, e.g. "Thriller", "Comedy" or "53".
            Leave empty for no genre filter. Note that television has its own
            genre list: Thriller, Horror and Romance exist only for films.
        media_type: "movie" (default) or "tv" for series. Set it to "tv"
            whenever the user asks about shows, series or television.
        release_year: Four-digit release or first-air year, e.g. 2024. Use 0
            for any year.
        language: Original language name or ISO code, e.g. "Tamil" or "ta".
            Leave empty for any language.
        min_rating: Minimum TMDB community rating out of 10, e.g. 7.0. Use 0.0
            for no minimum.
        limit: How many titles to return. Defaults to 5, capped at 20.

    Returns:
        dict with ``status`` and, on success, a ``results`` list where each
        entry carries the title, TMDB ID, media type, release year, rating,
        genres and overview.
    """
    kind = normalise_media_type(media_type) or "movie"
    provider_id = resolve_provider(provider) if provider else None
    if provider and provider_id is None:
        return {
            "status": "error",
            "error_message": (
                f"Unknown OTT platform '{provider}'. Supported platforms: "
                + ", ".join(sorted(config.PROVIDER_NAMES.values()))
            ),
        }

    genre_id = resolve_genre(genre, kind) if genre else None
    if genre and genre_id is None:
        names = config.TV_GENRE_NAMES if kind == "tv" else config.GENRE_NAMES
        # Be specific when the genre exists but only for films — silently
        # substituting something adjacent would misrepresent the results.
        if kind == "tv" and str(genre).strip().lower() in config.FILM_ONLY_GENRES:
            hint = (
                f"TMDB has no '{genre}' genre for television — it exists only "
                "for films. Closest television genres: "
            )
        else:
            hint = f"Unknown genre '{genre}' for {kind}. Supported: "
        return {"status": "error", "error_message": hint + ", ".join(sorted(names.values()))}

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

    # The year parameter is named differently per catalogue.
    year_param = (
        {"first_air_date_year": release_year or None}
        if kind == "tv"
        else {"primary_release_year": release_year or None}
    )

    try:
        payload = _get(
            f"/discover/{kind}",
            with_watch_providers=provider_id,
            watch_region=config.WATCH_REGION,
            with_genres=genre_id,
            with_original_language=language_code or None,
            **year_param,
            **{"vote_average.gte": min_rating or None},
            # Require a minimum vote count so obscure titles with a lone 10/10
            # do not crowd out genuinely popular results. Television has a
            # smaller voting population, so the bar is lower.
            **{"vote_count.gte": 10 if kind == "tv" else 50},
            sort_by="popularity.desc",
            include_adult="false",
        )
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    results = [_summarise(m, kind) for m in payload.get("results", [])[:limit]]

    if not results:
        noun = "series" if kind == "tv" else "movies"
        return {
            "status": "success",
            "media_type": kind,
            "results": [],
            "total_results": 0,
            "message": (
                f"No {noun} matched those filters on that platform in India. "
                "Try relaxing the year, genre or rating filter."
            ),
        }

    return {
        "status": "success",
        "media_type": kind,
        "provider": config.PROVIDER_NAMES.get(provider_id, provider)
        if provider_id
        else "All platforms (India)",
        "results": results,
        "total_results": payload.get("total_results", len(results)),
    }


def search_titles(query: str, media_type: str = "", limit: int = 5) -> dict:
    """Search TMDB for a movie or TV series by title.

    Use this to resolve a title the user mentioned into a TMDB ID before
    fetching details or saving it. Searches both films and television unless
    media_type narrows it.

    Args:
        query: The title to search for, e.g. "Stree 2" or "Ted Lasso".
        media_type: "movie" or "tv" to restrict the search. Leave empty to
            search both, which is usually what you want.
        limit: How many matches to return. Defaults to 5, capped at 20.

    Returns:
        dict with ``status`` and, on success, a ``results`` list ordered by
        popularity. Each entry carries a ``tmdb_id`` and a ``media_type`` of
        "movie" or "tv", both of which the other tools need.
    """
    if not str(query).strip():
        return {"status": "error", "error_message": "A title is required."}

    limit = max(1, min(int(limit or 5), 20))
    wanted = normalise_media_type(media_type)

    # /search/multi returns films, television and people in one call, tagged
    # with media_type — so "Ted Lasso" resolves without the caller having to
    # know in advance that it is a series.
    endpoint = f"/search/{wanted}" if wanted else "/search/multi"

    try:
        payload = _get(endpoint, query=query, include_adult="false")
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    results = []
    for item in payload.get("results", []):
        kind = item.get("media_type") or wanted or "movie"
        if kind not in {"movie", "tv"}:
            continue  # /search/multi also returns people
        results.append(_summarise(item, kind))
        if len(results) >= limit:
            break

    if not results:
        return {
            "status": "success",
            "results": [],
            "message": f"Nothing on TMDB matched '{query}'.",
        }

    return {"status": "success", "query": query, "results": results}


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


def _tv_certification(ratings: dict) -> str:
    """Indian age rating for a series, from the appended content_ratings block."""
    for entry in ratings.get("results", []):
        if entry.get("iso_3166_1") == config.WATCH_REGION and entry.get("rating"):
            return entry["rating"]
    for entry in ratings.get("results", []):
        if entry.get("iso_3166_1") == "US" and entry.get("rating"):
            return f"{entry['rating']} (US)"
    return "Not rated"


def fetch_title_details(title_or_id: str, media_type: str = "") -> dict:
    """Get full details for one movie or TV series: plot, cast, director, runtime, rating and where to stream it in India.

    Accepts a title or a TMDB ID, and handles both films and television. A
    title is resolved to the most popular match automatically, so "Ted Lasso"
    works without saying it is a series.

    Args:
        title_or_id: A title such as "Maharaja" or "Ted Lasso", or a TMDB ID
            such as "109123".
        media_type: "movie" or "tv". Only needed to disambiguate when a film
            and a series share a title, or when passing a numeric ID — an ID
            alone is meaningless without knowing which catalogue it belongs to.

    Returns:
        dict with ``status`` and, on success, the plot, creator or director,
        top-billed cast, formatted runtime, age certification, community
        rating and Indian streaming platforms. For a series it also carries
        ``seasons`` and ``episodes``.
    """
    text = str(title_or_id).strip()
    if not text:
        return {"status": "error", "error_message": "A title or TMDB ID is required."}

    kind = normalise_media_type(media_type)

    if text.isdigit():
        # A bare ID says nothing about which catalogue it came from, so assume
        # film unless told otherwise.
        tmdb_id = int(text)
        kind = kind or "movie"
    else:
        found = search_titles(text, media_type=kind, limit=1)
        if found["status"] == "error":
            return found
        if not found.get("results"):
            return {
                "status": "error",
                "error_message": f"Nothing on TMDB matched '{text}'.",
            }
        tmdb_id = found["results"][0]["tmdb_id"]
        kind = found["results"][0]["media_type"]

    extras = (
        "credits,content_ratings,watch/providers,external_ids"
        if kind == "tv"
        else "credits,release_dates,watch/providers"
    )

    try:
        item = _get(f"/{kind}/{tmdb_id}", append_to_response=extras)
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    credits = item.get("credits", {})
    cast = [person["name"] for person in credits.get("cast", [])[:5]]

    if kind == "tv":
        # Series have creators rather than a director, and a per-episode
        # runtime rather than one total.
        makers = [c["name"] for c in item.get("created_by", [])] or [
            p["name"] for p in credits.get("crew", []) if p.get("job") == "Director"
        ]
        runtimes = item.get("episode_run_time") or []
        minutes = runtimes[0] if runtimes else 0
        # imdb_id lives under external_ids for television, not at the top level.
        imdb_id = (item.get("external_ids") or {}).get("imdb_id") or ""
        certification = _tv_certification(item.get("content_ratings", {}))
    else:
        makers = [
            p["name"] for p in credits.get("crew", []) if p.get("job") == "Director"
        ]
        minutes = item.get("runtime") or 0
        imdb_id = item.get("imdb_id") or ""
        certification = _certification(item.get("release_dates", {}))

    summary = _summarise(item, kind)

    return {
        "status": "success",
        "tmdb_id": item.get("id", tmdb_id),
        "media_type": kind,
        "imdb_id": imdb_id,
        "title": summary["title"],
        "original_title": item.get("original_title") or item.get("original_name", ""),
        "tagline": item.get("tagline", ""),
        "overview": item.get("overview", ""),
        "release_date": summary["release_date"],
        "runtime_minutes": minutes,
        "runtime": format_runtime(minutes)
        + (" per episode" if kind == "tv" and minutes else ""),
        "seasons": item.get("number_of_seasons", 0) if kind == "tv" else 0,
        "episodes": item.get("number_of_episodes", 0) if kind == "tv" else 0,
        "genres": [g["name"] for g in item.get("genres", [])],
        "director": ", ".join(makers) or "Unknown",
        "cast": cast,
        "language": item.get("original_language", ""),
        "rating": round(float(item.get("vote_average") or 0), 1),
        "vote_count": item.get("vote_count", 0),
        "certification": certification,
        "streaming_in_india": _india_streaming(item.get("watch/providers", {})),
        "poster_url": _poster_url(item.get("poster_path")),
    }


def fetch_credits(tmdb_id: int, media_type: str = "movie", cast_limit: int = 10) -> dict:
    """Get the full cast and key crew for a movie or series by TMDB ID.

    Use this when the user asks specifically about who acted in or made
    something, beyond the top-billed names in fetch_title_details.

    Args:
        tmdb_id: The TMDB ID, e.g. 109123.
        media_type: "movie" (default) or "tv". A TMDB ID means different things
            in each catalogue, so this must match where the ID came from.
        cast_limit: How many cast members to return. Defaults to 10, capped at 30.

    Returns:
        dict with ``status`` and, on success, ``cast`` (name and character) plus
        the director or creator, writers, composer and cinematographer.
    """
    if not tmdb_id:
        return {"status": "error", "error_message": "A TMDB ID is required."}

    kind = normalise_media_type(media_type) or "movie"
    cast_limit = max(1, min(int(cast_limit or 10), 30))

    try:
        payload = _get(f"/{kind}/{tmdb_id}/credits")
    except TMDBError as exc:
        return {"status": "error", "error_message": str(exc)}

    crew = payload.get("crew", [])

    def by_job(*jobs: str) -> list[str]:
        return [p["name"] for p in crew if p.get("job") in jobs]

    return {
        "status": "success",
        "tmdb_id": tmdb_id,
        "media_type": kind,
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
