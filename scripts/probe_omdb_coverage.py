"""Measure how much of OMDb's rating data actually exists for our titles.

Run this BEFORE adding rating columns to the sheets. Rotten Tomatoes is
US-critic-centric, so an India-focused journal may find that column almost
always empty — better to learn that now than to ship a dead column.

    uv run python scripts/probe_omdb_coverage.py

Reads nothing and writes nothing: TMDB lookups plus one OMDb call per title.
"""

from __future__ import annotations

import sys

import requests

from movie_connoisseur import config
from movie_connoisseur.tools import tmdb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The owner's actual entries, plus a spread of Indian cinema as a control set —
# a hit rate measured only on Hollywood titles would flatter Rotten Tomatoes.
TITLES = [
    # In the journal / watchlist today
    "Iron Man",
    "The Town",
    "Fantastic 4",
    "Inception",
    "Maharaja",
    "Kalki 2898 AD",
    # Indian control set
    "Anniyan",
    "Tumbbad",
    "Stree 2",
    "Drishyam",
    "RRR",
    "Jawan",
    "3 Idiots",
    "Gangs of Wasseypur",
    "Kantara",
    "Vikram",
]


def omdb_by_imdb_id(imdb_id: str) -> dict:
    """Fetch OMDb's record for an IMDb ID."""
    response = requests.get(
        config.OMDB_BASE_URL,
        params={"apikey": config.OMDB_API_KEY, "i": imdb_id},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def value_or_blank(raw: str) -> str:
    """OMDb writes the literal string 'N/A' for anything it does not hold."""
    text = str(raw or "").strip()
    return "" if text.upper() in {"", "N/A"} else text


def rating_from(payload: dict, source: str) -> str:
    for entry in payload.get("Ratings", []):
        if entry.get("Source") == source:
            return value_or_blank(entry.get("Value", ""))
    return ""


def main() -> int:
    if not config.OMDB_API_KEY:
        print("OMDB_API_KEY is not set — add it to .env (line 55) and re-run.")
        return 1

    rows = []
    print(f"{'Title':<24} {'IMDb':>6} {'RT':>6} {'Meta':>6}  {'TMDB':>5}")
    print("-" * 58)

    for title in TITLES:
        details = tmdb.fetch_title_details(title)
        if details["status"] != "success":
            print(f"{title:<24} {'—':>6} {'—':>6} {'—':>6}  (not on TMDB)")
            rows.append((title, "", "", "", False))
            continue

        resolved = details["title"]
        tmdb_rating = details["rating"]

        raw = tmdb._get(f"/movie/{details['tmdb_id']}")
        imdb_id = raw.get("imdb_id") or ""
        if not imdb_id:
            print(f"{resolved:<24} {'—':>6} {'—':>6} {'—':>6}  {tmdb_rating:>5}  (no IMDb id)")
            rows.append((resolved, "", "", "", False))
            continue

        try:
            payload = omdb_by_imdb_id(imdb_id)
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
            print(f"{resolved:<24} OMDb error: {type(exc).__name__}")
            rows.append((resolved, "", "", "", False))
            continue

        if payload.get("Response") == "False":
            print(f"{resolved:<24} not found in OMDb ({payload.get('Error','')})")
            rows.append((resolved, "", "", "", False))
            continue

        imdb = value_or_blank(payload.get("imdbRating", ""))
        rt = rating_from(payload, "Rotten Tomatoes")
        meta = value_or_blank(payload.get("Metascore", ""))

        print(
            f"{resolved:<24} {imdb or '—':>6} {rt or '—':>6} {meta or '—':>6}  {tmdb_rating:>5}"
        )
        rows.append((resolved, imdb, rt, meta, True))

    total = len(rows)
    found = sum(1 for r in rows if r[4])
    have_imdb = sum(1 for r in rows if r[1])
    have_rt = sum(1 for r in rows if r[2])
    have_meta = sum(1 for r in rows if r[3])

    def pct(n: int) -> str:
        return f"{n}/{total} ({100 * n // total}%)"

    print("\n" + "=" * 58)
    print("COVERAGE")
    print(f"  resolved in OMDb : {pct(found)}")
    print(f"  IMDb rating      : {pct(have_imdb)}")
    print(f"  Rotten Tomatoes  : {pct(have_rt)}")
    print(f"  Metacritic       : {pct(have_meta)}")
    print("=" * 58)
    print(
        "\nA column is worth adding at roughly 50%+ coverage. Below that it is\n"
        "mostly empty space in every row."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
