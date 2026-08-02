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
from movie_connoisseur.tools import tmdb

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


def _column_letter(index: int) -> str:
    """1-indexed column number to its A1 letter: 1 -> A, 15 -> O, 27 -> AA.

    The obvious chr(ord('A') + n) breaks silently past column Z.
    """
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


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
        "platform": str(row.get("OTT_Platform", "")),
        "genre": str(row.get("Genre", "")),
        "rating": rating,
        "review": str(row.get("User_Review", "")),
        "shared": str(row.get("Shared_Status", "")).strip().upper() == "TRUE",
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

    # Enrich from TMDB so Movie_Title, TMDB_ID and Genre stay canonical.
    resolved_title = str(title).strip()
    tmdb_id: int | str = ""
    genre = ""
    details = tmdb.fetch_movie_details(resolved_title)
    if details["status"] == "success":
        resolved_title = details["title"] or resolved_title
        tmdb_id = details["tmdb_id"]
        genre = ", ".join(details["genres"])
        if not platform and details["streaming_in_india"]:
            platform = details["streaming_in_india"][0]

    log_id = _new_log_id()
    row = [
        log_id,
        watch_date_value,
        resolved_title,
        tmdb_id,
        str(platform).strip(),
        genre,
        rating_value if rating_value else "",
        str(review).strip(),
        "FALSE",
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
            "tmdb_id": tmdb_id,
            "platform": str(platform).strip(),
            "genre": genre,
            "rating": rating_value,
            "review": str(review).strip(),
            "shared": False,
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
        "platform": str(row.get("OTT_Platform", "")),
        "genre": str(row.get("Genre", "")),
        "notes": str(row.get("Notes", "")),
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
    tmdb_id: int | str = ""
    genre = ""
    platform = ""
    details = tmdb.fetch_movie_details(resolved_title)
    if details["status"] == "success":
        resolved_title = details["title"] or resolved_title
        tmdb_id = details["tmdb_id"]
        genre = ", ".join(details["genres"])
        if details["streaming_in_india"]:
            platform = details["streaming_in_india"][0]

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
                tmdb_id,
                platform,
                genre,
                str(notes).strip(),
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
            "tmdb_id": tmdb_id,
            "platform": platform,
            "genre": genre,
            "notes": str(notes).strip(),
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
