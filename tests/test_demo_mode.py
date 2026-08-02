"""Tests for the read-only public demo build.

The guarantee under test: with DEMO_MODE on, nothing can write to the owner's
spreadsheet — not the agent, not a stray tool call.
"""

from __future__ import annotations

import importlib

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal


# Every tool that mutates the owner's spreadsheet.
WRITE_TOOLS = {"add_to_journal", "add_to_watchlist", "remove_from_watchlist"}


@pytest.fixture
def demo_tree():
    """A read-only agent tree, as served to an anonymous visitor."""
    import movie_connoisseur.agents as agents_module

    return agents_module.build_agent_tree(write_enabled=False)


def _journal_agent(tree):
    return next(a for a in tree.sub_agents if a.name == "journal_agent")


def test_demo_journal_agent_has_no_write_tools(demo_tree):
    names = {t.__name__ for t in _journal_agent(demo_tree).tools}
    assert not (names & WRITE_TOOLS)
    assert names == {"get_journal_history", "get_watchlist", "generate_shareable_summary"}


def test_no_agent_anywhere_can_write_in_demo_mode(demo_tree):
    # Guards against a write tool being added to the wrong agent later.
    for agent in demo_tree.sub_agents:
        assert not ({t.__name__ for t in agent.tools} & WRITE_TOOLS)


def test_write_tree_has_every_write_tool():
    import movie_connoisseur.agents as agents_module

    tree = agents_module.build_agent_tree(write_enabled=True)
    names = {t.__name__ for t in _journal_agent(tree).tools}
    assert WRITE_TOOLS <= names


def test_the_two_trees_differ_only_in_write_capability():
    import movie_connoisseur.agents as agents_module

    ro = {t.__name__ for t in _journal_agent(agents_module.build_agent_tree(False)).tools}
    rw = {t.__name__ for t in _journal_agent(agents_module.build_agent_tree(True)).tools}
    # search_titles only exists to support confirm-before-adding.
    assert rw - ro == WRITE_TOOLS | {"search_titles"}
    assert ro - rw == set()


def test_demo_journal_agent_is_told_it_is_read_only(demo_tree):
    assert "read-only" in _journal_agent(demo_tree).instruction.lower()


def test_normal_mode_keeps_the_write_tool():
    import movie_connoisseur.agents as agents_module

    names = {t.__name__ for t in agents_module.journal_agent.tools}
    assert "add_to_journal" in names


def test_add_to_journal_refuses_without_write_permission(monkeypatch):
    # Backstop: even if the tool is reachable, it must not write.
    monkeypatch.setattr(config, "WRITE_ENABLED", False)
    result = journal.add_to_journal(title="Maharaja", rating=5.0)
    assert result["status"] == "error"
    assert "read-only" in result["error_message"]


def test_shareable_summary_does_not_write_shared_status_in_demo(monkeypatch):
    rows = [
        {
            "Log_ID": "LOG-1",
            "Watch_Date": "2026-07-28",
            "Movie_Title": "Maharaja",
            "OTT_Platform": "Netflix",
            "Genre": "Thriller",
            "User_Rating": 4.5,
            "User_Review": "Great",
            "Shared_Status": "FALSE",
            "_row": 2,
        }
    ]
    monkeypatch.setattr(config, "WRITE_ENABLED", False)
    monkeypatch.setattr(journal, "_read_rows", lambda: rows)

    def fail_if_opened():
        raise AssertionError("read-only mode must not open the sheet for writing")

    monkeypatch.setattr(journal, "_worksheet", fail_if_opened)

    result = journal.generate_shareable_summary(limit=1)
    assert result["status"] == "success"
    assert "Maharaja" in result["summary"]


def test_shareable_summary_does_write_outside_demo_mode(monkeypatch):
    rows = [
        {
            "Log_ID": "LOG-1",
            "Watch_Date": "2026-07-28",
            "Movie_Title": "Maharaja",
            "OTT_Platform": "Netflix",
            "Genre": "Thriller",
            "User_Rating": 4.5,
            "User_Review": "Great",
            "Shared_Status": "FALSE",
            "_row": 2,
        }
    ]
    written = {}

    class FakeWorksheet:
        def batch_update(self, updates):
            written["updates"] = updates

    monkeypatch.setattr(config, "WRITE_ENABLED", True)
    monkeypatch.setattr(journal, "_read_rows", lambda: rows)
    monkeypatch.setattr(journal, "_worksheet", lambda: FakeWorksheet())

    result = journal.generate_shareable_summary(limit=1)
    assert result["status"] == "success"
    assert written["updates"][0]["values"] == [["TRUE"]]


@pytest.mark.parametrize(
    "env,expected_provider,expected_model,forced",
    [
        # Demo mode must land on the free provider whatever the secrets say.
        (
            {"DEMO_MODE": "true", "MODEL_PROVIDER": "openai"},
            "nvidia",
            "nvidia/nvidia-nemotron-nano-9b-v2",
            True,
        ),
        (
            {"DEMO_MODE": "true", "MODEL_PROVIDER": "gemini"},
            "nvidia",
            "nvidia/nvidia-nemotron-nano-9b-v2",
            True,
        ),
        # A model name meant for another provider must not survive the switch.
        (
            {"DEMO_MODE": "true", "MODEL_PROVIDER": "openai", "MODEL_NAME": "gpt-4o-mini"},
            "nvidia",
            "nvidia/nvidia-nemotron-nano-9b-v2",
            True,
        ),
        # A legitimate NIM model name is kept.
        (
            {"DEMO_MODE": "true", "MODEL_NAME": "meta/llama-3.3-70b-instruct"},
            "nvidia",
            "meta/llama-3.3-70b-instruct",
            False,
        ),
        # With writes enabled the operator's choice stands.
        (
            {"WRITE_ENABLED": "true", "MODEL_PROVIDER": "openai"},
            "openai",
            "gpt-4o-mini",
            False,
        ),
    ],
)
def test_demo_mode_locks_to_the_free_provider(
    monkeypatch, env, expected_provider, expected_model, forced
):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key in ("DEMO_MODE", "WRITE_ENABLED", "MODEL_PROVIDER", "MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    reloaded = importlib.reload(config)
    try:
        assert reloaded.MODEL_PROVIDER == expected_provider
        assert reloaded.MODEL_NAME == expected_model
        assert reloaded.PROVIDER_WAS_FORCED is forced
    finally:
        monkeypatch.undo()
        importlib.reload(config)


@pytest.mark.parametrize(
    "env,expected_demo,expected_cap",
    [
        # THE important case: configure nothing, get a read-only app. A
        # deployment that forgets to set anything must fail closed.
        ({}, True, 10),
        # Writes are opt-in.
        ({"WRITE_ENABLED": "true"}, False, 0),
        ({"WRITE_ENABLED": "yes"}, False, 0),
        ({"WRITE_ENABLED": "false"}, True, 10),
        # An explicit DEMO_MODE pins read-only even if writes were enabled.
        ({"WRITE_ENABLED": "true", "DEMO_MODE": "true"}, True, 10),
        # The cap can still be overridden.
        ({"MAX_MESSAGES_PER_SESSION": "3"}, True, 3),
        ({"WRITE_ENABLED": "true", "MAX_MESSAGES_PER_SESSION": "5"}, False, 5),
    ],
)
def test_write_access_is_opt_in_and_cap_follows(
    monkeypatch, env, expected_demo, expected_cap
):
    # Reloading config re-runs load_dotenv, which would pull the developer's
    # own .env back in and defeat delenv. Stub it so the test sees only `env`.
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key in ("DEMO_MODE", "WRITE_ENABLED", "MAX_MESSAGES_PER_SESSION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEMO_MODE is expected_demo
        assert reloaded.WRITE_ENABLED is not expected_demo
        assert reloaded.MAX_MESSAGES_PER_SESSION == expected_cap
    finally:
        monkeypatch.undo()
        importlib.reload(config)
