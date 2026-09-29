"""
The mirror's contract, and above all its refusal to be ambiguous.

The defect this module exists to prevent is a silent empty frame. A mirror that answers
"zero rows" for a delisted security, for a day before it listed, and for a day it simply
has not downloaded yet has moved the problem from the network into the database and called
it a feature. So the majority of these tests are about the FOUR-WAY distinction in
:meth:`Mirror.coverage`, not about the happy path.

Non-vacuity is enforced by mutation at the end of the session, not asserted here: every
check below is written so that a plausible wrong implementation fails it.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from bfinance.mirror import (
    SCHEMA_VERSION,
    Mirror,
    default_mirror_path,
    holidays_from_calendar,
)


def market_frame(rows) -> pd.DataFrame:
    """A whole-market frame shaped exactly as bhavcopy_range returns one."""
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex([r[0] for r in rows]),
         pd.Index([r[1] for r in rows], dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        [(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows],
        columns=["Open", "High", "Low", "Close", "Volume", "TurnoverValue"],
        index=index,
    )
    frame["Volume"] = frame["Volume"].astype("int64")
    return frame.sort_index()


DAY1 = "2026-09-23"
HOLIDAY = "2026-01-26"
DAY2 = "2026-09-24"


def sample(rows_for_day1, rows_for_day2=None) -> pd.DataFrame:
    rows = list(rows_for_day1)
    rows += [(d, *rest) for (d, *rest) in (rows_for_day2 or [])]
    return market_frame(rows)


BASE_A = [
    (DAY1, "RELIANCE", 1400.0, 1420.0, 1390.0, 1410.0, 1000, 1_410_000.0),
    (DAY1, "TCS", 3000.0, 3050.0, 2980.0, 3020.0, 2000, 6_040_000.0),
    (DAY1, "SBIN", 700.0, 710.0, 695.0, 705.0, 3000, 2_115_000.0),
]
BASE_B = [
    (DAY2, "RELIANCE", 1410.0, 1430.0, 1405.0, 1425.0, 1100, 1_567_500.0),
    (DAY2, "TCS", 3020.0, 3070.0, 3010.0, 3050.0, 2100, 6_405_000.0),
]


@pytest.fixture()
def mirror(tmp_path) -> Mirror:
    m = Mirror(tmp_path / "mirror.db")
    yield m
    m.close()


# ------------------------------------------------------------------ construction
class TestConstruction:
    def test_constructing_touches_no_disk_and_no_network(self, tmp_path):
        path = tmp_path / "never.db"
        m = Mirror(path)
        assert not path.exists(), "constructing must not create the file; that is sync()'s job"
        m.close()

    def test_default_path_sits_beside_the_cache(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BFINANCE_HOME", str(tmp_path))
        assert default_mirror_path() == tmp_path / "mirror.db"

    def test_schema_version_is_recorded(self, mirror):
        mirror._connect()
        row = mirror._conn.execute(
            "SELECT value FROM mirror_meta WHERE key='schema_version'").fetchone()
        assert int(row["value"]) == SCHEMA_VERSION

    def test_a_mirror_written_by_another_schema_version_is_refused(self, mirror, tmp_path):
        """Reading a layout you do not understand is worse than not reading it."""
        mirror._connect()
        mirror._conn.execute(
            "UPDATE mirror_meta SET value='999' WHERE key='schema_version'")
        mirror._conn.commit()
        mirror.close()
        other = Mirror(tmp_path / "mirror.db")
        with pytest.raises(RuntimeError, match="schema version"):
            other._connect()
        other.close()


# ---------------------------------------------------------------------- writing
class TestPutDay:
    def test_stores_every_symbol_in_the_frame(self, mirror):
        n = mirror.put_day(DAY1, sample(BASE_A), "udiff", "abc123")
        assert n == 3
        assert mirror.read("RELIANCE", DAY1, DAY1).shape[0] == 1
        assert mirror.read("TCS", DAY1, DAY1).shape[0] == 1

    def test_values_round_trip_exactly(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "abc")
        got = mirror.read("RELIANCE", DAY1, DAY1)
        assert float(got["Open"].iloc[0]) == 1400.0
        assert float(got["High"].iloc[0]) == 1420.0
        assert float(got["Low"].iloc[0]) == 1390.0
        assert float(got["Close"].iloc[0]) == 1410.0
        assert int(got["Volume"].iloc[0]) == 1000

    def test_writes_the_coverage_row(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "deadbeef")
        cov = mirror.coverage(DAY1, DAY1)
        c = cov[DAY1]
        assert c.downloaded is True
        assert c.n_rows == 3
        assert c.era == "udiff"

    def test_re_putting_a_day_replaces_it_rather_than_accumulating(self, mirror):
        """
        Today's file is republished intraday. A mirror that appended would carry two
        different OHLC rows for the same (day, symbol) and a reader would get whichever the
        index returned.
        """
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "one")
        revised = [(r[0], r[1], r[2], r[3], r[4], r[5] + 1.0, r[6], r[7]) for r in BASE_A]
        mirror.put_day(DAY1, market_frame(revised), "udiff", "two")
        got = mirror.read("RELIANCE", DAY1, DAY1)
        assert len(got) == 1, "one row per (day, symbol), not two"
        assert float(got["Close"].iloc[0]) == 1411.0

    def test_a_sliced_frame_is_rejected_with_a_reason(self, mirror):
        """
        Storing one symbol per call would reintroduce exactly the O(symbols x days) problem
        this store exists to remove, so the shape is enforced rather than documented.
        """
        one = market_frame([BASE_A[0]]).reset_index()
        with pytest.raises(ValueError, match="not a per-symbol slice"):
            mirror.put_day(DAY1, one[["Symbol", "Open"]], "udiff", "x")

    def test_an_empty_frame_stores_nothing_and_does_not_mark_the_day(self, mirror):
        assert mirror.put_day(DAY1, pd.DataFrame(), "udiff", "x") == 0
        assert mirror.coverage(DAY1, DAY1)[DAY1].downloaded is False

    def test_a_repeated_symbol_in_one_day_is_refused_not_upserted(self, mirror):
        """
        The primary key is (day, symbol, series) and the insert is an upsert, so a day
        carrying one symbol twice would silently lose a row. A bhavcopy file holding both
        EQ and BE for a single symbol is the real route to that, and the outcome -- a BSE
        bar relabelled EQ and served from a read filtered on EQ -- is the same defect the
        Trendlyne listing work documents from the other side: a frame that reads NSE and
        holds BSE history.
        """
        collided = market_frame([
            (DAY1, "SBIN", 1.0, 2.0, 0.5, 1.5, 10, 15.0),
            (DAY1, "SBIN", 9.0, 9.0, 9.0, 9.0, 20, 180.0),
        ])
        with pytest.raises(ValueError, match="appear more than once"):
            mirror.put_day(DAY1, collided, "udiff", "x")
        assert mirror.read("SBIN", DAY1, DAY1).empty, (
            "the refused day must leave nothing behind, not half of it"
        )

    def test_the_series_is_stamped_from_the_caller_not_assumed(self, mirror):
        """
        The parsed frame carries no per-row series -- only ``attrs['bfinance_series']``,
        a tuple of the series present somewhere in the file. So the caller states it, and
        a non-default series must be honoured rather than normalised to EQ.
        """
        frame = market_frame([(DAY1, "SBIN", 1.0, 2.0, 0.5, 1.5, 10, 15.0)])
        assert mirror.put_day(DAY1, frame, "udiff", "a", series="BE") == 1
        assert len(mirror.read("SBIN", DAY1, DAY1, series="BE")) == 1
        assert mirror.read("SBIN", DAY1, DAY1, series="EQ").empty, (
            "a BE row must not be visible to a read filtered on EQ"
        )

    def test_a_clean_real_shaped_day_stores_every_row(self, mirror):
        """The guard must cost nothing on a real file: measured live, one row per symbol."""
        rows = [(DAY1, f"SYM{i}", 1.0, 2.0, 0.5, 1.5, 10, 15.0) for i in range(50)]
        assert mirror.put_day(DAY1, market_frame(rows), "udiff", "a") == 50
        assert mirror.stats()["symbols"] == 50


# ---------------------------------------------------------------------- reading
class TestRead:
    def test_empty_result_carries_the_right_columns_and_index(self, mirror):
        """
        A caller must be able to tell an empty frame apart from a differently-shaped one,
        or `df["Close"]` raises on exactly the frame most likely to be mishandled.
        """
        got = mirror.read("NOPE", DAY1, DAY2)
        assert got.empty
        assert list(got.columns) == ["Open", "High", "Low", "Close", "Volume", "TurnoverValue"]
        assert got.index.name == "Date"

    def test_range_is_inclusive_and_ordered(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        mirror.put_day(DAY2, sample(BASE_B), "udiff", "b")
        got = mirror.read("RELIANCE", DAY1, DAY2)
        assert list(got.index.strftime("%Y-%m-%d")) == [DAY1, DAY2]
        assert got.index.is_monotonic_increasing

    def test_read_many_serves_many_symbols_from_one_query(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        out = mirror.read_many(["RELIANCE", "TCS", "SBIN"], DAY1, DAY2)
        assert set(out) == {"RELIANCE", "TCS", "SBIN"}
        assert all(len(v) == 1 for v in out.values())
        assert float(out["TCS"]["Close"].iloc[0]) == 3020.0

    def test_read_many_with_no_symbols_is_not_a_query(self, mirror):
        assert mirror.read_many([], DAY1, DAY2) == {}

    def test_read_never_touches_the_network(self, mirror, monkeypatch):
        """A mirror read is a local read. A network call here would defeat the whole design."""
        import bfinance.nse.bhavcopy as bc

        def explode(*a, **k):
            raise AssertionError("read() must not reach the network")

        monkeypatch.setattr(bc, "_get_archive", explode, raising=False)
        monkeypatch.setattr(bc, "bhavcopy_range", explode, raising=False)
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        assert len(mirror.read("RELIANCE", DAY1, DAY1)) == 1
        assert len(mirror.read_many(["TCS"], DAY1, DAY1)["TCS"]) == 1


# ---------------------------------------------------------------------- coverage
class TestCoverageIsNotAmbiguous:
    """
    The four-way distinction. These are the tests that matter: an implementation that
    collapsed any two of these states into one answer would pass every other test here.
    """

    def test_downloaded_trading_day_with_a_row_is_real_data(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        mirror.set_holidays([HOLIDAY])
        c = mirror.coverage(DAY1, DAY1)[DAY1]
        assert c.downloaded is True and c.is_trading_day is True and c.n_rows == 3

    def test_downloaded_trading_day_without_a_row_means_not_held_or_not_traded(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        mirror.set_holidays([HOLIDAY])
        cov = mirror.coverage(DAY1, DAY2)
        assert cov[DAY1].downloaded is True
        assert cov[DAY2].downloaded is False
        assert mirror.read("RELIANCE", DAY2, DAY2).empty
        # and the difference is REPORTED, not merely inferable from the empty frame
        assert cov.missing_trading_days == [DAY2]

    def test_a_holiday_is_not_reported_as_a_gap(self, mirror):
        """
        The failure this whole design turns on. A holiday has no file, ever. Reporting it
        as a missing trading day would send a caller to the network for a file that does not
        exist, forever.

        The store holds HOLIDAYS, not trading days, because that is the shape of the
        upstream payload: 20 CM rows, all of them holidays. Adding a holiday to the list
        makes that date non-trading; every other weekday in a covered year is a trading day.
        """
        mirror.set_holidays([HOLIDAY])
        cov = mirror.coverage(HOLIDAY, HOLIDAY)
        assert cov[HOLIDAY].is_trading_day is False
        assert cov[HOLIDAY].downloaded is False
        assert cov.missing_trading_days == [], "a holiday is not a gap"
        assert cov.holidays == [HOLIDAY]

    def test_without_a_calendar_the_mirror_refuses_to_guess(self, mirror):
        """
        With no trading calendar, a missing day could be a holiday or a gap, and the honest
        answer is to say so rather than to assume either.
        """
        cov = mirror.coverage(DAY1, DAY2)
        assert cov[DAY1].is_trading_day is None
        assert cov[DAY2].is_trading_day is None
        assert cov.missing_trading_days == [DAY1, DAY2], (
            "with no calendar both days are reported as possible gaps, not as neither"
        )

    def test_todays_row_goes_stale_so_a_partial_session_is_not_served(self, mirror):
        today = dt.date.today().isoformat()
        frame = market_frame([(today, "RELIANCE", 1.0, 2.0, 0.5, 1.5, 10, 15.0)])
        mirror.put_day(today, frame, "udiff", "live")
        assert mirror.coverage(today, today)[today].downloaded is True
        mirror.mark_today_stale()
        assert mirror.coverage(today, today)[today].downloaded is False, (
            "today's file is republished intraday; a cached copy must stop counting as held"
        )

    def test_summary_reports_all_three_counts(self, mirror):
        """
        2026-09-23 (Wed) to 2026-10-02 (Fri) is ten calendar days: two weekend days
        (26-27 Sep), one seeded holiday (26 Jan is outside the span, so the span's only
        non-weekend non-trading day comes from the holiday list), leaving seven trading
        days of which one is held. Counts are recomputed here rather than copied from the
        module so a change in the arithmetic shows up as a test failure.
        """
        mirror.set_holidays(["2026-10-02"])
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        cov = mirror.coverage(DAY1, "2026-10-02")
        text = cov.summary()
        assert "10 day(s) considered" in text
        assert "1 downloaded" in text
        # seven trading days, one held, so six are genuine gaps
        assert "6 trading day(s) missing" in text
        # two weekend days plus the seeded holiday
        assert "3 non-trading day(s)" in text
        assert cov.missing_trading_days == [
            "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
            "2026-10-01"]

    def test_coverage_object_is_iterable_and_sized(self, mirror):
        cov = mirror.coverage(DAY1, DAY2)
        assert len(cov) == 2
        assert {d.day for d in cov} == {DAY1, DAY2}
        assert DAY1 in cov and "2020-01-01" not in cov


# ------------------------------------------------------------- calendar parsing
class TestCalendarPolarity:
    """
    The payload is a HOLIDAY list, and reading it as a trading calendar is a bug that
    shipped and was caught by the end-to-end run.

    Measured on the real CM segment, 2026-09-29: 20 rows, every one a holiday ("Republic
    Day", "Holi", "Mahashivratri", "25-Dec-2026"), and every one carrying
    `morning_session: null` AND `evening_session: null`. So null-both means "this row is a
    holiday", not "no session information". An earlier version read it as "therefore a
    trading day" and would have made the mirror demand ~20 files a year that are never
    published -- the exact failure the watermark exists to prevent.
    """

    # The real rows, verbatim from /holiday-master?type=trading, CM segment.
    REAL_CM = [
        {"tradingDate": "15-Jan-2026", "weekDay": "Thursday",
         "description": "Municipal Corporation Election - Maharashtra",
         "morning_session": None, "evening_session": None, "Sr_no": 1},
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day",
         "morning_session": None, "evening_session": None, "Sr_no": 2},
        {"tradingDate": "15-Feb-2026", "weekDay": "Sunday", "description": "Mahashivratri",
         "morning_session": None, "evening_session": None, "Sr_no": 3},
        {"tradingDate": "03-Mar-2026", "weekDay": "Tuesday", "description": "Holi",
         "morning_session": None, "evening_session": None, "Sr_no": 4},
    ]

    def test_every_real_row_is_a_holiday(self):
        assert holidays_from_calendar(self.REAL_CM) == [
            "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03"]

    def test_null_sessions_do_not_make_a_holiday_a_trading_day(self):
        """The exact regression. Republic Day is not a trading day."""
        hol = set(holidays_from_calendar(self.REAL_CM))
        assert "2026-01-26" in hol

    def test_a_real_weekday_is_not_in_the_holiday_set(self):
        hol = set(holidays_from_calendar(self.REAL_CM))
        assert "2026-09-24" not in hol

    def test_a_session_flagged_row_is_still_a_holiday(self):
        """
        Even a row that DOES carry session times is a holiday -- the route only lists
        non-trading days. Reading session flags as the signal would drop it.
        """
        rows = [{"tradingDate": "02-Oct-2026", "morning_session": "Closed",
                 "evening_session": "Closed"}]
        assert holidays_from_calendar(rows) == ["2026-10-02"]

    def test_parses_the_nse_date_format(self):
        assert holidays_from_calendar(
            [{"tradingDate": "15-Jan-2026"}]) == ["2026-01-15"]

    def test_unparseable_dates_are_skipped_not_guessed(self):
        assert holidays_from_calendar([{"tradingDate": "not-a-date"}]) == []

    def test_rows_without_a_date_are_skipped(self):
        assert holidays_from_calendar([{"weekDay": "Thursday"}]) == []


class TestTradingDayInference:
    def test_a_seeded_holiday_is_not_a_trading_day(self, mirror):
        mirror.set_holidays(["2026-01-26"])
        assert mirror._is_trading_day("2026-01-26") is False

    def test_an_unlisted_weekday_is_a_trading_day(self, mirror):
        mirror.set_holidays(["2026-01-26"])
        assert mirror._is_trading_day("2026-09-24") is True

    def test_a_weekend_is_not_a_trading_day(self, mirror):
        mirror.set_holidays([])
        assert mirror._is_trading_day("2026-01-25") is False, "a Sunday never traded"

    def test_a_year_the_calendar_does_not_cover_is_unknown_not_guessed(self, mirror):
        """
        The payload covers one calendar year (measured: 15-Jan-2026..25-Dec-2026). A date
        outside it must return None, because calling it a trading day would invent a gap and
        calling it a holiday would hide a real one.
        """
        mirror.set_holidays(["2026-01-26"])
        assert mirror._is_trading_day("2025-06-16") is None
        assert mirror._is_trading_day("2027-01-26") is None

    def test_no_calendar_at_all_means_unknown(self, mirror):
        assert mirror._is_trading_day("2026-09-24") is None

    def test_sync_without_a_calendar_refuses_rather_than_guessing(self, mirror):
        with pytest.raises(LookupError, match="holiday calendar"):
            mirror.missing_trading_days_in_range(DAY1, DAY2)


# ------------------------------------------------------------------------ stats
class TestStats:
    def test_reports_rows_days_symbols_and_size(self, mirror):
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        mirror.put_day(DAY2, sample(BASE_B), "udiff", "b")
        s = mirror.stats()
        assert s["rows"] == 5, "3 symbols on DAY1 plus 2 on DAY2"
        assert s["days"] == 2
        assert s["symbols"] == 3
        assert s["first_day"] == DAY1 and s["last_day"] == DAY2
        assert s["bytes"] > 0
        assert s["bytes_per_row"] > 0

    def test_stats_on_an_empty_mirror_do_not_divide_by_zero(self, mirror):
        s = mirror.stats()
        assert s["rows"] == 0
        assert s["bytes_per_row"] is None

    def test_on_disk_bytes_includes_the_write_ahead_log(self, mirror, tmp_path):
        """
        WAL keeps freshly committed pages in the -wal file, so reading the main file alone
        reports one page for a database holding every row. Measured on a 4-day mirror: the
        main file was 4,096 bytes and the pair together 1,368,064 -- a 334x under-report.
        """
        mirror.put_day(DAY1, sample(BASE_A), "udiff", "a")
        mirror.put_day(DAY2, sample(BASE_B), "udiff", "b")
        total = mirror._on_disk_bytes()
        main = Path(str(mirror.path)).stat().st_size
        wal = Path(str(mirror.path) + "-wal")
        assert total >= main
        if wal.exists():
            assert total > main, (
                "a -wal file exists, so the total must exceed the main file or the WAL "
                "pages are being ignored"
            )
        assert mirror.stats()["bytes"] == total
        assert mirror.stats()["bytes_per_row"] > 1.0, (
            "a real mirror is ~119 bytes per row; anything near 1 means the size is wrong"
        )
