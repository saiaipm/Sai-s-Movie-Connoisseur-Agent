"""Offline tests for the Phase 1 tool logic — no API keys or network needed."""

from __future__ import annotations

import datetime

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal, tmdb


def _clear_genre_cache() -> None:
    # A test may have replaced _live_genre_map with a plain stub.
    clear = getattr(tmdb._live_genre_map, "cache_clear", None)
    if clear:
        clear()


@pytest.fixture(autouse=True)
def isolate_tmdb_state(monkeypatch):
    """Keep host failover and the genre cache from leaking between tests."""
    monkeypatch.setattr(tmdb, "_active_host", None)
    _clear_genre_cache()
    yield
    _clear_genre_cache()


@pytest.fixture
def no_network(monkeypatch):
    """Fail any outbound HTTP call so a test cannot silently hit the internet."""

    def blocked(*args, **kwargs):
        raise AssertionError("test attempted a real network call")

    monkeypatch.setattr(tmdb.requests, "get", blocked)


# --- Resolvers -------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Netflix", 8),
        ("netflix", 8),
        ("8", 8),
        ("Prime Video", 119),
        # Disney+ Hotstar and JioCinema both retired into JioHotstar in 2025.
        ("Disney+ Hotstar", 2336),
        ("hotstar", 2336),
        ("JioCinema", 2336),
        ("JioHotstar", 2336),
        ("Zee5", 232),
        ("SonyLIV", 237),
        ("watch on netflix india", 8),
        ("Hulu", None),
        ("", None),
    ],
)
def test_resolve_provider(value, expected):
    assert tmdb.resolve_provider(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Thriller", 53),
        ("thriller", 53),
        ("53", 53),
        ("Sci-Fi", 878),
        ("Science Fiction", 878),
        ("Comedy", 35),
        ("", None),
    ],
)
def test_resolve_genre(value, expected):
    assert tmdb.resolve_genre(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("Tamil", "ta"), ("hindi", "hi"), ("ml", "ml"), ("Klingon", ""), ("", "")],
)
def test_resolve_language(value, expected):
    assert tmdb.resolve_language(value) == expected


@pytest.mark.parametrize(
    "minutes,expected",
    [(141, "2h 21m"), (120, "2h"), (45, "45m"), (0, "Unknown")],
)
def test_format_runtime(minutes, expected):
    assert tmdb.format_runtime(minutes) == expected


# --- Tool validation (returns errors, never raises) ------------------------


def test_unknown_provider_returns_error():
    result = tmdb.fetch_ott_movies(provider="Hulu")
    assert result["status"] == "error"
    assert "Hulu" in result["error_message"]


def test_unknown_genre_returns_error(monkeypatch):
    # A genre outside the static table falls through to the live list; stub it
    # so the test stays offline.
    monkeypatch.setattr(tmdb, "_live_genre_map", lambda: {})
    result = tmdb.fetch_ott_movies(provider="Netflix", genre="Wuxia")
    assert result["status"] == "error"
    assert "Wuxia" in result["error_message"]


def test_empty_search_returns_error():
    assert tmdb.search_movies("  ")["status"] == "error"


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    result = tmdb.fetch_ott_movies(provider="Netflix")
    assert result["status"] == "error"
    assert "TMDB_API_KEY" in result["error_message"]


def test_list_ott_providers():
    result = tmdb.list_ott_providers()
    assert result["status"] == "success"
    assert {"name": "Netflix", "provider_id": 8} in result["providers"]


# --- TMDB response parsing -------------------------------------------------


def test_certification_prefers_india():
    payload = {
        "results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "R"}]},
            {"iso_3166_1": "IN", "release_dates": [{"certification": "UA"}]},
        ]
    }
    assert tmdb._certification(payload) == "UA"


def test_certification_falls_back_to_us():
    payload = {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]}]}
    assert tmdb._certification(payload) == "PG-13 (US)"


def test_certification_when_absent():
    assert tmdb._certification({"results": []}) == "Not rated"


def test_india_streaming_dedupes_across_tiers():
    payload = {
        "results": {
            "IN": {
                "flatrate": [{"provider_name": "Netflix"}],
                "free": [{"provider_name": "JioCinema"}],
                "ads": [{"provider_name": "Netflix"}],
            }
        }
    }
    assert tmdb._india_streaming(payload) == ["Netflix", "JioCinema"]


def test_summarise_maps_genre_ids_to_names():
    summary = tmdb._summarise(
        {
            "id": 1,
            "title": "Example",
            "release_date": "2024-06-14",
            "vote_average": 7.84,
            "genre_ids": [53, 80],
            "overview": "…",
        }
    )
    assert summary["release_year"] == "2024"
    assert summary["rating"] == 7.8
    assert summary["genres"] == ["Thriller", "Crime"]


# --- Request construction --------------------------------------------------


class _FakeResponse:
    status_code = 200
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def captured_request(monkeypatch):
    """Intercept the outgoing TMDB call and expose the params it was built with."""
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        return _FakeResponse({"results": [], "total_results": 0})

    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.requests, "get", fake_get)
    return calls


def test_discover_query_is_built_correctly(captured_request):
    tmdb.fetch_ott_movies(
        provider="Disney+ Hotstar", genre="Comedy", release_year=2024,
        language="Hindi", min_rating=7.0,
    )
    params = captured_request["params"]

    assert captured_request["url"].endswith("/discover/movie")
    assert params["with_watch_providers"] == 2336
    assert params["watch_region"] == "IN"
    assert params["with_genres"] == 35
    assert params["primary_release_year"] == 2024
    assert params["with_original_language"] == "hi"
    assert params["vote_average.gte"] == 7.0
    assert params["vote_count.gte"] == 50
    assert params["sort_by"] == "popularity.desc"
    assert params["api_key"] == "test-key"


def test_unset_filters_are_omitted_from_the_query(captured_request):
    tmdb.fetch_ott_movies(provider="Netflix")
    params = captured_request["params"]

    assert params["with_watch_providers"] == 8
    for dropped in (
        "with_genres", "primary_release_year", "with_original_language", "vote_average.gte"
    ):
        assert dropped not in params


def test_details_request_appends_credits_and_providers(captured_request):
    result = tmdb.fetch_movie_details("109123")
    assert captured_request["url"].endswith("/movie/109123")
    assert captured_request["params"]["append_to_response"] == (
        "credits,release_dates,watch/providers"
    )
    # A sparse body must still come back as a well-formed result, not raise.
    assert result["status"] == "success"
    assert result["tmdb_id"] == 109123
    assert result["runtime"] == "Unknown"
    assert result["director"] == "Unknown"


def test_blocked_primary_host_fails_over_to_alias(monkeypatch):
    attempted = []

    def flaky_get(url, params=None, timeout=None):
        attempted.append(url)
        if url.startswith("https://api.themoviedb.org"):
            raise tmdb.requests.ConnectionError("connection reset by peer")
        return _FakeResponse({"results": [], "total_results": 0})

    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.requests, "get", flaky_get)

    assert tmdb.fetch_ott_movies(provider="Netflix")["status"] == "success"
    assert len(attempted) == 2
    assert attempted[1].startswith("https://api.tmdb.org")


def test_working_host_is_reused_without_reprobing(monkeypatch):
    attempted = []

    def flaky_get(url, params=None, timeout=None):
        attempted.append(url)
        if url.startswith("https://api.themoviedb.org"):
            raise tmdb.requests.ConnectionError("connection reset by peer")
        return _FakeResponse({"results": [], "total_results": 0})

    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.requests, "get", flaky_get)

    tmdb.fetch_ott_movies(provider="Netflix")
    tmdb.fetch_ott_movies(provider="Zee5")

    # Second call must go straight to the alias, not retry the blocked host.
    assert attempted[2:] == ["https://api.tmdb.org/3/discover/movie"]
    assert tmdb.active_host() == "https://api.tmdb.org/3"


def test_all_hosts_unreachable_reports_error(monkeypatch):
    def always_fails(url, params=None, timeout=None):
        raise tmdb.requests.ConnectionError("connection reset by peer")

    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.requests, "get", always_fails)

    result = tmdb.fetch_ott_movies(provider="Netflix")
    assert result["status"] == "error"
    assert "Could not reach TMDB" in result["error_message"]


def test_bad_key_does_not_retry_the_other_host(monkeypatch):
    attempted = []

    class Unauthorized:
        status_code = 401
        ok = False
        text = "invalid api key"

        def json(self):
            return {}

    def unauthorized_get(url, params=None, timeout=None):
        attempted.append(url)
        return Unauthorized()

    monkeypatch.setattr(config, "TMDB_API_KEY", "wrong-key")
    monkeypatch.setattr(tmdb.requests, "get", unauthorized_get)

    result = tmdb.fetch_ott_movies(provider="Netflix")
    assert result["status"] == "error"
    assert "401" in result["error_message"]
    assert len(attempted) == 1


def test_empty_discover_result_is_success_not_error(captured_request):
    result = tmdb.fetch_ott_movies(provider="Zee5", genre="Western")
    assert result["status"] == "success"
    assert result["movies"] == []
    assert "message" in result


# --- Journal helpers -------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-07-28", "2026-07-28"),
        ("28-07-2026", "2026-07-28"),
        ("28/07/2026", "2026-07-28"),
        ("28 July 2026", "2026-07-28"),
    ],
)
def test_normalise_date(value, expected):
    assert journal._normalise_date(value) == expected


def test_normalise_date_defaults_to_today():
    assert journal._normalise_date("") == datetime.date.today().isoformat()


def test_normalise_date_rejects_garbage():
    with pytest.raises(ValueError):
        journal._normalise_date("last tuesday-ish")


def test_log_id_format():
    log_id = journal._new_log_id()
    assert log_id.startswith("LOG-")
    assert len(log_id) == 12
    assert journal._new_log_id() != log_id


def test_add_to_journal_rejects_bad_rating():
    result = journal.add_to_journal(title="Maharaja", rating=9.0)
    assert result["status"] == "error"
    assert "between 1.0 and 5.0" in result["error_message"]


def test_add_to_journal_requires_title():
    assert journal.add_to_journal(title=" ")["status"] == "error"


def test_shared_status_column_matches_schema():
    # Shared_Status is column I in the PRD schema.
    assert journal.SHARED_STATUS_COLUMN == 9
    assert config.JOURNAL_HEADERS[8] == "Shared_Status"


def test_sort_newest_first_breaks_ties_by_row():
    rows = [
        {"Watch_Date": "2026-07-20", "_row": 2},
        {"Watch_Date": "2026-07-28", "_row": 3},
        {"Watch_Date": "2026-07-28", "_row": 4},
    ]
    assert [r["_row"] for r in journal._sort_newest_first(rows)] == [4, 3, 2]


def test_to_entry_coerces_types():
    entry = journal._to_entry(
        {
            "Log_ID": "LOG-9821",
            "Watch_Date": "2026-07-28",
            "Movie_Title": "Maharaja",
            "TMDB_ID": 109123,
            "OTT_Platform": "Netflix",
            "Genre": "Action, Thriller",
            "User_Rating": "4.5",
            "User_Review": "Brilliant screenplay and twist!",
            "Shared_Status": "TRUE",
        }
    )
    assert entry["rating"] == 4.5
    assert entry["shared"] is True


def test_to_entry_survives_blank_rating():
    assert journal._to_entry({"User_Rating": ""})["rating"] == 0.0


def test_format_card_matches_prd_layout():
    card = journal._format_card(
        [
            {
                "title": "Stree 2",
                "platform": "Disney+ Hotstar",
                "rating": 4.0,
                "review": "Super funny, great performance by Rajkummar!",
            },
            {"title": "Maharaja", "platform": "Netflix", "rating": 4.5, "review": ""},
        ]
    )
    assert card.splitlines() == [
        "🍿 My Recent Movie Logs:",
        "1. Stree 2 (Disney+ Hotstar) - ⭐️ 4.0/5",
        '   "Super funny, great performance by Rajkummar!"',
        "2. Maharaja (Netflix) - ⭐️ 4.5/5",
    ]
