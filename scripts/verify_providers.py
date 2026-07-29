"""Check config.PROVIDER_NAMES against TMDB's live Indian provider list.

Provider catalogues change — JioCinema folded into Disney+ Hotstar during 2025 —
so run this whenever discovery results for a platform look wrong.

    uv run python scripts/verify_providers.py
"""

from __future__ import annotations

import sys

from movie_connoisseur import config
from movie_connoisseur.tools.tmdb import TMDBError, _get

# Provider display names can contain non-ASCII characters that a cp1252 console
# cannot encode.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    try:
        payload = _get("/watch/providers/movie", watch_region=config.WATCH_REGION)
    except TMDBError as exc:
        print(f"FAILED: {exc}")
        return 1

    live = {
        int(p["provider_id"]): p.get("provider_name", "?")
        for p in payload.get("results", [])
    }
    print(f"TMDB lists {len(live)} movie providers for region {config.WATCH_REGION}.\n")

    print("Configured providers:")
    stale = []
    for pid, name in sorted(config.PROVIDER_NAMES.items(), key=lambda kv: kv[1]):
        if pid in live:
            match = "ok " if live[pid].lower() == name.lower() else "renamed"
            print(f"  [{match:>7}] {pid:>5}  {name}  ->  {live[pid]}")
        else:
            stale.append((pid, name))
            print(f"  [ MISSING] {pid:>5}  {name}  ->  not offered in India")

    if stale:
        print(
            "\nThese IDs are no longer in TMDB's India list and will return no "
            "results. Remove them from config.OTT_PROVIDERS / PROVIDER_NAMES:"
        )
        for pid, name in stale:
            print(f"  - {name} ({pid})")

    print("\nOther major providers available in India:")
    configured = set(config.PROVIDER_NAMES)
    for pid, name in sorted(live.items(), key=lambda kv: kv[1]):
        if pid not in configured:
            print(f"  {pid:>5}  {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
