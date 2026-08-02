"""Offline tests for the OMDb critic-ratings tool."""

from __future__ import annotations

import pytest
import requests

from movie_connoisseur import config
from movie_connoisseur.tools import omdb

# A trimmed real response shape. Note Metascore "N/A": absent for roughly
# two-thirds of Indian releases, so it is the normal case, not an error.
INCEPTION = {
    "Response": "True",
    "Title": "Inception",
    "imdbID": "tt1375666",
    "imdbRating": "8.8",
    "imdbVotes": "2,600,000",
    "Metascore": "74",
    "Rated": "PG-13",
    "Ratings": [
        {"Source": "Internet Movie Database", "Value": "8.8/10"},
        {"Source": "Rotten Tomatoes", "Value": "87%"},
        {"Source": "Metacritic", "Value": "74/100"},
    ],
}

TUMBBAD = {
    "Response": "True",
    "Title": "Tumbbad",
    "Year": "2018",
    "Language": "Hindi, Marathi",
    "Country": "India",
    "imdbID": "tt8239946",
    "imdbRating": "8.2",
    "Metascore": "N/A",
    "Ratings": [
        {"Source": "Internet Movie Database", "Value": "8.2/10"},
        {"Source": "Rotten Tomatoes", "Value": "87%"},
    ],
}


class FakeResponse:
    status_code = 200
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def omdb_returns(monkeypatch):
    """Serve a canned OMDb payload and capture the request params."""

    def _install(payload, status_code=200):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            response = FakeResponse(payload)
            response.status_code = status_code
            response.ok = status_code == 200
            return response

        monkeypatch.setattr(config, "OMDB_API_KEY", "test-key")
        monkeypatch.setattr(omdb.requests, "get", fake_get)
        omdb.reset_cache()
        return captured

    return _install


# --- Parsing ----------------------------------------------------------------


def test_parses_all_three_ratings(omdb_returns):
    omdb_returns(INCEPTION)
    result = omdb.fetch_external_ratings(imdb_id="tt1375666")

    assert result["status"] == "success"
    assert result["imdb_rating"] == 8.8
    assert result["rt_rating"] == 87
    assert result["metacritic"] == 74


def test_ratings_keep_native_scales(omdb_returns):
    # IMDb out of 10, RT and Metacritic out of 100. Flattening them would imply
    # a comparability that does not exist.
    omdb_returns(INCEPTION)
    result = omdb.fetch_external_ratings(imdb_id="tt1375666")
    assert 0 <= result["imdb_rating"] <= 10
    assert 0 <= result["rt_rating"] <= 100
    assert 0 <= result["metacritic"] <= 100


def test_ratings_are_numbers_not_strings(omdb_returns):
    # Strings would sort alphabetically in the spreadsheet: "100" before "87".
    omdb_returns(INCEPTION)
    result = omdb.fetch_external_ratings(imdb_id="tt1375666")
    for key in ("imdb_rating", "rt_rating", "metacritic"):
        assert isinstance(result[key], (int, float))


def test_missing_metacritic_becomes_blank_not_the_string_na(omdb_returns):
    omdb_returns(TUMBBAD)
    result = omdb.fetch_external_ratings(imdb_id="tt8239946")

    assert result["status"] == "success"
    assert result["imdb_rating"] == 8.2
    assert result["rt_rating"] == 87
    # The literal "N/A" would sort among the numbers and read as a real value.
    assert result["metacritic"] == ""


def test_absent_rotten_tomatoes_entry_is_blank(omdb_returns):
    payload = {**TUMBBAD, "Ratings": [{"Source": "Internet Movie Database", "Value": "8.3/10"}]}
    omdb_returns(payload)
    assert omdb.fetch_external_ratings(imdb_id="tt0000001")["rt_rating"] == ""


@pytest.mark.parametrize(
    "raw,expected",
    [("8.8", 8.8), ("87%", 87), ("74/100", 74), ("N/A", ""), ("", ""), ("nonsense", "")],
)
def test_number_parsing(raw, expected):
    assert omdb._to_number(raw) == expected


# --- Request shape ----------------------------------------------------------


def test_imdb_id_is_preferred_over_title(omdb_returns):
    captured = omdb_returns(INCEPTION)
    omdb.fetch_external_ratings(imdb_id="tt1375666", title="Inception")
    assert captured["params"]["i"] == "tt1375666"
    assert "t" not in captured["params"]


def test_falls_back_to_title_without_an_id(omdb_returns):
    captured = omdb_returns(INCEPTION)
    omdb.fetch_external_ratings(title="Inception")
    assert captured["params"]["t"] == "Inception"
    assert "i" not in captured["params"]


# --- Failure handling -------------------------------------------------------


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.setattr(config, "OMDB_API_KEY", "")
    result = omdb.fetch_external_ratings(imdb_id="tt1375666")
    assert result["status"] == "error"
    assert "OMDB_API_KEY" in result["error_message"]


def test_no_identifier_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "OMDB_API_KEY", "test-key")
    assert omdb.fetch_external_ratings()["status"] == "error"


def test_film_not_in_omdb_is_an_error_not_a_crash(omdb_returns):
    omdb_returns({"Response": "False", "Error": "Movie not found!"})
    result = omdb.fetch_external_ratings(imdb_id="tt0000000")
    assert result["status"] == "error"
    assert "not found" in result["error_message"].lower()


def test_bad_key_is_explained(omdb_returns):
    omdb_returns({}, status_code=401)
    result = omdb.fetch_external_ratings(imdb_id="tt1375666")
    assert result["status"] == "error"
    assert "OMDB_API_KEY" in result["error_message"]


def test_network_failure_is_reported(monkeypatch):
    monkeypatch.setattr(config, "OMDB_API_KEY", "test-key")

    def boom(*_a, **_k):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(omdb.requests, "get", boom)
    omdb.reset_cache()

    result = omdb.fetch_external_ratings(imdb_id="tt1375666")
    assert result["status"] == "error"
    assert "Could not reach OMDb" in result["error_message"]


# --- ratings_or_blank: enrichment must never break a write ------------------


def test_ratings_or_blank_returns_blanks_on_failure(monkeypatch):
    monkeypatch.setattr(config, "OMDB_API_KEY", "")
    assert omdb.ratings_or_blank(imdb_id="tt1375666") == {
        "imdb_rating": "",
        "rt_rating": "",
        "metacritic": "",
    }


def test_ratings_or_blank_survives_an_unexpected_exception(monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("something unforeseen")

    monkeypatch.setattr(omdb, "fetch_external_ratings", boom)
    result = omdb.ratings_or_blank(imdb_id="tt1375666")
    assert result == {"imdb_rating": "", "rt_rating": "", "metacritic": ""}


def test_year_and_language_are_returned(omdb_returns):
    """Grounding, not decoration.

    Without these the model supplied them from memory and got both wrong,
    describing Tumbbad as a 2015 Telugu film rather than a 2018 Hindi/Marathi
    one.
    """
    omdb_returns(TUMBBAD)
    result = omdb.fetch_external_ratings(imdb_id="tt8239946")

    assert result["year"] == "2018"
    assert result["language"] == "Hindi, Marathi"
    assert result["country"] == "India"


def test_the_critic_agent_can_fetch_critic_scores():
    """The Critic answers "how was it received?", so it needs this tool."""
    from movie_connoisseur import agents

    names = {t.__name__ for t in agents.critic_agent.tools}
    assert "fetch_external_ratings" in names


def test_only_the_critic_gets_it():
    """Discovery returns lists — a rating call per result would be wasteful."""
    from movie_connoisseur import agents

    for agent in agents.coordinator_agent.sub_agents:
        if agent.name == "critic_agent":
            continue
        assert "fetch_external_ratings" not in {t.__name__ for t in agent.tools}


def test_ratings_or_blank_passes_values_through(omdb_returns):
    omdb_returns(INCEPTION)
    assert omdb.ratings_or_blank(imdb_id="tt1375666") == {
        "imdb_rating": 8.8,
        "rt_rating": 87,
        "metacritic": 74,
    }


# --- Silent degradation without a key ---------------------------------------


def test_missing_key_leaves_ratings_blank_without_failing_the_write(monkeypatch):
    """The deployment bug this exists to catch.

    OMDB_API_KEY was absent from the deployed secrets, so anything logged
    there got a populated IMDb_ID (from TMDB) but three empty rating columns —
    and nothing anywhere said why.
    """
    from movie_connoisseur.tools import journal, tmdb

    monkeypatch.setattr(config, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        tmdb,
        "fetch_title_details",
        lambda *_a, **_k: {
            "status": "success",
            "title": "Ted Lasso",
            "tmdb_id": 97546,
            "imdb_id": "tt10986410",
            "media_type": "tv",
            "seasons": 4,
            "genres": ["Comedy"],
            "streaming_in_india": ["Apple TV"],
            "rating": 8.3,
            "overview": "A coach.",
        },
    )

    meta = journal._movie_metadata("Ted Lasso")

    # Enrichment still succeeds and the row is still worth writing...
    assert meta["title"] == "Ted Lasso"
    assert meta["imdb_id"] == "tt10986410"
    assert meta["media_type"] == "tv"
    # ...but the OMDb-sourced columns are empty, which is the symptom to
    # recognise: an imdb_id with no imdb_rating means the key is missing.
    assert meta["imdb_rating"] == ""
    assert meta["rt_rating"] == ""
    assert meta["metacritic"] == ""


def test_a_series_with_only_an_imdb_score_is_not_an_error(omdb_returns):
    """Rotten Tomatoes and Metacritic are frequently absent for television.

    Ted Lasso really does have only an IMDb score in OMDb — blank RT and
    Metacritic there are correct, not a failure.
    """
    omdb_returns(
        {
            "Response": "True",
            "Title": "Ted Lasso",
            "Type": "series",
            "imdbID": "tt10986410",
            "imdbRating": "8.7",
            "Metascore": "N/A",
            "Ratings": [{"Source": "Internet Movie Database", "Value": "8.7/10"}],
        }
    )
    result = omdb.fetch_external_ratings(imdb_id="tt10986410")

    assert result["status"] == "success"
    assert result["imdb_rating"] == 8.7
    assert result["rt_rating"] == ""
    assert result["metacritic"] == ""
