"""Per-session write permission.

One Streamlit process serves many visitors at once. If write permission were a
module-level flag, the signed-in owner's permission would leak to every
concurrent anonymous visitor. These tests pin down that it rides on ADK session
state instead.
"""

from __future__ import annotations

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal, tmdb
from movie_connoisseur.tools.journal import WRITE_ENABLED_STATE_KEY, writes_allowed


class FakeToolContext:
    """Stand-in for ADK's ToolContext — only `state` is consulted."""

    def __init__(self, state: dict | None = None):
        self.state = {} if state is None else state


class FakeWorksheet:
    def __init__(self):
        self.appended = []
        self.deleted = []

    def append_row(self, row, value_input_option=None):
        self.appended.append(row)

    def delete_rows(self, index):
        self.deleted.append(index)


@pytest.fixture(autouse=True)
def no_tmdb(monkeypatch):
    monkeypatch.setattr(
        tmdb, "fetch_movie_details", lambda *_a, **_k: {"status": "error"}
    )


# --- writes_allowed resolution ---------------------------------------------


def test_session_state_grants_permission(monkeypatch):
    # Deployment default says no; this session says yes.
    monkeypatch.setattr(config, "WRITE_ENABLED", False)
    ctx = FakeToolContext({WRITE_ENABLED_STATE_KEY: True})
    assert writes_allowed(ctx) is True


def test_session_state_denies_permission(monkeypatch):
    # Deployment default says yes; this session says no. The session wins.
    monkeypatch.setattr(config, "WRITE_ENABLED", True)
    ctx = FakeToolContext({WRITE_ENABLED_STATE_KEY: False})
    assert writes_allowed(ctx) is False


@pytest.mark.parametrize("default", [True, False])
def test_falls_back_to_deployment_setting_without_a_session(monkeypatch, default):
    # Scripts and tests call tools with no ToolContext at all.
    monkeypatch.setattr(config, "WRITE_ENABLED", default)
    assert writes_allowed(None) is default
    assert writes_allowed(FakeToolContext({})) is default


def test_a_context_without_state_does_not_crash(monkeypatch):
    monkeypatch.setattr(config, "WRITE_ENABLED", False)

    class Bare:
        pass

    assert writes_allowed(Bare()) is False


# --- The isolation guarantee ------------------------------------------------


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (journal.add_to_journal, {"title": "Maharaja", "rating": 4.0}),
        (journal.add_to_watchlist, {"title": "Maharaja"}),
        (journal.remove_from_watchlist, {"title": "Maharaja"}),
    ],
)
def test_anonymous_session_is_refused_even_when_deployment_allows_writes(
    monkeypatch, tool, kwargs
):
    """The leak this design exists to prevent.

    WRITE_ENABLED is true (the owner is signed in somewhere), but *this*
    visitor's session says no. Nothing may reach the spreadsheet.
    """
    monkeypatch.setattr(config, "WRITE_ENABLED", True)

    def fail(*_a, **_k):
        raise AssertionError("a read-only session must not touch the sheet")

    monkeypatch.setattr(journal, "_worksheet", fail)
    monkeypatch.setattr(journal, "_watchlist_worksheet", fail)
    monkeypatch.setattr(journal, "_read_watchlist_rows", fail)

    result = tool(**kwargs, tool_context=FakeToolContext({WRITE_ENABLED_STATE_KEY: False}))
    assert result["status"] == "error"
    assert "read-only" in result["error_message"]


def test_owner_session_may_write_even_when_deployment_default_is_read_only(monkeypatch):
    """The other direction: a public deploy where the owner has signed in."""
    sheet = FakeWorksheet()
    monkeypatch.setattr(config, "WRITE_ENABLED", False)
    monkeypatch.setattr(journal, "_worksheet", lambda: sheet)
    monkeypatch.setattr(journal, "_read_watchlist_rows", lambda: [])
    monkeypatch.setattr(journal, "_watchlist_worksheet", lambda: FakeWorksheet())

    result = journal.add_to_journal(
        title="Inception",
        rating=5.0,
        tool_context=FakeToolContext({WRITE_ENABLED_STATE_KEY: True}),
    )

    assert result["status"] == "success"
    assert sheet.appended[0][2] == "Inception"


def test_shared_status_write_follows_session_permission(monkeypatch):
    rows = [
        {
            "Log_ID": "LOG-1",
            "Watch_Date": "2026-07-29",
            "Movie_Title": "Inception",
            "OTT_Platform": "Netflix",
            "Genre": "Sci-Fi",
            "User_Rating": 5.0,
            "User_Review": "",
            "Shared_Status": "FALSE",
            "_row": 2,
        }
    ]
    monkeypatch.setattr(config, "WRITE_ENABLED", True)
    monkeypatch.setattr(journal, "_read_rows", lambda: rows)

    def fail():
        raise AssertionError("a read-only session must not update Shared_Status")

    monkeypatch.setattr(journal, "_worksheet", fail)

    # The card is still produced for a read-only visitor, just not persisted.
    result = journal.generate_shareable_summary(
        limit=1, tool_context=FakeToolContext({WRITE_ENABLED_STATE_KEY: False})
    )
    assert result["status"] == "success"
    assert "Inception" in result["summary"]


# --- tool_context must stay invisible to the model -------------------------


@pytest.mark.parametrize(
    "tool",
    [
        journal.add_to_journal,
        journal.add_to_watchlist,
        journal.remove_from_watchlist,
        journal.generate_shareable_summary,
    ],
)
def test_tool_context_is_not_exposed_to_the_llm(tool):
    """ADK injects tool_context and hides it from the function declaration.

    If it ever leaked into the schema the model could pass its own value and
    grant itself write access.
    """
    from google.adk.tools.function_tool import FunctionTool

    declaration = FunctionTool(tool)._get_declaration()
    schema = declaration.parameters_json_schema or {}
    assert "tool_context" not in (schema.get("properties") or {})
    assert "tool_context" not in (schema.get("required") or [])
