"""Header handling must tolerate a spreadsheet that a newer version has widened.

One spreadsheet is shared by whatever version happens to be running. A branch
that adds columns must not have its headers stripped off the moment an older
version reads the same sheet.
"""

from __future__ import annotations

import pytest

from movie_connoisseur import config
from movie_connoisseur.tools import journal

JOURNAL = list(config.JOURNAL_HEADERS)


class FakeWorksheet:
    def __init__(self, header_row, col_count=None):
        self._header_row = list(header_row)
        self.col_count = col_count if col_count is not None else len(header_row)
        self.updates = []

    def row_values(self, _row):
        return list(self._header_row)

    def update(self, values, range_name=None):
        self.updates.append({"values": values, "range": range_name})
        self._header_row = list(values[0])

    def add_cols(self, n):
        self.col_count += n


@pytest.fixture
def open_with():
    """Call the real _reconcile_headers against a fake worksheet.

    Deliberately not a reimplementation of the rule: a copy in the test would
    keep passing while production drifted away from it.
    """

    def _run(sheet, headers):
        journal._reconcile_headers(sheet, list(headers))
        return sheet

    return _run


@pytest.mark.parametrize(
    "index,expected", [(1, "A"), (9, "I"), (15, "O"), (26, "Z"), (27, "AA"), (52, "AZ")]
)
def test_column_letter(index, expected):
    # chr(ord('A') + n) silently produces junk past column Z.
    assert journal._column_letter(index) == expected


def test_matching_headers_are_left_alone(open_with):
    sheet = FakeWorksheet(JOURNAL)
    open_with(sheet, JOURNAL)
    assert sheet.updates == []


def test_missing_headers_are_written(open_with):
    sheet = FakeWorksheet([])
    open_with(sheet, JOURNAL)
    assert sheet.updates
    assert sheet.updates[0]["values"] == [JOURNAL]


def test_extra_trailing_columns_are_preserved(open_with):
    """The reason this exists.

    A feature branch appends rating columns to the shared sheet. This version
    knows nothing about them, but must not delete their headers.
    """
    widened = JOURNAL + ["IMDb_ID", "TMDB_Rating", "IMDb_Rating", "RT_Rating"]
    sheet = FakeWorksheet(widened)

    open_with(sheet, JOURNAL)

    assert sheet.updates == [], "extra columns were clobbered"
    assert sheet.row_values(1) == widened


def test_wrong_headers_within_our_range_are_still_corrected(open_with):
    sheet = FakeWorksheet(["Wrong"] + JOURNAL[1:] + ["Extra_Column"])
    open_with(sheet, JOURNAL)
    assert sheet.updates, "a genuinely wrong header should be repaired"
    # Only our own range is rewritten.
    assert sheet.updates[0]["range"] == f"A1:{journal._column_letter(len(JOURNAL))}1"


def test_narrow_grid_is_widened_before_writing(open_with):
    sheet = FakeWorksheet([], col_count=2)
    open_with(sheet, JOURNAL)
    # Writing past the last column fails with "exceeds grid limits" otherwise.
    assert sheet.col_count >= len(JOURNAL)
