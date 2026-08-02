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


def test_ratings_or_blank_passes_values_through(omdb_returns):
    omdb_returns(INCEPTION)
    assert omdb.ratings_or_blank(imdb_id="tt1375666") == {
        "imdb_rating": 8.8,
        "rt_rating": 87,
        "metacritic": 74,
    }
