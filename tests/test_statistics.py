"""Tests for the journal dashboard statistics."""

from __future__ import annotations

from movie_connoisseur.tools.journal import summarise_entries


def entry(**kwargs):
    base = {
        "title": "X",
        "genre": "",
        "platform": "",
        "watch_date": "2026-07-01",
        "rating": 0,
        "imdb_rating": "",
        "media_type": "movie",
    }
    base.update(kwargs)
    return base


# --- Genres -----------------------------------------------------------------


def test_a_title_counts_towards_each_of_its_genres():
    stats = summarise_entries(
        [
            entry(genre="Action, Science Fiction, Adventure"),
            entry(genre="Action, Thriller"),
        ]
    )
    counts = dict(stats["genres"])
    assert counts["Action"] == 2
    assert counts["Thriller"] == 1
    assert counts["Science Fiction"] == 1


def test_genres_are_ordered_by_frequency():
    stats = summarise_entries(
        [entry(genre="Drama"), entry(genre="Drama"), entry(genre="Comedy")]
    )
    assert stats["genres"][0] == ("Drama", 2)


def test_blank_and_whitespace_genres_are_ignored():
    stats = summarise_entries([entry(genre=" , Drama ,, "), entry(genre="")])
    assert stats["genres"] == [("Drama", 1)]


# --- Platforms --------------------------------------------------------------


def test_platform_counts():
    stats = summarise_entries(
        [entry(platform="Netflix"), entry(platform="Netflix"), entry(platform="Zee5")]
    )
    assert stats["platforms"][0] == ("Netflix", 2)


def test_missing_platform_is_not_counted():
    stats = summarise_entries([entry(platform=""), entry(platform="Netflix")])
    assert stats["platforms"] == [("Netflix", 1)]


# --- You vs critics ---------------------------------------------------------


def test_user_rating_is_scaled_to_ten_before_comparing():
    """A 4/5 is an 8/10. Comparing 4 against 8.2 directly would be nonsense."""
    stats = summarise_entries([entry(rating=4.0, imdb_rating=8.2)])
    taste = stats["taste"]
    assert taste["yours"] == 8.0
    assert taste["critics"] == 8.2
    assert taste["delta"] == -0.2


def test_generous_marker_shows_a_positive_delta():
    stats = summarise_entries(
        [entry(rating=5.0, imdb_rating=7.0), entry(rating=5.0, imdb_rating=8.0)]
    )
    assert stats["taste"]["yours"] == 10.0
    assert stats["taste"]["delta"] == 2.5


def test_only_titles_with_both_scores_are_compared():
    stats = summarise_entries(
        [
            entry(rating=4.0, imdb_rating=8.0),
            entry(rating=0, imdb_rating=9.0),  # unrated by the user
            entry(rating=5.0, imdb_rating=""),  # no IMDb score
        ]
    )
    assert stats["taste"]["sample"] == 1


def test_no_comparison_without_any_rated_titles():
    stats = summarise_entries([entry(rating=0, imdb_rating=8.0)])
    assert stats["taste"] is None


def test_unparseable_ratings_do_not_crash():
    stats = summarise_entries([entry(rating="N/A", imdb_rating="unknown")])
    assert stats["taste"] is None


# --- Cadence and media type -------------------------------------------------


def test_months_are_grouped_and_sorted():
    stats = summarise_entries(
        [
            entry(watch_date="2026-07-29"),
            entry(watch_date="2026-07-01"),
            entry(watch_date="2026-06-15"),
        ]
    )
    assert stats["months"] == [("2026-06", 1), ("2026-07", 2)]


def test_malformed_dates_are_skipped():
    stats = summarise_entries([entry(watch_date=""), entry(watch_date="2026-07-01")])
    assert stats["months"] == [("2026-07", 1)]


def test_media_types_are_counted():
    stats = summarise_entries(
        [entry(media_type="tv"), entry(media_type="movie"), entry(media_type="")]
    )
    # A blank media type means a row written before television was supported;
    # those are films.
    assert stats["media_types"] == {"tv": 1, "movie": 2}


def test_empty_journal_is_safe():
    stats = summarise_entries([])
    assert stats["total"] == 0
    assert stats["genres"] == []
    assert stats["taste"] is None


# --- Platform normalisation -------------------------------------------------


def test_platform_aliases_collapse_to_one_name():
    """"Prime Video" and "Amazon Prime Video" are one service.

    Stored verbatim they became two bars in the platform breakdown.
    """
    from movie_connoisseur.tools.tmdb import canonical_platform

    assert canonical_platform("Prime Video") == "Amazon Prime Video"
    assert canonical_platform("Amazon Prime Video") == "Amazon Prime Video"
    assert canonical_platform("prime") == "Amazon Prime Video"
    # The retired names still resolve to the merged service.
    assert canonical_platform("Hotstar") == "JioHotstar"
    assert canonical_platform("JioCinema") == "JioHotstar"


def test_unknown_platform_is_kept_as_typed():
    # A service we do not know about is still the user's answer.
    from movie_connoisseur.tools.tmdb import canonical_platform

    assert canonical_platform("Mubi India") == "Mubi India"
    assert canonical_platform("") == ""


def test_aliases_are_counted_together_in_the_breakdown():
    stats = summarise_entries(
        [
            entry(platform="Amazon Prime Video"),
            entry(platform="Amazon Prime Video"),
            entry(platform="Netflix"),
        ]
    )
    assert stats["platforms"][0] == ("Amazon Prime Video", 2)
