"""Offline tests for the agent tree and the chat wrapper — no API calls."""

from __future__ import annotations

import pytest

from movie_connoisseur import agents, config
from movie_connoisseur.chat import Turn, _describe_model_error

# --- Agent tree wiring -----------------------------------------------------


def test_coordinator_owns_the_three_specialists():
    names = {a.name for a in agents.coordinator_agent.sub_agents}
    assert names == {"discovery_agent", "critic_agent", "journal_agent"}


def test_root_agent_is_the_coordinator():
    assert agents.root_agent is agents.coordinator_agent


def test_coordinator_has_no_tools_of_its_own():
    # The coordinator routes; it must not answer movie questions itself.
    assert not agents.coordinator_agent.tools


@pytest.mark.parametrize(
    "agent,expected",
    [
        (agents.discovery_agent, {"fetch_ott_titles", "search_titles", "list_ott_providers"}),
        (
            agents.critic_agent,
            {
                "fetch_title_details",
                "fetch_credits",
                "search_titles",
                # TMDB carries only its own community score; IMDb, Rotten
                # Tomatoes and Metacritic come from OMDb.
                "fetch_external_ratings",
            },
        ),
        (
            agents.journal_agent,
            {
                "add_to_journal",
                "rate_journal_entry",
                "suggest_from_watchlist",
                "get_journal_history",
                "generate_shareable_summary",
                "add_to_watchlist",
                "get_watchlist",
                "remove_from_watchlist",
                # Needed for the confirm-before-adding step on the watchlist.
                "search_titles",
            },
        ),
    ],
)
def test_each_specialist_has_its_prd_tools(agent, expected):
    assert {t.__name__ for t in agent.tools} == expected


def test_every_agent_has_a_description_for_routing():
    # The coordinator routes on sub-agent descriptions, so they cannot be blank.
    for agent in agents.coordinator_agent.sub_agents:
        assert agent.description.strip()


def test_all_agents_share_one_model():
    # Compared by identity, not in a set: LiteLlm model objects are unhashable.
    for agent in agents.coordinator_agent.sub_agents:
        assert agent.model is agents.MODEL
    assert agents.coordinator_agent.model is agents.MODEL


# --- Model provider selection ----------------------------------------------


def test_gemini_provider_returns_a_plain_model_name(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "gemini")
    monkeypatch.setattr(config, "MODEL_NAME", "gemini-3.1-flash-lite")
    assert agents.build_model() == "gemini-3.1-flash-lite"


def test_nvidia_provider_without_key_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "nvidia")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        agents.build_model()


def test_openai_provider_without_key_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        agents.build_model()


@pytest.mark.parametrize(
    "provider,key_attr,expected_base",
    [
        ("openai", "OPENAI_API_KEY", None),
        ("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1"),
    ],
)
def test_litellm_providers_are_configured_correctly(
    monkeypatch, provider, key_attr, expected_base
):
    captured = {}

    def fake_lite_llm(**kwargs):
        captured.update(kwargs)
        return "model-object"

    monkeypatch.setattr(config, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(config, key_attr, "test-key")
    monkeypatch.setattr(config, "MODEL_NAME", "some-model")
    monkeypatch.setattr(agents, "_lite_llm", fake_lite_llm)

    assert agents.build_model() == "model-object"
    # NIM is OpenAI-compatible, so both use the openai/ prefix.
    assert captured["model"] == "openai/some-model"
    assert captured["api_key"] == "test-key"
    assert captured.get("api_base") == expected_base


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="Unknown MODEL_PROVIDER"):
        agents.build_model()


def test_importing_agents_without_a_key_does_not_raise(monkeypatch):
    """A missing key must degrade to a UI message, not a crash at import.

    Regression: the first Streamlit Cloud deploy died with a redacted
    RuntimeError from `MODEL = build_model()` before anything could render.
    """
    import importlib

    # Set the attribute rather than the environment: get_secret also falls back
    # to st.secrets, which reads .streamlit/secrets.toml if a developer has one,
    # so clearing os.environ alone does not guarantee an unset key.
    monkeypatch.setattr(config, "MODEL_PROVIDER", "nvidia")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")

    reloaded = importlib.reload(agents)
    try:
        assert reloaded.MODEL_ERROR
        assert "NVIDIA_API_KEY" in reloaded.MODEL_ERROR
        # The tree must still exist so the app can import and explain itself.
        assert reloaded.root_agent is not None
        assert "NVIDIA_API_KEY" in reloaded.missing_credentials()
    finally:
        monkeypatch.undo()
        importlib.reload(agents)


def test_no_model_error_when_configured():
    assert agents.MODEL_ERROR == ""


def test_missing_credentials_lists_what_is_absent(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "gemini")
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    monkeypatch.setattr(config, "SPREADSHEET_KEY", "key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "sa.json")
    assert agents.missing_credentials() == ["TMDB_API_KEY"]


@pytest.mark.parametrize(
    "provider,expected_key",
    [
        ("gemini", "GOOGLE_API_KEY or GEMINI_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("nvidia", "NVIDIA_API_KEY"),
    ],
)
def test_only_the_active_providers_key_is_required(monkeypatch, provider, expected_key):
    # Running on NIM must not demand a Gemini key, and vice versa.
    monkeypatch.setattr(config, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(config, "TMDB_API_KEY", "key")
    monkeypatch.setattr(config, "SPREADSHEET_KEY", "key")
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "sa.json")
    assert agents.missing_credentials() == [expected_key]


# --- Model error translation -----------------------------------------------


def test_rate_limit_error_extracts_retry_delay():
    message, wait = _describe_model_error(
        Exception("429 RESOURCE_EXHAUSTED ... Please retry in 52.101137755s.")
    )
    assert wait == 53
    assert "rate limit" in message.lower()


def test_rate_limit_without_a_delay_falls_back():
    message, wait = _describe_model_error(Exception("429 RESOURCE_EXHAUSTED"))
    assert wait == 60
    assert "rate limit" in message.lower()


def test_retired_model_error_points_at_model_name():
    message, wait = _describe_model_error(
        Exception("404 NOT_FOUND ... is no longer available to new users")
    )
    assert "MODEL_NAME" in message
    assert wait == 0


def test_bad_key_error_points_at_the_key():
    # Provider-neutral: the model may be Gemini, OpenAI or NIM.
    message, _ = _describe_model_error(Exception("400 API_KEY_INVALID"))
    assert "API key" in message
    assert ".env" in message


def test_self_transfer_error_is_recoverable():
    # Weaker models occasionally route to the agent they already are.
    message, wait = _describe_model_error(
        Exception("ValueError: Agent 'critic_agent' cannot transfer to itself.")
    )
    assert "rephrased" in message
    assert wait == 0


def test_unrecognised_error_is_passed_through():
    message, wait = _describe_model_error(Exception("kaboom"))
    assert "kaboom" in message
    assert wait == 0


# --- Turn ------------------------------------------------------------------


def test_failed_tools_filters_by_status():
    from movie_connoisseur.chat import ToolCall

    turn = Turn(
        text="",
        tool_calls=[
            ToolCall(name="a", status="success"),
            ToolCall(name="b", status="error", error="boom"),
        ],
    )
    assert [c.name for c in turn.failed_tools] == ["b"]
