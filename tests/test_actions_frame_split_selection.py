"""
``actions_frame`` selects splits on ``SplitRatio``, not on the row's ``Action`` label.

A composite NSE subject is a dividend **and** a split on one row: RSSOFTWARE's ex-date 2015-01-21
is ``Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share To Rs 5/-
Per Share``, which the parser rightly classifies ``dividend`` (the ``Value`` is the cash amount)
while ``split_ratio`` derives a real ``2.0``. yfinance publishes that ``2.0`` for the same date, so
a selection filtered by ``Action`` silently drops a factor that ``splits_from_actions`` reports.
The ratio column is the honest discriminator: a row that declares no split leaves it ``None``.

The negative control below (a plain dividend row) goes red if the condition is widened to "any
row". The blast-radius test pins the exact measurement taken when the condition changed: on the
curated market-wide fixture the frame keeps its 184 rows and gains exactly two non-zero split dates
(RSSOFTWARE 2015-01-21 and DWARKESH 2017-08-10, both composite dividend+split rows), taking the
count of non-zero ``Stock Splits`` dates from 22 to 24.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"

#: Verbatim subject from the curated recording, RSSOFTWARE ex-date 2015-01-21.
COMPOSITE_SUBJECT = (
    "Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share To Rs 5/- "
    "Per Share"
)


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def payload_row(subject: str, ex_date: str, *, symbol: str = "RSSOFTWARE") -> dict:
    """One raw upstream row, shaped exactly like the recorded payloads."""
    return {
        "symbol": symbol,
        "comp": "R S Software (India) Limited",
        "series": "EQ",
        "subject": subject,
        "faceVal": "10",
        "exDate": ex_date,
        "recDate": ex_date,
        "bcStartDate": "-",
        "bcEndDate": "-",
        "ndStartDate": "-",
        "ndEndDate": "-",
        "ind": "-",
        "isin": "INE165B01029",
        "caBroadcastDate": None,
    }


def events(*rows: dict) -> pd.DataFrame:
    """The parsed events frame, built through the real parser from raw rows."""
    return ca.parse_corporate_actions(list(rows), symbol="RSSOFTWARE", url="fixture")


@pytest.fixture
def curated_events() -> pd.DataFrame:
    return ca.parse_corporate_actions(
        load("nse_marketwide_2015_2026_curated.json"), url="fixture"
    )


def test_a_composite_dividend_and_split_row_yields_both_facts():
    """One row, two facts: the cash amount in ``Dividends`` and the factor in ``Stock Splits``."""
    frame = events(payload_row(COMPOSITE_SUBJECT, "21-Jan-2015"))
    assert frame["Action"].tolist() == [ca.DIVIDEND]
    assert frame["Value"].iloc[0] == pytest.approx(1.5)
    assert frame["SplitRatio"].iloc[0] == pytest.approx(2.0)

    actions = ca.actions_frame(frame, symbol="RSSOFTWARE", url="fixture")
    day = pd.Timestamp("2015-01-21", tz="Asia/Kolkata")
    assert actions.loc[day, "Dividends"] == pytest.approx(1.5)
    assert actions.loc[day, "Stock Splits"] == pytest.approx(2.0)


def test_a_plain_dividend_row_still_yields_no_split():
    """Negative control.

    Red if the condition becomes "any row": a dividend with no ``SplitRatio`` must contribute a
    zero to ``Stock Splits``. Deriving a factor from the rupee ``Value`` would rescale every prior
    bar by 6.5x.
    """
    frame = events(payload_row("Dividend - Rs 6.50 Per Share", "02-Jul-2020"))
    assert frame["SplitRatio"].isna().all()

    actions = ca.actions_frame(frame, symbol="RSSOFTWARE", url="fixture")
    day = pd.Timestamp("2020-07-02", tz="Asia/Kolkata")
    assert actions.loc[day, "Dividends"] == pytest.approx(6.5)
    assert actions.loc[day, "Stock Splits"] == 0.0


def test_an_undated_split_row_is_still_excluded():
    """A ``NaT`` has no place in a time series, whichever branch selected it."""
    frame = events(payload_row("Bonus 1:1", "-"))
    assert len(frame) == 1
    assert frame["SplitRatio"].notna().all()

    actions = ca.actions_frame(frame, symbol="RSSOFTWARE", url="fixture")
    assert actions.empty


def test_the_curated_rssoftware_row_now_publishes_its_factor(curated_events):
    """The recorded row yfinance answers 2.0 for, and the measured blast radius of the change.

    Before the change the frame had 184 rows and 22 non-zero ``Stock Splits`` dates; after, it has
    the same 184 rows and 24 non-zero dates. The two additions are exactly the composite rows in
    the fixture — RSSOFTWARE 2015-01-21 (2.0) and DWARKESH 2017-08-10 (10.0) — both classified
    ``dividend`` while carrying a real ratio. No other row or date changes.
    """
    actions = ca.actions_frame(curated_events, url="fixture")
    assert len(actions) == 184

    rssoftware = pd.Timestamp("2015-01-21", tz="Asia/Kolkata")
    assert actions.loc[rssoftware, "Dividends"] == pytest.approx(1.5)
    assert actions.loc[rssoftware, "Stock Splits"] == pytest.approx(2.0)

    dwarkesh = pd.Timestamp("2017-08-10", tz="Asia/Kolkata")
    assert actions.loc[dwarkesh, "Stock Splits"] == pytest.approx(10.0)

    assert int((actions["Stock Splits"] != 0.0).sum()) == 24

    composite = curated_events[
        curated_events.index.notna() & curated_events["SplitRatio"].notna()
    ]
    composite = composite[composite["Action"] == ca.DIVIDEND]
    assert composite["Symbol"].tolist() == ["RSSOFTWARE", "DWARKESH"]
    assert composite["SplitRatio"].tolist() == [pytest.approx(2.0), pytest.approx(10.0)]


def test_a_frame_with_no_split_ratios_returns_the_empty_float_frame():
    """Nothing to split is a real answer: zero rows, the real columns, real dtypes.

    The input here has no rows and no ``SplitRatio`` column at all, which is the shape a caller
    passes when the feed returned nothing. It must not raise, invent a row, or lose the contract.
    """
    actions = ca.actions_frame(pd.DataFrame(), symbol="RSSOFTWARE", url="fixture")
    assert actions.empty
    assert list(actions.columns) == list(ca.ACTION_COLUMNS)
    assert actions["Dividends"].dtype == float
    assert actions["Stock Splits"].dtype == float
    assert isinstance(actions.index, pd.DatetimeIndex)
    assert str(actions.index.tz) == "Asia/Kolkata"
    assert actions.index.name == "Date"


def test_an_empty_events_frame_returns_the_same_empty_shape():
    empty = ca.parse_corporate_actions([], symbol="RSSOFTWARE", url="fixture")
    actions = ca.actions_frame(empty, symbol="RSSOFTWARE", url="fixture")
    assert actions.empty
    assert list(actions.columns) == list(ca.ACTION_COLUMNS)
    assert actions["Dividends"].dtype == float
    assert actions["Stock Splits"].dtype == float
    assert isinstance(actions.index, pd.DatetimeIndex)
    assert str(actions.index.tz) == "Asia/Kolkata"
    assert actions.index.name == "Date"
