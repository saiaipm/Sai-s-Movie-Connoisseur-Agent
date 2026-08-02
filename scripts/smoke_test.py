"""Phase 1 acceptance check: exercise every tool against the live APIs.

Needs real credentials in .env. The journal section writes one throwaway row to
the sheet and leaves it there so you can eyeball the formatting.

    uv run python scripts/smoke_test.py            # TMDB only
    uv run python scripts/smoke_test.py --sheets   # TMDB + Google Sheets
"""

from __future__ import annotations

import argparse
import json
import sys

from movie_connoisseur import config
from movie_connoisseur.tools import journal, tmdb

# Windows consoles default to cp1252, which cannot encode the emoji in the
# shareable card.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASSED = 0
FAILED = 0


def check(label: str, result: dict, *, expect_rows: str = "") -> dict:
    """Print a tool result and record pass/fail."""
    global PASSED, FAILED
    ok = result.get("status") == "success"
    if expect_rows and ok and not result.get(expect_rows):
        ok = False
        result = {**result, "note": f"'{expect_rows}' came back empty"}

    print(f"\n{'PASS' if ok else 'FAIL'}  {label}")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:1200])

    if ok:
        PASSED += 1
    else:
        FAILED += 1
    return result


def test_tmdb() -> None:
    print("=" * 70)
    print("TMDB TOOLS")
    print("=" * 70)

    # Workflow A from the PRD.
    check(
        'fetch_ott_titles(provider="Netflix", genre="Thriller")',
        tmdb.fetch_ott_titles(provider="Netflix", genre="Thriller", limit=5),
        expect_rows="movies",
    )
    check(
        'fetch_ott_titles(provider="8", genre="53") — PRD ID form',
        tmdb.fetch_ott_titles(provider="8", genre="53", limit=3),
        expect_rows="movies",
    )
    check(
        'fetch_ott_titles(language="Tamil", min_rating=7.0)',
        tmdb.fetch_ott_titles(language="Tamil", min_rating=7.0, limit=3),
        expect_rows="movies",
    )
    check(
        'search_titles("Maharaja")',
        tmdb.search_titles("Maharaja", limit=3),
        expect_rows="movies",
    )
    details = check(
        'fetch_title_details("Stree 2")',
        tmdb.fetch_title_details("Stree 2"),
    )
    if details.get("status") == "success":
        check(
            f"fetch_credits({details['tmdb_id']})",
            tmdb.fetch_credits(details["tmdb_id"], cast_limit=5),
            expect_rows="cast",
        )

    # Error paths should degrade politely, not raise.
    bad = tmdb.fetch_ott_titles(provider="Hulu")
    print(f"\n{'PASS' if bad['status'] == 'error' else 'FAIL'}  unknown provider is rejected")
    print(f"  {bad.get('error_message', '')[:160]}")
    globals()['PASSED' if bad['status'] == 'error' else 'FAILED'] += 1


def test_sheets() -> None:
    print("\n" + "=" * 70)
    print("GOOGLE SHEETS TOOLS")
    print("=" * 70)

    check(
        "add_to_journal('Stree 2', 'Disney+ Hotstar', 4.0, ...)",
        journal.add_to_journal(
            title="Stree 2",
            platform="Disney+ Hotstar",
            rating=4.0,
            review="Smoke test row — safe to delete.",
        ),
    )
    check(
        "get_journal_history(limit=5)",
        journal.get_journal_history(limit=5),
        expect_rows="entries",
    )
    summary = check(
        "generate_shareable_summary(limit=3)",
        journal.generate_shareable_summary(limit=3),
    )
    if summary.get("status") == "success":
        print("\n--- shareable card ---")
        print(summary["summary"])
        print("----------------------")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sheets", action="store_true", help="also test Google Sheets (writes a row)"
    )
    args = parser.parse_args()

    if not config.TMDB_API_KEY:
        print("TMDB_API_KEY is not set — copy .env.example to .env and fill it in.")
        return 1

    test_tmdb()

    if args.sheets:
        if not config.SPREADSHEET_KEY:
            print("\nSPREADSHEET_KEY is not set — skipping the Google Sheets checks.")
        else:
            test_sheets()
    else:
        print("\n(Skipping Google Sheets. Re-run with --sheets to include it.)")

    print(f"\n{'=' * 70}\n{PASSED} passed, {FAILED} failed\n{'=' * 70}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
