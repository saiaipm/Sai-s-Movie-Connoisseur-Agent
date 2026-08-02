"""OMDb-backed critic ratings.

TMDB carries only its own community score. OMDb supplies the IMDb, Rotten
Tomatoes and Metacritic numbers, keyed by the IMDb ID that TMDB already hands
us — so no extra lookup is needed to bridge the two.

Measured coverage on this project's titles (see
``scripts/probe_omdb_coverage.py``): IMDb 100%, Rotten Tomatoes 81%,
Metacritic 50% — and on Indian films specifically, 100% / 83% / 33%. Missing
values are normal, not errors.

Like the other tools here, nothing raises: every function returns a dict with
a ``status`` key.
"""

from __future__ import annotations

import functools

import requests

from movie_connoisseur import config

# OMDb writes this literal string for anything it does not hold.
_MISSING = {"", "N/A", "NA", "NONE"}


def _clean(raw: object) -> str:
    """Normalise OMDb's 'N/A' placeholders to an empty string."""
    text = str(raw or "").strip()
    return "" if text.upper() in _MISSING else text


def _to_number(raw: str) -> float | str:
    """Parse '8.8', '87%' or '74' into a number, keeping the native scale.

    Scales are deliberately not unified: IMDb is out of 10 while Rotten
    Tomatoes and Metacritic are out of 100, and flattening them would imply a
    comparability that does not exist. Numbers rather than strings so the
    spreadsheet columns sort.
    """
    text = _clean(raw).rstrip("%").split("/")[0].strip()
    if not text:
        return ""
    try:
        value = float(text)
    except ValueError:
        return ""
    return int(value) if value.is_integer() else value


def _rating_from(payload: dict, source: str) -> float | str:
    for entry in payload.get("Ratings", []) or []:
        if entry.get("Source") == source:
            return _to_number(entry.get("Value", ""))
    return ""


@functools.lru_cache(maxsize=256)
def _fetch(imdb_id: str, title: str) -> dict:
    """Call OMDb by IMDb ID when available, else by title. Cached per process."""
    params = {"apikey": config.OMDB_API_KEY}
    if imdb_id:
        params["i"] = imdb_id
    else:
        params["t"] = title

    response = requests.get(
        config.OMDB_BASE_URL,
        params=params,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 401:
        raise RuntimeError("OMDb rejected the API key (401). Check OMDB_API_KEY.")
    if not response.ok:
        raise RuntimeError(f"OMDb returned HTTP {response.status_code}.")
    return response.json()


def fetch_external_ratings(imdb_id: str = "", title: str = "") -> dict:
    """Get IMDb, Rotten Tomatoes and Metacritic scores for a movie.

    Use this when the user asks how critics rated a film, as opposed to TMDB's
    community score.

    Args:
        imdb_id: The IMDb ID, e.g. "tt1375666". Preferred — it is exact.
        title: The movie title, used only when no IMDb ID is available.

    Returns:
        dict with ``status`` and, on success, ``imdb_rating`` (out of 10),
        ``rt_rating`` and ``metacritic`` (both out of 100), plus the film's
        ``year``, ``language`` and ``country`` for context. Any of the three
        ratings may be an empty string when that source has no score for the
        film, which is common for Indian cinema.
    """
    if not config.OMDB_API_KEY:
        return {
            "status": "error",
            "error_message": "OMDB_API_KEY is not set, so critic ratings are unavailable.",
        }

    if not str(imdb_id).strip() and not str(title).strip():
        return {"status": "error", "error_message": "An IMDb ID or title is required."}

    try:
        payload = _fetch(str(imdb_id).strip(), str(title).strip())
    except requests.RequestException as exc:
        return {"status": "error", "error_message": f"Could not reach OMDb: {exc}"}
    except RuntimeError as exc:
        return {"status": "error", "error_message": str(exc)}

    if payload.get("Response") == "False":
        return {
            "status": "error",
            "error_message": payload.get("Error", "OMDb has no record of that film."),
        }

    return {
        "status": "success",
        "imdb_id": _clean(payload.get("imdbID")),
        "title": _clean(payload.get("Title")),
        # Year, language and country are returned so the model does not have to
        # supply them from memory. Without them it invented both — calling
        # Tumbbad a 2015 Telugu film when it is a 2018 Hindi/Marathi one.
        "year": _clean(payload.get("Year")),
        "language": _clean(payload.get("Language")),
        "country": _clean(payload.get("Country")),
        "imdb_rating": _to_number(payload.get("imdbRating", "")),
        "imdb_votes": _clean(payload.get("imdbVotes")),
        "rt_rating": _rating_from(payload, "Rotten Tomatoes"),
        "metacritic": _to_number(payload.get("Metascore", "")),
        "rated": _clean(payload.get("Rated")),
    }


def ratings_or_blank(imdb_id: str = "", title: str = "") -> dict:
    """Ratings for writing into a sheet row, blank if anything goes wrong.

    Used by the journal and watchlist writes, where a missing rating must never
    block the log itself.
    """
    blank = {"imdb_rating": "", "rt_rating": "", "metacritic": ""}
    try:
        result = fetch_external_ratings(imdb_id=imdb_id, title=title)
    except Exception:  # noqa: BLE001 - enrichment must never break a write
        return blank
    if result.get("status") != "success":
        return blank
    return {key: result.get(key, "") for key in blank}


def reset_cache() -> None:
    """Drop the cached OMDb responses."""
    _fetch.cache_clear()
