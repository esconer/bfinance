"""
Identity accessors over a parsed corporate-actions frame — pure, offline, and refusing to guess.

Two things are being pinned here, and they are different in kind:

* **``isin`` is published, not derived.** The feed carries an ``isin`` field on every row, and
  :func:`bfinance.nse.corporate_actions.isin_from_actions` only reads it. The recordings under
  ``tests/fixtures/corporate_actions/`` are checked field-by-field, so the claim "the payload has
  it" is evidence in the test rather than prose in a docstring. If upstream ever drops the field,
  the parametrised test fails before a caller can be handed a name-matched or invented identifier.
* **A split factor is a published ratio, not arithmetic on a rupee amount.** ``splits_from_actions``
  reads ``SplitRatio`` and nothing else, so a dividend row cannot become a factor. That is the
  negative control at the bottom of the splits section: it goes red if the helper ever reads
  ``Value``, or derives a factor from a row merely because the row exists.

The archived-index fallback is tested for the opposite failure: it must answer ``None`` for a symbol
it has never seen, with every socket connect armed to fail, and it must not create the archive it is
reading. Its coverage is **archived indices only** — a stock outside every catalogued index has no
ISIN here no matter how many times it is asked, and the tests say so rather than implying upstream
has no identifier for it.
"""

import json
import socket
from pathlib import Path

import pandas as pd
import pytest

from bfinance import universe as U
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"

#: Every recording that contains rows. ``nse_empty_feed.json`` is ``[]`` and is covered separately;
#: a file with no rows cannot testify that a field is present.
RECORDED_PAYLOADS = (
    "nse_reliance_2020_2026.json",
    "nse_itc_2025_demerger.json",
    "nse_marketwide_2015_2026_curated.json",
    "nse_malformed_rows.json",
)

#: Verbatim subject from the curation, ex-date 2015-01-21 (RSSOFTWARE). It is a dividend **and** a
#: 2x face-value sub-division on one row, which is the case that separates "read SplitRatio" from
#: "read Action".
COMPOSITE_SUBJECT = (
    "Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share To Rs 5/- "
    "Per Share"
)


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def payload_row(
    subject: str,
    ex_date: str,
    *,
    isin="INE002A01018",
    symbol: str = "RELIANCE",
) -> dict:
    """One raw upstream row, shaped exactly like the recorded payloads."""
    return {
        "symbol": symbol,
        "comp": "Reliance Industries Limited",
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
        "isin": isin,
        "caBroadcastDate": None,
    }


def events(*rows: dict) -> pd.DataFrame:
    """The parsed events frame, built through the real parser from raw rows."""
    return ca.parse_corporate_actions(list(rows), symbol="RELIANCE", url="fixture")


@pytest.fixture
def reliance_events() -> pd.DataFrame:
    return ca.parse_corporate_actions(
        load("nse_reliance_2020_2026.json"), symbol="RELIANCE", url="fixture"
    )


@pytest.fixture
def itc_events() -> pd.DataFrame:
    return ca.parse_corporate_actions(
        load("nse_itc_2025_demerger.json"), symbol="ITC", url="fixture"
    )


@pytest.fixture
def no_network(monkeypatch):
    """Every socket connection raises. A helper that fetches fails loudly instead of slowly."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"network I/O attempted in an offline test: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    return forbidden


# =============================================================== the payload carries the ISIN


@pytest.mark.parametrize("name", RECORDED_PAYLOADS)
def test_every_recorded_row_has_a_non_empty_isin_field(name):
    """The accessor is only honest if the field exists upstream. It does, on every recorded row.

    This is the guard for plan step 2: if a future payload drops ``isin``, this fails and the
    reader is forced back to "the source has no ISIN" rather than to a name match, a scrip code,
    or a second page. The ``nse_malformed_rows.json`` recording is included on purpose — even its
    rows with no dividend amount and no parseable date carry the identifier.
    """
    rows = load(name)
    assert rows, f"{name} has no rows, so it cannot testify about the payload schema"
    missing = [
        index for index, row in enumerate(rows) if not str(row.get("isin") or "").strip()
    ]
    assert missing == [], f"{name}: rows {missing} carry no 'isin' field"


def test_the_parser_carries_the_payload_value_onto_the_frame(reliance_events):
    """Read, not derived: the frame's ``ISIN`` is the payload's ``isin`` on every row."""
    assert "ISIN" in reliance_events.columns
    assert set(reliance_events["ISIN"]) == {"INE002A01018"}
    assert reliance_events.loc[pd.Timestamp("2024-08-19"), "ISIN"] == "INE002A01018"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("nse_reliance_2020_2026.json", "INE002A01018"),
        ("nse_itc_2025_demerger.json", "INE154A01025"),
    ],
)
def test_isin_from_actions_returns_the_recorded_identifier(name, expected):
    frame = ca.parse_corporate_actions(load(name), url="fixture")
    assert ca.isin_from_actions(frame) == expected


# ================================================================= isin_from_actions refuses


def test_a_missing_isin_is_none_not_an_empty_string_or_the_string_none():
    """Negative control.

    Red if the accessor returns ``""``, ``"None"``, or any other truthy placeholder: an empty
    string joins to nothing and a literal "None" is a fabricated identifier that looks real. The
    row is built through the parser from a payload row whose ``isin`` is null, so the whole chain
    is exercised rather than a hand-made frame.
    """
    frame = events(payload_row("Dividend - Rs 6.50 Per Share", "02-Jul-2020", isin=None))
    assert frame["ISIN"].isna().all(), "the parser must map an absent isin to None"
    assert ca.isin_from_actions(frame) is None


def test_a_payload_row_without_an_isin_key_is_none_too():
    """The same case with the key absent entirely, which ``str(None)`` used to turn into "None"."""
    row = payload_row("Dividend - Rs 6.50 Per Share", "02-Jul-2020")
    row.pop("isin")
    frame = events(row)
    assert ca.isin_from_actions(frame) is None


def test_blank_cells_are_absence_not_a_value():
    """A frame that says "no ISIN" in three ways has no ISIN. ``""`` is not an identifier."""
    frame = pd.DataFrame(
        {"ISIN": ["", None, "   "]},
        index=pd.DatetimeIndex(["2020-01-01", "2020-01-02", "2020-01-03"], name="Date"),
    )
    assert ca.isin_from_actions(frame) is None


def test_an_empty_frame_has_no_isin():
    empty = ca.parse_corporate_actions([], symbol="RELIANCE", url="fixture")
    assert ca.isin_from_actions(empty) is None


def test_a_frame_without_an_isin_column_has_no_isin():
    frame = pd.DataFrame(
        {"Action": [ca.DIVIDEND]}, index=pd.DatetimeIndex(["2020-01-01"], name="Date")
    )
    assert ca.isin_from_actions(frame) is None


def test_disagreeing_rows_return_none_rather_than_picking_a_winner():
    """A market-wide pull has many ISINs. Choosing the first would hand back a wrong identifier.

    The refusal is the honest answer: this frame does not identify one instrument, so the caller
    must not be given one instrument's ISIN as if it did.
    """
    frame = events(
        payload_row("Dividend - Rs 1 Per Share", "01-Jan-2020", isin="INE002A01018"),
        payload_row("Dividend - Rs 2 Per Share", "02-Jan-2020", isin="INE154A01025"),
    )
    assert ca.isin_from_actions(frame) is None


# ===================================================================== splits_from_actions


class TestSplitsFromActions:
    def test_a_bonus_row_produces_the_factor_on_its_ex_date(self, reliance_events):
        """RELIANCE's 1:1 bonus is 2.0 in yfinance's convention, dated at the true ex-date."""
        splits = ca.splits_from_actions(reliance_events)
        assert isinstance(splits.index, pd.DatetimeIndex)
        assert list(splits.index) == [pd.Timestamp("2024-10-28")]
        assert splits.loc[pd.Timestamp("2024-10-28")] == pytest.approx(2.0)
        assert splits.name == "Stock Splits"
        assert splits.dtype == float

    def test_a_face_value_subdivision_produces_the_ratio(self):
        """Rs 10 -> Rs 2 is a 5-for-1: the factor is the face-value ratio, not the rupee amounts."""
        frame = events(
            payload_row(
                "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
                "10-Jan-2025",
            )
        )
        splits = ca.splits_from_actions(frame)
        assert splits.loc[pd.Timestamp("2025-01-10")] == pytest.approx(5.0)

    def test_no_split_rows_is_empty_with_a_datetime_index(self):
        """A dividend-only feed is a real answer: empty Series, not a missing or object index."""
        frame = events(payload_row("Dividend - Rs 6.50 Per Share", "02-Jul-2020"))
        splits = ca.splits_from_actions(frame)
        assert splits.empty
        assert isinstance(splits.index, pd.DatetimeIndex)
        assert list(splits.index) == []
        assert splits.dtype == float
        assert splits.name == "Stock Splits"

    def test_an_empty_events_frame_is_the_same_empty_series(self):
        splits = ca.splits_from_actions(
            ca.parse_corporate_actions([], symbol="RELIANCE", url="fixture")
        )
        assert splits.empty
        assert isinstance(splits.index, pd.DatetimeIndex)

    def test_a_dividend_row_never_becomes_a_split_factor(self):
        """Negative control.

        Red if the helper derives a factor from a dividend row — for instance by reading ``Value``
        (6.5 and 4.0 here), by inventing a 1.0 row, or by treating "a row exists" as "a split
        happened". A rupee amount is not a share ratio, and 6.5 would rescale every prior bar by
        6.5x.
        """
        frame = events(
            payload_row("Dividend - Rs 6.50 Per Share", "02-Jul-2020"),
            payload_row("Interim Dividend - Rs 4 Per Share", "01-Aug-2020"),
        )
        assert frame["Value"].tolist() == [6.5, 4.0]
        assert frame["SplitRatio"].isna().all()
        assert ca.splits_from_actions(frame).empty

    def test_a_composite_dividend_and_split_row_keeps_its_factor(self):
        """The ratio is the discriminator, not ``Action``.

        This row is classified ``dividend`` (the cash amount is what ``Value`` carries) while
        declaring a real 2x sub-division. yfinance publishes the 2.0 on the same date, so dropping
        it because of the ``Action`` label would lose a genuine factor.
        """
        frame = events(payload_row(COMPOSITE_SUBJECT, "21-Jan-2015"))
        assert frame["Action"].tolist() == [ca.DIVIDEND]
        assert frame["Value"].tolist() == [1.5]
        splits = ca.splits_from_actions(frame)
        assert splits.loc[pd.Timestamp("2015-01-21")] == pytest.approx(2.0)

    def test_two_rows_on_one_ex_date_compound(self):
        """Two issuances on one date multiply — the rule ``actions_frame`` documents.

        A duplicate index would make every downstream reindex ambiguous, so they are collapsed.
        """
        frame = events(
            payload_row("Bonus 1:1", "28-Oct-2024"),
            payload_row("Bonus 1:1", "28-Oct-2024"),
        )
        splits = ca.splits_from_actions(frame)
        assert list(splits.index) == [pd.Timestamp("2024-10-28")]
        assert splits.loc[pd.Timestamp("2024-10-28")] == pytest.approx(4.0)

    def test_an_undated_split_is_left_out_of_the_series(self):
        """``NaT`` has no place in a time series; the row stays visible on the events frame."""
        frame = events(payload_row("Bonus 1:1", "-"))
        assert len(frame) == 1 and frame["SplitRatio"].notna().all()
        assert ca.splits_from_actions(frame).empty

    def test_the_recorded_itc_splits_are_all_present(self, itc_events):
        splits = ca.splits_from_actions(itc_events)
        assert splits.to_dict() == {
            pd.Timestamp("2010-08-03"): pytest.approx(2.0),
            pd.Timestamp("2016-07-01"): pytest.approx(1.5),
        }
        assert splits.index.is_monotonic_increasing


# ========================================================= archived-index ISIN fallback


def _archive(root: Path) -> U.UniverseArchive:
    """A real archive written from a real constituent CSV, with the recorded ISIN values."""
    archive = U.UniverseArchive(root)
    frame = U.parse_constituents(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Adani Enterprises Ltd.,Metals & Mining,ADANIENT,EQ,INE423A01024\n"
        "Bajaj Auto Ltd.,Automobiles,BAJAJ-AUTO,EQ,INE917I01010\n",
        index="nifty50",
        url="fixture",
        retrieved_utc="2026-09-27T20:44:25+00:00",
    )
    archive.record(
        "nifty50",
        frame,
        retrieved_utc="2026-09-27T20:44:25+00:00",
        recorded_on="2026-09-27",
    )
    return archive


class TestArchivedIndexIsinFallback:
    def test_returns_the_isin_the_archive_holds(self, tmp_path, no_network):
        archive = _archive(tmp_path / "universe")
        assert U.isin_from_archive("BAJAJ-AUTO", root=archive.root) == "INE917I01010"
        # A yfinance-shaped ticker resolves to the same membership row instead of silently
        # missing the join that the rest of the package performs.
        assert U.isin_from_archive("BAJAJ-AUTO.NS", root=archive.root) == "INE917I01010"

    def test_an_unarchived_symbol_returns_none_without_opening_a_socket(
        self, tmp_path, no_network
    ):
        """Negative control.

        Red if the fallback fabricates an identifier (from a name match, a company field, a
        different index page) or if it decides to fetch one: every socket connect here raises.
        """
        archive = _archive(tmp_path / "universe")
        assert U.isin_from_archive("RELIANCE", root=archive.root) is None

    def test_the_socket_guard_is_actually_armed(self, no_network):
        """A guard that never fires proves nothing, so it is fired once, deliberately."""
        with socket.socket() as probe, pytest.raises(AssertionError, match="network I/O"):
            probe.connect(("127.0.0.1", 9))

    def test_a_missing_archive_returns_none_and_is_not_created(self, tmp_path, no_network):
        """Read-only: a lookup must not create the directories ``UniverseArchive`` would."""
        root = tmp_path / "never-written"
        assert U.isin_from_archive("RELIANCE", root=root) is None
        assert not root.exists(), "a read-only lookup created the archive it was asked to read"

    def test_a_recorded_member_with_no_isin_returns_none(self, tmp_path, no_network):
        """The symbol is in the archive, but the archive has no identifier for it.

        Red if the fallback falls back to the company name or to any other column: "No Isin Ltd."
        is present here precisely so a name-based match would have something to match.
        """
        archive = U.UniverseArchive(tmp_path / "universe")
        frame = U.parse_constituents(
            "Company Name,Industry,Symbol,Series\nNo Isin Ltd.,Misc,NOISIN,EQ\n",
            index="nifty50",
            url="fixture",
            retrieved_utc="2026-09-27T20:44:25+00:00",
        )
        archive.record(
            "nifty50",
            frame,
            retrieved_utc="2026-09-27T20:44:25+00:00",
            recorded_on="2026-09-27",
        )
        assert U.isin_from_archive("NOISIN", root=archive.root) is None

    def test_a_near_miss_symbol_does_not_match_a_member(self, tmp_path, no_network):
        """``ADANI`` is a prefix of ``ADANIENT`` and part of its company name; it is not a member."""
        archive = _archive(tmp_path / "universe")
        assert U.isin_from_archive("ADANI", root=archive.root) is None
        assert U.isin_from_archive("", root=archive.root) is None

    def test_the_docstring_states_its_coverage(self):
        """The plan requires the coverage to be stated, not implied by silence."""
        doc = (U.isin_from_archive.__doc__ or "").lower()
        assert "archived indices only" in doc
        assert "no request" in doc
