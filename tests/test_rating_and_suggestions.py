"""Rating an existing journal entry, and picking what to watch tonight."""

from __future__ import annotations

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal


class FakeWorksheet:
    def __init__(self):
        self.updates = []

    def batch_update(self, updates):
        self.updates.extend(updates)


def journal_row(title: str, row: int, rating="", review=""):
    return {
        "Log_ID": f"LOG-{row}",
        "Watch_Date": "2026-07-29",
        "Movie_Title": title,
        "TMDB_ID": 1,
        "OTT_Platform": "Netflix",
        "Genre": "Drama",
        "User_Rating": rating,
        "User_Review": review,
        "Shared_Status": "FALSE",
        "_row": row,
    }


def watch_row(title: str, row: int, imdb="", tmdb="", platform="Netflix", added="2026-07-01", kind="movie"):
    return {
        "Watchlist_ID": f"WL-{row}",
        "Added_Date": added,
        "Movie_Title": title,
        "TMDB_ID": 1,
        "OTT_Platform": platform,
        "Genre": "Drama",
        "Notes": "",
        "IMDb_Rating": imdb,
        "TMDB_Rating": tmdb,
        "Media_Type": kind,
        "_row": row,
    }


# --- Rating an existing entry ------------------------------------------------


def test_rating_updates_the_existing_row(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_read_rows", lambda: [journal_row("Inception", 3)])
    monkeypatch.setattr(journal, "_worksheet", lambda: sheet)

    result = journal.rate_journal_entry(title="Inception", rating=4.5)

    assert result["status"] == "success"
    # Row 3, User_Rating is column G.
    assert sheet.updates == [{"range": "G3", "values": [[4.5]]}]
    assert result["entry"]["rating"] == 4.5


def test_rating_does_not_append_a_duplicate(monkeypatch):
    """The reason this tool exists.

    Re-logging via add_to_journal would leave two rows for one film and skew
    every statistic drawn from the journal.
    """
    sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_read_rows", lambda: [journal_row("Inception", 3)])
    monkeypatch.setattr(journal, "_worksheet", lambda: sheet)

    journal.rate_journal_entry(title="Inception", rating=5.0)

    assert not hasattr(sheet, "appended"), "must update in place, never append"


def test_review_only_leaves_the_rating_alone(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(
        journal, "_read_rows", lambda: [journal_row("Inception", 3, rating=4.0)]
    )
    monkeypatch.setattr(journal, "_worksheet", lambda: sheet)

    journal.rate_journal_entry(title="Inception", review="Holds up.")

    # Only the review column (H) is touched.
    assert [u["range"] for u in sheet.updates] == ["H3"]


def test_nothing_to_change_is_rejected(monkeypatch):
    monkeypatch.setattr(journal, "_read_rows", lambda: [journal_row("Inception", 3)])
    result = journal.rate_journal_entry(title="Inception")
    assert result["status"] == "error"
    assert "nothing to change" in result["error_message"].lower()


def test_rating_out_of_range_is_rejected(monkeypatch):
    monkeypatch.setattr(journal, "_read_rows", lambda: [journal_row("Inception", 3)])
    result = journal.rate_journal_entry(title="Inception", rating=9.0)
    assert result["status"] == "error"
    assert "between 1.0 and 5.0" in result["error_message"]


def test_rating_something_not_logged_says_so(monkeypatch):
    monkeypatch.setattr(journal, "_read_rows", lambda: [journal_row("Inception", 3)])
    result = journal.rate_journal_entry(title="Sholay", rating=5.0)
    assert result["status"] == "error"
    assert "not in the journal" in result["error_message"]


def test_ambiguous_title_asks_instead_of_guessing(monkeypatch):
    sheet = FakeWorksheet()
    rows = [journal_row("Drishyam", 2), journal_row("Drishyam 2", 3)]
    monkeypatch.setattr(journal, "_read_rows", lambda: rows)
    monkeypatch.setattr(journal, "_worksheet", lambda: sheet)

    result = journal.rate_journal_entry(title="Drish", rating=4.0)

    assert result["status"] == "error"
    assert len(result["candidates"]) == 2
    assert sheet.updates == [], "nothing may be written while ambiguous"


def test_rating_is_blocked_without_write_permission(monkeypatch):
    monkeypatch.setattr(config, "WRITE_ENABLED", False)

    def fail():
        raise AssertionError("a read-only session must not touch the sheet")

    monkeypatch.setattr(journal, "_worksheet", fail)
    monkeypatch.setattr(journal, "_read_rows", fail)

    result = journal.rate_journal_entry(title="Inception", rating=5.0)
    assert result["status"] == "error"
    assert "read-only" in result["error_message"]


# --- What to watch tonight ---------------------------------------------------


def test_suggestions_are_ranked_by_imdb(monkeypatch):
    rows = [
        watch_row("Weak", 2, imdb="5.1"),
        watch_row("Strong", 3, imdb="8.6"),
        watch_row("Middling", 4, imdb="7.0"),
    ]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    result = journal.suggest_from_watchlist()

    assert [s["title"] for s in result["suggestions"]] == ["Strong", "Middling", "Weak"]


def test_tmdb_is_the_fallback_when_imdb_is_missing(monkeypatch):
    rows = [watch_row("NoImdb", 2, tmdb="8.0"), watch_row("HasImdb", 3, imdb="6.0")]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    result = journal.suggest_from_watchlist()

    assert result["suggestions"][0]["title"] == "NoImdb"


def test_unrated_entries_sink_to_the_bottom(monkeypatch):
    rows = [watch_row("Unrated", 2), watch_row("Rated", 3, imdb="6.1")]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    result = journal.suggest_from_watchlist()

    assert result["suggestions"][0]["title"] == "Rated"


def test_platform_filter_accepts_an_alias(monkeypatch):
    rows = [
        watch_row("OnPrime", 2, imdb="8.0", platform="Amazon Prime Video"),
        watch_row("OnNetflix", 3, imdb="9.0", platform="Netflix"),
    ]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    # "Prime Video" is the same service as "Amazon Prime Video".
    result = journal.suggest_from_watchlist(platform="Prime Video")

    assert [s["title"] for s in result["suggestions"]] == ["OnPrime"]


def test_media_type_filter(monkeypatch):
    rows = [
        watch_row("AFilm", 2, imdb="8.0", kind="movie"),
        watch_row("ASeries", 3, imdb="9.0", kind="tv"),
    ]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    assert journal.suggest_from_watchlist(media_type="series")["suggestions"][0][
        "title"
    ] == "ASeries"


def test_waiting_longest_is_reported(monkeypatch):
    rows = [
        watch_row("Recent", 2, imdb="9.0", added="2026-07-01"),
        watch_row("Ancient", 3, imdb="6.0", added="2025-01-15"),
    ]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)

    result = journal.suggest_from_watchlist()

    # Highest rated leads, but the one gathering dust is surfaced separately.
    assert result["suggestions"][0]["title"] == "Recent"
    assert result["waiting_longest"]["title"] == "Ancient"


def test_no_match_for_the_filter_is_success_not_error(monkeypatch):
    monkeypatch.setattr(
        journal, "_read_watchlist_rows", lambda: [watch_row("X", 2, platform="Netflix")]
    )
    result = journal.suggest_from_watchlist(platform="Zee5")
    assert result["status"] == "success"
    assert result["suggestions"] == []
    assert "message" in result


def test_suggesting_needs_no_write_permission(monkeypatch):
    # Reading the watchlist is safe for a visitor.
    monkeypatch.setattr(config, "WRITE_ENABLED", False)
    monkeypatch.setattr(
        journal, "_read_watchlist_rows", lambda: [watch_row("X", 2, imdb="7.0")]
    )
    assert journal.suggest_from_watchlist()["status"] == "success"


# --- Re-enriching a stored series by its ID ---------------------------------


def test_media_type_travels_with_a_numeric_id(monkeypatch):
    """A bare TMDB ID is read as a film unless told otherwise.

    Re-enriching a stored series by its ID without passing Media_Type looked
    up /movie/<tv id> — a different record, or none at all. The backfill
    reported the series as "not resolvable on TMDB".
    """
    from movie_connoisseur.tools import journal as journal_mod
    from movie_connoisseur.tools import tmdb

    seen = {}

    def fake_details(title_or_id, media_type=""):
        seen["id"] = title_or_id
        seen["media_type"] = media_type
        return {"status": "error"}

    monkeypatch.setattr(tmdb, "fetch_title_details", fake_details)

    journal_mod._movie_metadata("97546", media_type="tv")

    assert seen["id"] == "97546"
    assert seen["media_type"] == "tv", "the catalogue must travel with the ID"


def test_media_type_defaults_to_empty_for_titles(monkeypatch):
    from movie_connoisseur.tools import journal as journal_mod
    from movie_connoisseur.tools import tmdb

    seen = {}
    monkeypatch.setattr(
        tmdb,
        "fetch_title_details",
        lambda t, media_type="": seen.update(media_type=media_type)
        or {"status": "error"},
    )

    journal_mod._movie_metadata("Inception")

    # A title is unambiguous enough for /search/multi to classify it.
    assert seen["media_type"] == ""
