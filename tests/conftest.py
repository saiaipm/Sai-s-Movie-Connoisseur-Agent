"""Shared test setup.

Write access now defaults to OFF (fail-safe), and it is read from `.env` at
import time. Tests must not depend on whether a developer's `.env` happens to
enable it, so the default here is pinned to "writes allowed" and the demo-mode
tests opt into the restricted state explicitly.
"""

from __future__ import annotations

import pytest

from movie_connoisseur import config


@pytest.fixture(autouse=True)
def writes_enabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "WRITE_ENABLED", True)
    monkeypatch.setattr(config, "DEMO_MODE", False)
