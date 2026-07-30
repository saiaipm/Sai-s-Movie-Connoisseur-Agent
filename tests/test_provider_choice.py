"""Owner-only model provider selection.

The guarantee: an anonymous visitor can never cause a request to a billed
provider, no matter what the secrets contain or what the UI is asked to render.
"""

from __future__ import annotations

import pytest

from movie_connoisseur import agents, config

# The real implementation, not a copy: it lives in config precisely so it can be
# tested without importing app.py (which calls st.set_page_config at import).
resolve_provider = config.resolve_session_provider


# --- Which providers are on offer ------------------------------------------


def test_only_providers_with_keys_are_offered(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nv")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gm")
    assert config.available_providers() == ["nvidia", "gemini"]


def test_no_keys_means_no_options(monkeypatch):
    for attr in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(config, attr, "")
    assert config.available_providers() == []


def test_free_provider_is_listed_first(monkeypatch):
    for attr in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(config, attr, "key")
    assert config.available_providers()[0] == config.FREE_PROVIDER


# --- The cost guarantee -----------------------------------------------------


@pytest.mark.parametrize("chosen", ["openai", "gemini", "nvidia", "", "bogus"])
def test_a_visitor_always_gets_the_free_provider(monkeypatch, chosen):
    """Even asking for OpenAI explicitly must not move a visitor off the free tier."""
    for attr in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(config, attr, "key")
    assert resolve_provider(trusted=False, chosen=chosen) == config.FREE_PROVIDER


def test_the_free_provider_is_not_a_billed_one():
    # If this ever changes, the visitor guarantee above means nothing.
    assert config.FREE_PROVIDER == "nvidia"


def test_owner_selection_is_honoured(monkeypatch):
    for attr in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(config, attr, "key")
    assert resolve_provider(trusted=True, chosen="openai") == "openai"
    assert resolve_provider(trusted=True, chosen="gemini") == "gemini"


def test_owner_choosing_a_provider_without_a_key_falls_back(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nv")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "nvidia")
    assert resolve_provider(trusted=True, chosen="openai") == "nvidia"


# --- Model naming -----------------------------------------------------------


def test_default_model_matches_the_provider(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "nvidia")
    monkeypatch.setattr(config, "MODEL_NAME", "nvidia/nvidia-nemotron-nano-9b-v2")
    # Switching provider must not carry the other provider's model name over —
    # gpt-4o-mini against NIM, or a NIM name against OpenAI, would 404.
    assert config.default_model_for("openai") == config.DEFAULT_MODELS["openai"]
    assert config.default_model_for("gemini") == config.DEFAULT_MODELS["gemini"]
    assert config.default_model_for("nvidia") == "nvidia/nvidia-nemotron-nano-9b-v2"


def test_every_provider_has_a_label_and_default():
    for provider in ("nvidia", "gemini", "openai"):
        assert provider in config.PROVIDER_LABELS
        assert provider in config.DEFAULT_MODELS


# --- The tree honours the model it is given ---------------------------------


def test_agent_tree_uses_the_supplied_model():
    tree = agents.build_agent_tree(write_enabled=False, model="sentinel-model")
    assert tree.model == "sentinel-model"
    for sub in tree.sub_agents:
        assert sub.model == "sentinel-model"


def test_agent_tree_falls_back_to_the_configured_model():
    tree = agents.build_agent_tree(write_enabled=False)
    assert tree.model is agents.MODEL
