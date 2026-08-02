"""Offline tests for the watchlist tools — no sheet or network access."""

from __future__ import annotations

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal, tmdb


def _row(title: str, row: int, wl_id: str = "WL-1") -> dict:
    return {
        "Watchlist_ID": wl_id,
        "Added_Date": "2026-07-29",
        "Movie_Title": title,
        "TMDB_ID": 111,
        "OTT_Platform": "Netflix",
        "Genre": "Thriller",
        "Notes": "",
        "_row": row,
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keep demo mode off and TMDB enrichment out of the way by default."""
    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(
        tmdb, "fetch_title_details", lambda *_a, **_k: {"status": "error"}
    )


class FakeWorksheet:
    def __init__(self):
        self.appended = []
        self.deleted = []

    def append_row(self, row, value_input_option=None):
        self.appended.append(row)

    def delete_rows(self, index):
        self.deleted.append(index)


# --- Matching ---------------------------------------------------------------


def test_exact_title_wins_over_partial():
    rows = [_row("Maharaja 2", 2), _row("Maharaja", 3)]
    assert journal._match_watchlist_rows(rows, "Maharaja")[0]["_row"] == 3


def test_partial_match_when_no_exact():
    rows = [_row("Maharaja 2", 2)]
    assert len(journal._match_watchlist_rows(rows, "maharaja")) == 1


def test_no_match_returns_empty():
    assert journal._match_watchlist_rows([_row("Maharaja", 2)], "Sholay") == []


# --- Adding -----------------------------------------------------------------


def test_add_requires_a_title():
    assert journal.add_to_watchlist(title="  ")["status"] == "error"


def test_add_appends_a_row(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [])
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: sheet)

    result = journal.add_to_watchlist(title="Maharaja", notes="Vijay Sethupathi")

    assert result["status"] == "success"
    assert result["already_present"] is False
    assert sheet.appended[0][2] == "Maharaja"
    assert sheet.appended[0][6] == "Vijay Sethupathi"
    assert result["entry"]["watchlist_id"].startswith("WL-")


def test_add_is_idempotent(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [_row("Maharaja", 2)])
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: sheet)

    result = journal.add_to_watchlist(title="Maharaja")

    assert result["status"] == "success"
    assert result["already_present"] is True
    assert sheet.appended == []


def test_add_enriches_from_tmdb(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(
        tmdb,
        "fetch_title_details",
        lambda *_a, **_k: {
            "status": "success",
            "title": "Maharaja",
            "tmdb_id": 1118224,
            "genres": ["Action", "Thriller"],
            "streaming_in_india": ["Netflix"],
        },
    )
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [])
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: sheet)

    journal.add_to_watchlist(title="maharaja")

    assert sheet.appended[0][2] == "Maharaja"
    assert sheet.appended[0][3] == 1118224
    assert sheet.appended[0][4] == "Netflix"
    assert sheet.appended[0][5] == "Action, Thriller"


# --- Removing ---------------------------------------------------------------


def test_remove_deletes_the_matching_row(monkeypatch):
    sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [_row("Maharaja", 4)])
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: sheet)

    result = journal.remove_from_watchlist(title="Maharaja")

    assert result["status"] == "success"
    assert sheet.deleted == [4]


def test_remove_reports_missing_titles(monkeypatch):
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [_row("Maharaja", 2)])
    result = journal.remove_from_watchlist(title="Sholay")
    assert result["status"] == "error"
    assert "not on the watchlist" in result["error_message"]


def test_ambiguous_removal_asks_instead_of_guessing(monkeypatch):
    sheet = FakeWorksheet()
    rows = [_row("Drishyam 2", 2, "WL-A"), _row("Drishyam 2 Remake", 3, "WL-B")]
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: rows)
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: sheet)

    result = journal.remove_from_watchlist(title="Drishyam")

    assert result["status"] == "error"
    assert len(result["candidates"]) == 2
    # Nothing may be deleted while the title is ambiguous.
    assert sheet.deleted == []


# --- Reading ----------------------------------------------------------------


def test_get_watchlist_is_newest_first(monkeypatch):
    old = _row("Old", 2)
    old["Added_Date"] = "2026-01-01"
    new = _row("New", 3)
    new["Added_Date"] = "2026-07-29"
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [old, new])

    result = journal.get_watchlist()

    assert [e["title"] for e in result["entries"]] == ["New", "Old"]
    assert result["total"] == 2


def test_empty_watchlist_is_success(monkeypatch):
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [])
    result = journal.get_watchlist()
    assert result["status"] == "success"
    assert result["entries"] == []


# --- Logging a watched film clears the watchlist ----------------------------


def test_logging_a_film_removes_it_from_the_watchlist(monkeypatch):
    journal_sheet = FakeWorksheet()
    watchlist_sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_worksheet", lambda: journal_sheet)
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: watchlist_sheet)
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [_row("Maharaja", 5)])

    result = journal.add_to_journal(title="Maharaja", rating=4.5)

    assert result["status"] == "success"
    assert result["removed_from_watchlist"] is True
    assert watchlist_sheet.deleted == [5]


def test_logging_still_succeeds_when_watchlist_removal_fails(monkeypatch):
    journal_sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_worksheet", lambda: journal_sheet)

    def boom():
        raise RuntimeError("sheets is down")

    monkeypatch.setattr(journal, "_read_watchlist_rows", boom)

    result = journal.add_to_journal(title="Maharaja", rating=4.5)

    # The log is the user's actual request; watchlist cleanup is best effort.
    assert result["status"] == "success"
    assert result["removed_from_watchlist"] is False


def test_logging_leaves_watchlist_alone_when_not_present(monkeypatch):
    journal_sheet = FakeWorksheet()
    watchlist_sheet = FakeWorksheet()
    monkeypatch.setattr(journal, "_worksheet", lambda: journal_sheet)
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: watchlist_sheet)
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [_row("Sholay", 2)])

    result = journal.add_to_journal(title="Maharaja", rating=4.0)

    assert result["removed_from_watchlist"] is False
    assert watchlist_sheet.deleted == []


# --- Demo mode --------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: journal.add_to_watchlist(title="Maharaja"),
        lambda: journal.remove_from_watchlist(title="Maharaja"),
    ],
)
def test_watchlist_writes_are_blocked_without_permission(monkeypatch, call):
    monkeypatch.setattr(config, "WRITE_ENABLED", False)

    def fail_if_opened():
        raise AssertionError("read-only mode must not open the watchlist for writing")

    monkeypatch.setattr(journal, "_watchlist_worksheet", fail_if_opened)

    result = call()
    assert result["status"] == "error"
    assert "read-only" in result["error_message"]
