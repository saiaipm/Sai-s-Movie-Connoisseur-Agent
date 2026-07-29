"""Tool functions exposed to the ADK agents."""

from movie_connoisseur.tools.journal import (
    add_to_journal,
    add_to_watchlist,
    generate_shareable_summary,
    get_journal_history,
    get_watchlist,
    remove_from_watchlist,
)
from movie_connoisseur.tools.tmdb import (
    fetch_movie_credits,
    fetch_movie_details,
    fetch_ott_movies,
    list_ott_providers,
    search_movies,
)

__all__ = [
    "add_to_journal",
    "add_to_watchlist",
    "fetch_movie_credits",
    "fetch_movie_details",
    "fetch_ott_movies",
    "generate_shareable_summary",
    "get_journal_history",
    "get_watchlist",
    "list_ott_providers",
    "remove_from_watchlist",
    "search_movies",
]
