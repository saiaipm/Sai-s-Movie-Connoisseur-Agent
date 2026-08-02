"""Tool functions exposed to the ADK agents."""

from movie_connoisseur.tools.journal import (
    add_to_journal,
    add_to_watchlist,
    rate_journal_entry,
    suggest_from_watchlist,
    generate_shareable_summary,
    get_journal_history,
    get_watchlist,
    remove_from_watchlist,
)
from movie_connoisseur.tools.omdb import fetch_external_ratings
from movie_connoisseur.tools.tmdb import (
    fetch_credits,
    fetch_title_details,
    fetch_ott_titles,
    list_ott_providers,
    search_titles,
)

__all__ = [
    "add_to_journal",
    "add_to_watchlist",
    "rate_journal_entry",
    "suggest_from_watchlist",
    "fetch_external_ratings",
    "fetch_credits",
    "fetch_title_details",
    "fetch_ott_titles",
    "generate_shareable_summary",
    "get_journal_history",
    "get_watchlist",
    "list_ott_providers",
    "remove_from_watchlist",
    "search_titles",
]
