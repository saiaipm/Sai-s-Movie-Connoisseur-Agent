"""Populate the rating columns for rows written before they existed.

Rows logged under the old schema have blanks in IMDb_ID, TMDB_Rating,
IMDb_Rating, RT_Rating, Metacritic and Synopsis. This fills them in from TMDB
and OMDb, matching on TMDB_ID where the row has one and falling back to title.

    uv run python scripts/backfill_ratings.py            # dry run, writes nothing
    uv run python scripts/backfill_ratings.py --apply    # actually write
    uv run python scripts/backfill_ratings.py --apply --force   # also overwrite

Dry run is the default deliberately: this edits the owner's real spreadsheet.
Existing user data — ratings, reviews, notes, dates — is never touched; only
the six metadata columns are written.
"""

from __future__ import annotations

import argparse
import sys

from movie_connoisseur import config
from movie_connoisseur.tools import journal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The columns this script owns. Nothing else is written.
FILLED_COLUMNS = [
    "IMDb_ID",
    "TMDB_Rating",
    "IMDb_Rating",
    "RT_Rating",
    "Metacritic",
    "Synopsis",
    "Media_Type",
    "Seasons",
]

# Sheet header -> key in the dict _movie_metadata returns.
FIELD_FOR = {
    "IMDb_ID": "imdb_id",
    "TMDB_Rating": "tmdb_rating",
    "IMDb_Rating": "imdb_rating",
    "RT_Rating": "rt_rating",
    "Metacritic": "metacritic",
    "Synopsis": "synopsis",
    "Media_Type": "media_type",
    "Seasons": "seasons",
}


def plan_for(rows, headers, force: bool):
    """Work out the cell updates needed, without contacting Sheets."""
    updates = []
    report = []

    for row in rows:
        title = str(row.get("Movie_Title", "")).strip()
        if not title:
            continue

        already = [c for c in FILLED_COLUMNS if str(row.get(c, "")).strip()]
        if already and not force:
            report.append((title, "skip", "already populated"))
            continue

        # Prefer the stored TMDB_ID: exact, and immune to title ambiguity like
        # the three different films called Maharaja. The stored Media_Type has
        # to travel with it — the same ID means different things in the film
        # and television catalogues, and a bare ID is read as a film.
        tmdb_id = str(row.get("TMDB_ID", "")).strip()
        lookup = tmdb_id if tmdb_id.isdigit() else title
        media_type = str(row.get("Media_Type", "")).strip()

        meta = journal._movie_metadata(lookup, media_type=media_type)
        if not meta["title"]:
            report.append((title, "fail", "not resolvable on TMDB"))
            continue

        for column in FILLED_COLUMNS:
            value = meta[FIELD_FOR[column]]
            cell = journal._column_letter(headers.index(column) + 1) + str(row["_row"])
            updates.append({"range": cell, "values": [[value]]})

        found = [c for c in FILLED_COLUMNS if str(meta[FIELD_FOR[c]]).strip()]
        missing = [c for c in FILLED_COLUMNS if c not in found]
        report.append(
            (title, "fill", f"got {len(found)}/{len(FILLED_COLUMNS)}" + (f", blank: {', '.join(missing)}" if missing else ""))
        )

    return updates, report


def run(sheet_name: str, worksheet, headers, apply: bool, force: bool) -> int:
    rows = journal._rows_of(worksheet, headers)
    print(f"\n{'=' * 66}\n{sheet_name} — {len(rows)} rows\n{'=' * 66}")
    if not rows:
        print("  (empty)")
        return 0

    updates, report = plan_for(rows, headers, force)

    for title, action, detail in report:
        marker = {"fill": "+", "skip": ".", "fail": "!"}[action]
        print(f"  {marker} {title:<34} {detail}")

    if not updates:
        print("\n  nothing to write")
        return 0

    if not apply:
        print(f"\n  DRY RUN — would write {len(updates)} cells. Re-run with --apply.")
        return 0

    # One batched call rather than a request per cell.
    worksheet.batch_update(updates)
    print(f"\n  wrote {len(updates)} cells")
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write to the sheet")
    parser.add_argument(
        "--force", action="store_true", help="overwrite rows that already have ratings"
    )
    args = parser.parse_args()

    if not config.OMDB_API_KEY:
        print("OMDB_API_KEY is not set — IMDb, RT and Metacritic would all be blank.")
        return 1

    written = 0
    written += run(
        "Movie_Journal",
        journal._worksheet(),
        list(config.JOURNAL_HEADERS),
        args.apply,
        args.force,
    )
    written += run(
        "Watchlist",
        journal._watchlist_worksheet(),
        list(config.WATCHLIST_HEADERS),
        args.apply,
        args.force,
    )

    print(f"\n{'=' * 66}")
    if args.apply:
        print(f"Done. {written} cells written.")
    else:
        print("Dry run only. Nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
