"""Shared test setup.

Write access now defaults to OFF (fail-safe), and it is read from `.env` at
import time. Tests must not depend on whether a developer's `.env` happens to
enable it, so the default here is pinned to "writes allowed" and the demo-mode
tests opt into the restricted state explicitly.
"""

from __future__ import annotations

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import omdb


@pytest.fixture(autouse=True)
def writes_enabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "WRITE_ENABLED", True)
    monkeypatch.setattr(config, "DEMO_MODE", False)


@pytest.fixture(autouse=True)
def omdb_offline(monkeypatch):
    """Keep the suite off the network.

    Journal and watchlist writes enrich rows from OMDb. With a real
    OMDB_API_KEY in the developer's .env those tests quietly made live HTTP
    calls — slow, rate-limited, and dependent on someone else's uptime.
    Blanking the key makes fetch_external_ratings short-circuit before any
    request; tests that exercise OMDb set it back and stub requests.get.
    """
    monkeypatch.setattr(config, "OMDB_API_KEY", "")
    omdb.reset_cache()
    yield
    omdb.reset_cache()
