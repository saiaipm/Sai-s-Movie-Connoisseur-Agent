"""Google Sheets tools for the Journal agent.

The journal lives in a single worksheet named ``Movie_Journal`` with the nine
columns defined in ``config.JOURNAL_HEADERS``. The worksheet and its header row
are created on first write, so a brand new (empty) spreadsheet works.

As with the TMDB tools, every public function returns a dict carrying a
``status`` key rather than raising.
"""

from __future__ import annotations

import functools
import uuid
from datetime import date, datetime
from typing import Any

import gspread
from google.adk.tools.tool_context import ToolContext
from google.oauth2.service_account import Credentials

from movie_connoisseur import config
from movie_connoisseur.tools import omdb, tmdb

# Key under which per-session write permission is stored in ADK session state.
WRITE_ENABLED_STATE_KEY = "write_enabled"

READ_ONLY_MESSAGE = (
    "This is a read-only view — the journal and watchlist belong to the app's "
    "owner. Sign in as the owner to make changes."
)

# Column positions are 1-indexed to match the A1 notation in the PRD schema.
SHARED_STATUS_COLUMN = config.JOURNAL_HEADERS.index("Shared_Status") + 1
FIRST_DATA_ROW = 2


class JournalError(RuntimeError):
    """Raised when the journal spreadsheet cannot be reached or read."""


# --- Connection ------------------------------------------------------------


def _column_letter(index: int) -> str:
    """1-indexed column number to its A1 letter: 1 -> A, 15 -> O, 27 -> AA.

    The obvious chr(ord('A') + n) breaks silently past column Z.
    """
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


@functools.lru_cache(maxsize=4)
def _open_worksheet(title: str, headers: tuple[str, ...]) -> gspread.Worksheet:
    """Open a worksheet by title, creating it with its header row if absent.

    Headers are passed as a tuple so the result can be cached.
    """
    if not config.SPREADSHEET_KEY:
        raise JournalError("SPREADSHEET_KEY is not set.")

    credentials = Credentials.from_service_account_info(
        config.service_account_info(), scopes=config.GOOGLE_API_SCOPES
    )
    client = gspread.authorize(credentials)

    try:
        spreadsheet = client.open_by_key(config.SPREADSHEET_KEY)
    except gspread.SpreadsheetNotFound as exc:
        raise JournalError(
            f"Spreadsheet {config.SPREADSHEET_KEY} not found. Check SPREADSHEET_KEY "
            "and confirm the sheet is shared with the service account email."
        ) from exc
    except gspread.exceptions.APIError as exc:
        raise JournalError(f"Google Sheets rejected the request: {exc}") from exc

    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    _reconcile_headers(worksheet, list(headers))
    return worksheet


def _reconcile_headers(worksheet, headers: list[str]) -> None:
    """Ensure the header row starts with ``headers``, leaving extras alone.

    Split out from _open_worksheet so it can be tested without Google.

    Two rules, both learned the hard way:

    - Only the columns this version owns are compared and rewritten. One
      spreadsheet is shared by whatever version is running, so a newer build
      may have appended columns; demanding an exact match would strip their
      headers off and leave orphaned data underneath.
    - The grid is widened first. Writing past the last column fails with
      "exceeds grid limits" rather than growing the sheet.
    """
    if worksheet.col_count < len(headers):
        worksheet.add_cols(len(headers) - worksheet.col_count)

    existing = worksheet.row_values(1)
    if existing[: len(headers)] != headers:
        worksheet.update(
            [headers], range_name=f"A1:{_column_letter(len(headers))}1"
        )


def _worksheet() -> gspread.Worksheet:
    """The Movie_Journal worksheet."""
    return _open_worksheet(config.WORKSHEET_NAME, tuple(config.JOURNAL_HEADERS))


def _watchlist_worksheet() -> gspread.Worksheet:
    """The Watchlist worksheet."""
    return _open_worksheet(
        config.WATCHLIST_WORKSHEET_NAME, tuple(config.WATCHLIST_HEADERS)
    )


def reset_connection() -> None:
    """Drop cached worksheet handles (use after changing credentials)."""
    _open_worksheet.cache_clear()


# --- Helpers ---------------------------------------------------------------


def _movie_metadata(title: str) -> dict[str, Any]:
    """Everything the sheets store about a film, from TMDB plus OMDb.

    Best effort throughout: a film that cannot be resolved, or ratings that do
    not exist, must never block the user's own entry from being written. Blank
    fields are normal — Metacritic in particular is absent for roughly
    two-thirds of Indian releases.
    """
    blank = {
        "title": "",
        "tmdb_id": "",
        "imdb_id": "",
        "genre": "",
        "platform": "",
        "tmdb_rating": "",
        "imdb_rating": "",
        "rt_rating": "",
        "metacritic": "",
        "synopsis": "",
        "media_type": "",
        "seasons": "",
    }

    details = tmdb.fetch_title_details(title)
    if details.get("status") != "success":
        return blank

    streaming = details.get("streaming_in_india") or []
    ratings = omdb.ratings_or_blank(
        imdb_id=details.get("imdb_id", ""), title=details.get("title", "")
    )

    return {
        "title": details.get("title", ""),
        "tmdb_id": details.get("tmdb_id", ""),
        "imdb_id": details.get("imdb_id", ""),
        "genre": ", ".join(details.get("genres") or []),
        "platform": tmdb.canonical_platform(streaming[0]) if streaming else "",
        "tmdb_rating": details.get("rating", "") or "",
        "imdb_rating": ratings["imdb_rating"],
        "rt_rating": ratings["rt_rating"],
        "metacritic": ratings["metacritic"],
        "synopsis": details.get("overview", ""),
        "media_type": details.get("media_type", "movie"),
        # Only meaningful for a series; blank rather than 0 for films so the
        # column reads cleanly in the sheet.
        "seasons": details.get("seasons") or "",
    }


def writes_allowed(tool_context: ToolContext | None = None) -> bool:
    """Whether the caller may modify the owner's spreadsheet.

    Permission is per-session, carried in ADK session state, because one
    Streamlit process serves many visitors: reading a module-level flag would
    let the signed-in owner's permission leak to everyone else. The
    deployment-wide setting is only the fallback for callers with no session
    (scripts, tests).
    """
    if tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None:
            try:
                if WRITE_ENABLED_STATE_KEY in state:
                    return bool(state[WRITE_ENABLED_STATE_KEY])
            except TypeError:  # pragma: no cover - defensive
                pass
    return bool(config.WRITE_ENABLED)


def _new_log_id() -> str:
    return f"LOG-{uuid.uuid4().hex[:8].upper()}"


def _normalise_date(watch_date: str) -> str:
    """Coerce a date string to YYYY-MM-DD, defaulting to today."""
    text = str(watch_date).strip()
    if not text or text.lower() in {"today", "now"}:
        return date.today().isoformat()

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Could not read '{watch_date}' as a date. Use YYYY-MM-DD.")


def _rows_of(worksheet: gspread.Worksheet, headers: list[str]) -> list[dict[str, Any]]:
    """Return every row with its 1-indexed sheet row number attached."""
    records = worksheet.get_all_records(expected_headers=headers)
    rows = []
    for offset, record in enumerate(records):
        record["_row"] = FIRST_DATA_ROW + offset
        rows.append(record)
    return rows


def _read_rows() -> list[dict[str, Any]]:
    """Every Movie_Journal row."""
    return _rows_of(_worksheet(), config.JOURNAL_HEADERS)


def _read_watchlist_rows() -> list[dict[str, Any]]:
    """Every Watchlist row."""
    return _rows_of(_watchlist_worksheet(), config.WATCHLIST_HEADERS)


def _sort_newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order by watch date descending, using sheet position to break ties."""
    return sorted(
        rows,
        key=lambda r: (str(r.get("Watch_Date", "")), r["_row"]),
        reverse=True,
    )


def summarise_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate journal entries into the dashboard statistics.

    Pure and Streamlit-free so it can be tested directly.

    Every figure reports the sample it was computed from, because a "top
    genre" drawn from three films is noise dressed up as insight — the UI
    uses that count to decide whether to show the figure at all.
    """
    from collections import Counter

    def as_number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    genres: Counter[str] = Counter()
    platforms: Counter[str] = Counter()
    months: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    paired: list[tuple[float, float]] = []

    for entry in entries:
        # Genre is a comma-separated list, so a row counts towards each.
        for genre in str(entry.get("genre", "")).split(","):
            name = genre.strip()
            if name:
                genres[name] += 1

        platform = str(entry.get("platform", "")).strip()
        if platform:
            platforms[platform] += 1

        watched = str(entry.get("watch_date", ""))[:7]
        if len(watched) == 7:
            months[watched] += 1

        kinds[str(entry.get("media_type") or "movie")] += 1

        # Only films the user actually rated can be compared with the critics.
        mine, theirs = as_number(entry.get("rating")), as_number(entry.get("imdb_rating"))
        if mine and theirs:
            paired.append((mine, theirs))

    taste = None
    if paired:
        # User ratings are out of 5, IMDb out of 10, so double before comparing.
        mine_avg = sum(p[0] for p in paired) / len(paired) * 2
        theirs_avg = sum(p[1] for p in paired) / len(paired)
        taste = {
            "yours": round(mine_avg, 1),
            "critics": round(theirs_avg, 1),
            "delta": round(mine_avg - theirs_avg, 1),
            "sample": len(paired),
        }

    return {
        "total": len(entries),
        "genres": genres.most_common(),
        "platforms": platforms.most_common(),
        "months": sorted(months.items()),
        "media_types": dict(kinds),
        "taste": taste,
    }


def _to_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw sheet row into the entry shape the agent presents."""
    try:
        rating = float(row.get("User_Rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0

    return {
        "log_id": str(row.get("Log_ID", "")),
        "watch_date": str(row.get("Watch_Date", "")),
        "title": str(row.get("Movie_Title", "")),
        "tmdb_id": row.get("TMDB_ID", ""),
        "imdb_id": str(row.get("IMDb_ID", "")),
        # Normalised on read too, so rows written before this still aggregate
        # correctly in the platform breakdown.
        "platform": tmdb.canonical_platform(row.get("OTT_Platform", "")),
        "genre": str(row.get("Genre", "")),
        "rating": rating,
        "review": str(row.get("User_Review", "")),
        "shared": str(row.get("Shared_Status", "")).strip().upper() == "TRUE",
        # Blank for rows logged before these columns existed, and for films the
        # source has no score for.
        "tmdb_rating": row.get("TMDB_Rating", ""),
        "imdb_rating": row.get("IMDb_Rating", ""),
        "rt_rating": row.get("RT_Rating", ""),
        "metacritic": row.get("Metacritic", ""),
        "synopsis": str(row.get("Synopsis", "")),
        # Blank for rows written before television was supported.
        "media_type": str(row.get("Media_Type", "")),
        "seasons": row.get("Seasons", ""),
    }


# --- Journal Agent tools ---------------------------------------------------


def add_to_journal(
    title: str,
    platform: str = "",
    rating: float = 0.0,
    review: str = "",
    watch_date: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Log a watched movie to the user's Google Sheet movie journal.

    The TMDB ID and genre are looked up automatically from the title, so only
    the user's own details need to be supplied.

    Args:
        title: The movie title, e.g. "Stree 2".
        platform: The OTT platform it was watched on, e.g. "Disney+ Hotstar".
            Leave empty if the user did not say.
        rating: The user's personal star rating from 1.0 to 5.0, e.g. 4.0. Use
            0.0 if the user did not give one.
        review: The user's note or mini-review. Leave empty if none was given.
        watch_date: Date watched as YYYY-MM-DD. Leave empty for today.

    Returns:
        dict with ``status`` and, on success, the stored entry including its
        generated ``log_id``.
    """
    # The read-only build also withholds this tool from the agent; this is the
    # backstop so no code path can write to the owner's sheet without permission.
    if not writes_allowed(tool_context):
        return {"status": "error", "error_message": READ_ONLY_MESSAGE}

    if not str(title).strip():
        return {"status": "error", "error_message": "A movie title is required."}

    try:
        rating_value = float(rating or 0)
    except (TypeError, ValueError):
        return {"status": "error", "error_message": f"'{rating}' is not a valid rating."}

    if rating_value and not 1.0 <= rating_value <= 5.0:
        return {
            "status": "error",
            "error_message": f"Rating must be between 1.0 and 5.0, got {rating_value}.",
        }

    try:
        watch_date_value = _normalise_date(watch_date)
    except ValueError as exc:
        return {"status": "error", "error_message": str(exc)}

    # Enrich from TMDB so Movie_Title, TMDB_ID and Genre stay canonical, then
    # snapshot the critic scores as they stood when this was logged.
    resolved_title = str(title).strip()
    meta = _movie_metadata(resolved_title)
    resolved_title = meta["title"] or resolved_title
    if not platform and meta["platform"]:
        platform = meta["platform"]

    log_id = _new_log_id()
    row = [
        log_id,
        watch_date_value,
        resolved_title,
        meta["tmdb_id"],
        tmdb.canonical_platform(platform),
        meta["genre"],
        rating_value if rating_value else "",
        str(review).strip(),
        "FALSE",
        meta["imdb_id"],
        meta["tmdb_rating"],
        meta["imdb_rating"],
        meta["rt_rating"],
        meta["metacritic"],
        meta["synopsis"],
        meta["media_type"],
        meta["seasons"],
    ]

    try:
        _worksheet().append_row(row, value_input_option="USER_ENTERED")
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    # A watched film should not linger on the "want to watch" list.
    removed_from_watchlist = _drop_from_watchlist_quietly(resolved_title)

    return {
        "status": "success",
        "message": f"Logged '{resolved_title}' to the journal."
        + (
            f" Also removed it from your watchlist."
            if removed_from_watchlist
            else ""
        ),
        "removed_from_watchlist": bool(removed_from_watchlist),
        "entry": {
            "log_id": log_id,
            "watch_date": watch_date_value,
            "title": resolved_title,
            "tmdb_id": meta["tmdb_id"],
            "imdb_id": meta["imdb_id"],
            "platform": tmdb.canonical_platform(platform),
            "genre": meta["genre"],
            "rating": rating_value,
            "review": str(review).strip(),
            "shared": False,
            "tmdb_rating": meta["tmdb_rating"],
            "imdb_rating": meta["imdb_rating"],
            "rt_rating": meta["rt_rating"],
            "metacritic": meta["metacritic"],
            "synopsis": meta["synopsis"],
            "media_type": meta["media_type"],
            "seasons": meta["seasons"],
        },
    }


def get_journal_history(limit: int = 10, filter_rating: float = 0.0) -> dict:
    """Read the user's logged movies from the Google Sheet, newest first.

    Args:
        limit: How many entries to return. Defaults to 10, capped at 100.
        filter_rating: Only return entries rated at or above this value, e.g.
            4.0. Use 0.0 for no filter.

    Returns:
        dict with ``status`` and, on success, an ``entries`` list plus the
        total number of movies in the journal.
    """
    limit = max(1, min(int(limit or 10), 100))

    try:
        rows = _read_rows()
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    entries = [_to_entry(row) for row in _sort_newest_first(rows)]

    if filter_rating:
        entries = [e for e in entries if e["rating"] >= float(filter_rating)]

    if not entries:
        return {
            "status": "success",
            "entries": [],
            "total_logged": len(rows),
            "message": "No journal entries matched." if rows else "The journal is empty.",
        }

    return {
        "status": "success",
        "entries": entries[:limit],
        "total_logged": len(rows),
    }


# --- Watchlist tools -------------------------------------------------------


def _to_watchlist_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "watchlist_id": str(row.get("Watchlist_ID", "")),
        "added_date": str(row.get("Added_Date", "")),
        "title": str(row.get("Movie_Title", "")),
        "tmdb_id": row.get("TMDB_ID", ""),
        "imdb_id": str(row.get("IMDb_ID", "")),
        # Normalised on read too, so rows written before this still aggregate
        # correctly in the platform breakdown.
        "platform": tmdb.canonical_platform(row.get("OTT_Platform", "")),
        "genre": str(row.get("Genre", "")),
        "notes": str(row.get("Notes", "")),
        "tmdb_rating": row.get("TMDB_Rating", ""),
        "imdb_rating": row.get("IMDb_Rating", ""),
        "rt_rating": row.get("RT_Rating", ""),
        "metacritic": row.get("Metacritic", ""),
        "synopsis": str(row.get("Synopsis", "")),
        # Blank for rows written before television was supported.
        "media_type": str(row.get("Media_Type", "")),
        "seasons": row.get("Seasons", ""),
    }


def _match_watchlist_rows(rows: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """Rows whose title matches, preferring an exact match over a partial one."""
    wanted = title.strip().lower()
    exact = [r for r in rows if str(r.get("Movie_Title", "")).strip().lower() == wanted]
    if exact:
        return exact
    return [r for r in rows if wanted and wanted in str(r.get("Movie_Title", "")).lower()]


def add_to_watchlist(
    title: str, notes: str = "", tool_context: ToolContext = None
) -> dict:
    """Add a movie the user wants to watch later to their watchlist.

    Confirm the exact film with the user before calling this — search first and
    check you have the right title and year, since many films share a name.

    Args:
        title: The movie title, e.g. "Maharaja".
        notes: Optional note on why they want to watch it. Leave empty if none.

    Returns:
        dict with ``status`` and, on success, the stored watchlist entry.
    """
    if not writes_allowed(tool_context):
        return {"status": "error", "error_message": READ_ONLY_MESSAGE}

    if not str(title).strip():
        return {"status": "error", "error_message": "A movie title is required."}

    resolved_title = str(title).strip()
    meta = _movie_metadata(resolved_title)
    resolved_title = meta["title"] or resolved_title

    try:
        existing = _match_watchlist_rows(_read_watchlist_rows(), resolved_title)
        if existing:
            return {
                "status": "success",
                "message": f"'{resolved_title}' is already on the watchlist.",
                "already_present": True,
                "entry": _to_watchlist_entry(existing[0]),
            }

        watchlist_id = f"WL-{uuid.uuid4().hex[:8].upper()}"
        _watchlist_worksheet().append_row(
            [
                watchlist_id,
                date.today().isoformat(),
                resolved_title,
                meta["tmdb_id"],
                meta["platform"],
                meta["genre"],
                str(notes).strip(),
                meta["imdb_id"],
                meta["tmdb_rating"],
                meta["imdb_rating"],
                meta["rt_rating"],
                meta["metacritic"],
                meta["synopsis"],
                meta["media_type"],
                meta["seasons"],
            ],
            value_input_option="USER_ENTERED",
        )
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    return {
        "status": "success",
        "message": f"Added '{resolved_title}' to the watchlist.",
        "already_present": False,
        "entry": {
            "watchlist_id": watchlist_id,
            "added_date": date.today().isoformat(),
            "title": resolved_title,
            "tmdb_id": meta["tmdb_id"],
            "imdb_id": meta["imdb_id"],
            "platform": meta["platform"],
            "genre": meta["genre"],
            "notes": str(notes).strip(),
            "tmdb_rating": meta["tmdb_rating"],
            "imdb_rating": meta["imdb_rating"],
            "rt_rating": meta["rt_rating"],
            "metacritic": meta["metacritic"],
            "synopsis": meta["synopsis"],
            "media_type": meta["media_type"],
            "seasons": meta["seasons"],
        },
    }


def get_watchlist(limit: int = 20) -> dict:
    """Read the movies the user has saved to watch later.

    Args:
        limit: How many entries to return, newest first. Defaults to 20,
            capped at 100.

    Returns:
        dict with ``status`` and, on success, an ``entries`` list.
    """
    limit = max(1, min(int(limit or 20), 100))

    try:
        rows = _read_watchlist_rows()
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    if not rows:
        return {
            "status": "success",
            "entries": [],
            "total": 0,
            "message": "The watchlist is empty.",
        }

    ordered = sorted(
        rows, key=lambda r: (str(r.get("Added_Date", "")), r["_row"]), reverse=True
    )
    return {
        "status": "success",
        "entries": [_to_watchlist_entry(r) for r in ordered[:limit]],
        "total": len(rows),
    }


def remove_from_watchlist(title: str, tool_context: ToolContext = None) -> dict:
    """Remove a movie from the user's watchlist.

    If several saved films match the title, this returns the candidates instead
    of guessing — ask the user which one they meant and call again with the
    exact title.

    Args:
        title: The movie title to remove, e.g. "Maharaja".

    Returns:
        dict with ``status``. On success, the removed entry. If the title is
        ambiguous, ``status`` is "error" with a ``candidates`` list.
    """
    if not writes_allowed(tool_context):
        return {"status": "error", "error_message": READ_ONLY_MESSAGE}

    if not str(title).strip():
        return {"status": "error", "error_message": "A movie title is required."}

    try:
        rows = _read_watchlist_rows()
        matches = _match_watchlist_rows(rows, str(title))

        if not matches:
            return {
                "status": "error",
                "error_message": f"'{title}' is not on the watchlist.",
            }

        if len(matches) > 1:
            return {
                "status": "error",
                "error_message": (
                    f"Several watchlist entries match '{title}'. Ask which one "
                    "and call again with the exact title."
                ),
                "candidates": [_to_watchlist_entry(r) for r in matches],
            }

        removed = _to_watchlist_entry(matches[0])
        _watchlist_worksheet().delete_rows(matches[0]["_row"])
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    return {
        "status": "success",
        "message": f"Removed '{removed['title']}' from the watchlist.",
        "entry": removed,
    }


def _drop_from_watchlist_quietly(title: str) -> str:
    """Remove a title from the watchlist, ignoring failures.

    Used when a film is logged as watched: keeping it on the "want to watch"
    list would be stale. Never blocks the log itself.
    """
    try:
        matches = _match_watchlist_rows(_read_watchlist_rows(), title)
        if len(matches) == 1:
            _watchlist_worksheet().delete_rows(matches[0]["_row"])
            return str(matches[0].get("Movie_Title", title))
    except Exception:  # noqa: BLE001 — best effort, logging already succeeded
        pass
    return ""


def _format_card(entries: list[dict[str, Any]]) -> str:
    """Render entries as the WhatsApp-friendly card from the PRD."""
    lines = ["🍿 My Recent Movie Logs:"]
    for index, entry in enumerate(entries, start=1):
        platform = f" ({entry['platform']})" if entry["platform"] else ""
        rating = f" - ⭐️ {entry['rating']:.1f}/5" if entry["rating"] else ""
        lines.append(f"{index}. {entry['title']}{platform}{rating}")
        if entry["review"]:
            lines.append(f'   "{entry["review"]}"')
    return "\n".join(lines)


def generate_shareable_summary(
    log_ids: str = "", limit: int = 3, tool_context: ToolContext = None
) -> dict:
    """Format journal entries into a shareable card for WhatsApp or social media.

    Marks the included entries as shared in the sheet.

    Args:
        log_ids: Comma-separated Log_IDs to include, e.g. "LOG-9821,LOG-4410".
            Leave empty to use the most recent entries.
        limit: How many recent entries to include when log_ids is empty.
            Defaults to 3, capped at 20.

    Returns:
        dict with ``status`` and, on success, a ready-to-paste ``summary``
        string plus the entries it covers.
    """
    limit = max(1, min(int(limit or 3), 20))

    try:
        rows = _read_rows()
    except (JournalError, gspread.exceptions.APIError) as exc:
        return {"status": "error", "error_message": str(exc)}

    if not rows:
        return {"status": "error", "error_message": "The journal is empty — nothing to share."}

    wanted = [lid.strip().upper() for lid in str(log_ids).split(",") if lid.strip()]
    if wanted:
        by_id = {str(r.get("Log_ID", "")).strip().upper(): r for r in rows}
        selected = [by_id[lid] for lid in wanted if lid in by_id]
        missing = [lid for lid in wanted if lid not in by_id]
        if not selected:
            return {
                "status": "error",
                "error_message": f"No journal entries found for: {', '.join(missing)}",
            }
    else:
        selected = _sort_newest_first(rows)[:limit]
        missing = []

    entries = [_to_entry(row) for row in selected]

    # Flag the shared rows so Shared_Status reflects what has gone out. Skipped
    # without write permission — a read-only visitor must not touch the sheet,
    # but the card itself is still useful to them.
    if writes_allowed(tool_context):
        try:
            worksheet = _worksheet()
            worksheet.batch_update(
                [
                    {
                        "range": gspread.utils.rowcol_to_a1(
                            row["_row"], SHARED_STATUS_COLUMN
                        ),
                        "values": [["TRUE"]],
                    }
                    for row in selected
                ]
            )
        except (JournalError, gspread.exceptions.APIError) as exc:
            return {
                "status": "error",
                "error_message": f"Could not update Shared_Status: {exc}",
            }

    result = {
        "status": "success",
        "summary": _format_card(entries),
        "entries": entries,
        "shared_count": len(entries),
    }
    if missing:
        result["warning"] = f"Not found in the journal: {', '.join(missing)}"
    return result
