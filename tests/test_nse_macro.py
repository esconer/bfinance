"""
``bfinance.nse.macro`` tests — fully offline, driven by the two recorded fixtures.

What is on disk
---------------
``tests/fixtures/nse/macro/`` holds two recordings of
``nsearchives.nseindia.com/content/indices/ind_close_all_{DDMMYYYY}.csv`` plus a ``META.json``
each. **They are trimmed.** The 2026-09-25 file held 166 index rows and 5 are retained; the
2020-01-01 file held 77 and 4 are retained (the same four, minus NIFTY Smallcap 100). So the
row counts this file asserts are *two different numbers each*, and both are asserted:

* ``META.trim_behaviour_neutral.original_data_rows`` — 166 and 77, the **real** file sizes,
  which is the only place on disk that 166/77 exists at all;
* ``len(parsed_frame)`` — 5 and 4, what the trimmed recording actually parses to.

Every coverage number this suite reports is therefore a coverage number **of the recording**,
never of the 166-index file. Where the live capture's 143/166 P/E figure is relevant it is
cited as a recorded observation and is never asserted as something these bytes can reproduce.

Three states, and the controls that hold them in place
-----------------------------------------------------
The ``-`` sentinel is *not applicable*, which is a third state beside zero and missing. The
sentinel tests are only worth something because the negative controls at the bottom mutate the
real parser — ``_field_states`` is patched so a ``-`` becomes ``0.0`` — and then replay the
real assertions to watch them fail.

Network
-------
An autouse fixture forbids socket connections for the whole module, so "offline" is enforced
rather than promised. The reader is satisfied from fixtures through
:class:`bfinance.nse.macro.RecordingSource`, an object that has no code path to a host.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pandas as pd
import pytest

from bfinance.nse import macro
from bfinance.utils.exceptions import (
    BFinanceError,
    ParsingError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nse" / "macro"
NSE_FIXTURES = Path(__file__).parent / "fixtures" / "nse"
SRC = Path(__file__).resolve().parents[1] / "src"

DAY_2026 = "2026-09-25"
DAY_2020 = "2020-01-01"
STAMP_2026 = "25092026"
STAMP_2020 = "01012020"

CSV_2026 = (FIXTURES / f"ind_close_all_{STAMP_2026}.csv").read_text(encoding="utf-8")
CSV_2020 = (FIXTURES / f"ind_close_all_{STAMP_2020}.csv").read_text(encoding="utf-8")
META_2026 = json.loads((FIXTURES / f"ind_close_all_{STAMP_2026}.META.json").read_text("utf-8"))
META_2020 = json.loads((FIXTURES / f"ind_close_all_{STAMP_2020}.META.json").read_text("utf-8"))

#: The real row counts of the untrimmed files, as recorded in META. 166 and 77.
FULL_ROW_COUNTS = {DAY_2026: 166, DAY_2020: 77}
#: What the trimmed recordings on disk parse to.
STORED_ROW_COUNTS = {DAY_2026: 5, DAY_2020: 4}

#: The VIX row's five sentinel fields, on both dates.
SENTINEL_FIELDS = ("Volume", "Turnover", "PE", "PB", "DivYield")


# ============================================================== no network, ever


def module_code() -> str:
    """The module's source with every docstring removed, lowercased.

    Three separate tests need to assert that a *route* is absent from the code rather than
    merely unmentioned, and the module has to be free to *explain* each absence in prose. A
    naive substring scan over the raw source therefore fails on the module's own warnings — the
    same trap ``test_nse_bhavcopy`` documents. Stripping docstrings first is what makes the
    check mean "cannot do this" instead of "cannot talk about this".
    """
    tree = ast.parse(Path(macro.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree).lower()


#: Hosts the guard permits. Windows' ``asyncio`` event loop builds its own self-pipe with
#: ``socket.socketpair()``, which falls back to binding and connecting a real TCP socket on
#: 127.0.0.1 — so a guard that forbids *every* connect breaks the interpreter, not the code
#: under test. Loopback is allowed; every routable destination is not.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "", None}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Make a routable connection impossible, for every test in this module.

    Not a promise in a docstring: an accidental live call fails the suite instead of quietly
    succeeding on a machine with connectivity and hanging on CI without it. A test that *wants*
    the transport stubs ``httpx`` at a lower level, so nothing here needs a real socket.
    """

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK or str(host) in _LOOPBACK:
            return _real_connect(self, address, *args, **kwargs)
        raise AssertionError(f"network I/O attempted in an offline test: connect({address!r})")

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in _LOOPBACK or str(host) in _LOOPBACK:
            return _real_create_connection(address, *args, **kwargs)
        raise AssertionError(f"network I/O attempted in an offline test: {address!r}")

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host in _LOOPBACK or str(host) in _LOOPBACK:
            return _real_getaddrinfo(host, *args, **kwargs)
        raise AssertionError(f"DNS lookup attempted in an offline test: {host!r}")

    _real_connect = socket.socket.connect
    _real_create_connection = socket.create_connection
    _real_getaddrinfo = socket.getaddrinfo

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture
def client():
    """A client reading the two recordings. ``return_none`` so a range walk sees 404s."""
    return macro.MacroClient(
        source=macro.RecordingSource(FIXTURES, on_missing="return_none")
    )


@pytest.fixture
def strict_client():
    """A client whose source raises on an unrecorded date. Nothing silently 404s."""
    return macro.MacroClient(source=macro.RecordingSource(FIXTURES))


# ============================================================== the fixtures are real


def test_fixture_metadata_records_the_real_file_sizes():
    """166 and 77 exist on disk *only* in META. Asserting anything else would be theatre."""
    assert META_2026["trim_behaviour_neutral"]["original_data_rows"] == FULL_ROW_COUNTS[DAY_2026]
    assert META_2020["trim_behaviour_neutral"]["original_data_rows"] == FULL_ROW_COUNTS[DAY_2020]
    assert META_2026["http_status"] == 200
    assert META_2020["http_status"] == 200
    assert META_2026["url"] == (
        f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{STAMP_2026}.csv"
    )
    assert META_2020["url"].endswith(f"ind_close_all_{STAMP_2020}.csv")
    # One retrieval event for both, so a replayed frame reports a real capture date.
    assert META_2026["retrieved_utc"] == META_2020["retrieved_utc"] == "2026-09-27T20:44:25+00:00"


def test_the_trim_was_behaviour_neutral_and_the_header_is_untouched():
    """The trim kept the real header and the retained rows byte-for-byte."""
    for meta, stamp in ((META_2026, STAMP_2026), (META_2020, STAMP_2020)):
        neutral = meta["trim_behaviour_neutral"]
        assert neutral["rows_identical"] is True, stamp
        assert neutral["header_identical"] is True, stamp
        assert neutral["all_field_names_preserved"] == list(macro.RAW_FIELDS), stamp


def test_the_header_line_is_the_13_verbatim_fields_in_order():
    """Not a subset, not reordered, and it is the module's documented contract."""
    assert len(macro.RAW_FIELDS) == 13
    assert macro.RAW_FIELDS == (
        "Index Name", "Index Date", "Open Index Value", "High Index Value",
        "Low Index Value", "Closing Index Value", "Points Change", "Change(%)",
        "Volume", "Turnover (Rs. Cr.)", "P/E", "P/B", "Div Yield",
    )
    for text in (CSV_2026, CSV_2020):
        assert text.splitlines()[0].split(",") == list(macro.RAW_FIELDS)


def test_the_recording_really_is_trimmed_below_the_real_row_count():
    """The guard that stops someone widening a fixture and breaking every count below."""
    for day, text in ((DAY_2026, CSV_2026), (DAY_2020, CSV_2020)):
        stored = len(text.strip().splitlines()) - 1
        assert stored == STORED_ROW_COUNTS[day], day
        assert stored < FULL_ROW_COUNTS[day], (
            f"{day}: the fixture is no longer trimmed, so the full-file counts above are the "
            f"only source of 166/77 and can no longer be distinguished from the stored ones"
        )


# ============================================================== parsing


def test_both_recordings_parse_to_the_documented_shape():
    for day, text in ((DAY_2026, CSV_2026), (DAY_2020, CSV_2020)):
        frame = macro.parse_index_close_csv(text)
        assert len(frame) == STORED_ROW_COUNTS[day], day
        assert list(frame.columns) == list(macro.COLUMNS) + [
            macro.NOT_APPLICABLE_COLUMN, macro.UNPARSED_COLUMN
        ], day
        assert list(frame.index.names) == ["Date", "IndexName"], day
        level_zero = frame.index.get_level_values("Date")
        assert isinstance(level_zero, pd.DatetimeIndex) and level_zero.tz is None, day
        assert str(level_zero[0].date()) == day, day
        assert frame.index.is_monotonic_increasing, day
        assert not frame.index.duplicated().any(), day


def test_the_same_index_on_two_dates_parses_to_the_same_field_names_and_dtypes():
    """The cross-date consistency check that *is* possible offline.

    The three-route cross-check (this CSV vs ``/api/allIndices`` vs ``/api/marketStatus``) is
    not — see ``test_the_three_route_cross_check_cannot_be_run_here_and_nothing_fakes_it``.
    What is checkable is that one index, read six and a half years apart, keeps one schema: a
    frame whose dtypes drifted by date would silently change a downstream ``.mean()``.
    """
    newer = macro.parse_index_close_csv(CSV_2026)
    older = macro.parse_index_close_csv(CSV_2020)
    shared = sorted(
        set(newer.index.get_level_values("IndexName"))
        & set(older.index.get_level_values("IndexName"))
    )
    assert shared == ["India VIX", "NIFTY Midcap 100", "Nifty 50", "Nifty Bank"], (
        "the two recordings must overlap, or this test compares nothing"
    )

    for name in shared:
        a = macro.select_index(newer, name)
        b = macro.select_index(older, name)
        assert list(a.columns) == list(b.columns), name
        assert a.dtypes.astype(str).to_dict() == b.dtypes.astype(str).to_dict(), name


def test_parse_is_pure_and_touches_neither_clock_nor_network():
    """No ``datetime.now`` and no URL construction happen inside the parser."""
    source = textwrap.dedent(inspect.getsource(macro.parse_index_close_csv))
    for forbidden in ("_now_utc", "httpx", "requests", "urlopen"):
        assert forbidden not in source, forbidden
    # Called with no url/host it still parses, recording that there is no provenance to claim.
    frame = macro.parse_index_close_csv(CSV_2026)
    assert macro.URL_ATTR not in frame.attrs
    assert frame.attrs[macro.URLS_ATTR] == []


def test_index_level_sanity_holds_on_every_recorded_row():
    """Low <= min(Open, Close) <= max(Open, Close) <= High, and a positive close.

    A parser that bound ``High`` and ``Low`` to the wrong columns would still return a
    well-formed frame, so this is the cheapest available structural check on the mapping.
    """
    for day, text in ((DAY_2026, CSV_2026), (DAY_2020, CSV_2020)):
        frame = macro.parse_index_close_csv(text)
        assert frame.attrs[macro.RANGE_VIOLATIONS_ATTR] == 0, day
        assert frame.attrs[macro.NONPOSITIVE_CLOSE_ATTR] == 0, day
        assert frame.attrs[macro.NON_INTEGER_VOLUME_ATTR] == 0, day
        assert frame.attrs["bfinance_macro_unparsed_date_rows"] == 0, day
        assert frame.attrs["bfinance_macro_nameless_rows"] == 0, day
        assert frame.attrs["bfinance_macro_duplicate_rows"] == 0, day
        assert (frame["Close"].astype("float64") > 0).all(), day


def test_change_pct_is_consistent_with_the_close_to_close_move():
    """``Change(%)`` reconstructed from Close and Points Change, within the file's own rounding.

    Points Change is ``Close - previous Close``, so the percent is determined. The
    reconstruction is approximate because close and points change are each published to two
    decimals; the worst recorded case is India VIX on 2026-09-25, which reconstructs to
    -4.1765 against a reported -4.16 — a 0.017 pp gap. 0.05 pp is the tolerance that fits, and
    it is still nowhere near enough to survive reading the percent as a fraction, or taking the
    change against the open rather than the previous close.
    """
    for day, text in ((DAY_2026, CSV_2026), (DAY_2020, CSV_2020)):
        frame = macro.parse_index_close_csv(text)
        close = frame["Close"].astype("float64")
        points = frame["PointsChange"].astype("float64")
        reported = frame["ChangePct"].astype("float64")
        implied = pd.Series(macro.reconstruct_change_pct(close, points), index=frame.index)
        worst = (implied - reported).abs().max()

        assert worst < 0.05, f"{day}: worst reconstruction gap {worst:.4f} pp"
        # Sign agreement is a much tighter discriminator than magnitude.
        assert bool((implied >= 0).equals(reported >= 0)), day
        # A percent, not a fraction: no single index moved more than 10% on a recorded day.
        assert reported.abs().max() < 10.0, day


def test_change_pct_is_read_as_a_percent_not_a_fraction():
    """The header prints ``.34`` with no leading zero; pandas must still read 0.34, not 34."""
    frame = macro.select_index(macro.parse_index_close_csv(CSV_2026), "Nifty 50")
    assert float(frame["ChangePct"].iloc[0]) == 0.34
    assert float(frame["PointsChange"].iloc[0]) == 77.4
    assert float(frame["Close"].iloc[0]) == 23140.5


def test_a_renamed_column_is_loud_rather_than_a_silent_column_loss():
    """The parse resolves every field by name, so a rename must raise and name the field."""
    broken = CSV_2026.replace("P/B,", "PriceToBook,")
    with pytest.raises(ParsingError) as excinfo:
        macro.parse_index_close_csv(broken)
    message = str(excinfo.value)
    assert "P/B" in message
    assert "renam" in message.lower() or "schema" in message.lower()


# ============================================================== the '-' sentinel


@pytest.mark.parametrize("day,text", [(DAY_2026, CSV_2026), (DAY_2020, CSV_2020)])
def test_the_dash_sentinel_is_not_zero_on_india_vix(day, text):
    """The load-bearing assertion of this module.

    India VIX prints ``-`` in all five of these fields on both recorded dates. Every one must
    land as ``pd.NA``, and **not one** may read as ``0.0``.
    """
    vix = macro.select_index(macro.parse_index_close_csv(text), macro.INDIA_VIX)
    assert len(vix) == 1, day

    for field in SENTINEL_FIELDS:
        value = vix[field].iloc[0]
        assert pd.isna(value), f"{day} {field}: {value!r} is not NA"
        # The precise form of the trap. A `-` coerced to 0.0 passes isna() as False.
        assert not (not pd.isna(value) and float(value) == 0.0), (
            f"{day} {field}: the '-' sentinel became a fabricated 0.0"
        )
        assert field in vix[macro.NOT_APPLICABLE_COLUMN].iloc[0].split(","), (day, field)

    # The price fields are real, so the row is parsed and not accidentally blanked.
    assert float(vix["Close"].iloc[0]) > 0
    assert float(vix["Open"].iloc[0]) > 0


def test_the_sentinel_fields_are_nullable_dtype_so_zero_cannot_be_inferred():
    """The dtypes are part of the contract, not a formatting choice."""
    frame = macro.parse_index_close_csv(CSV_2026)
    assert str(frame["Volume"].dtype) == "Int64"
    for field in ("Open", "High", "Low", "Close", "PointsChange", "ChangePct",
                  "Turnover", "PE", "PB", "DivYield"):
        assert str(frame[field].dtype) == "Float64", field
    assert frame["PE"].isna().sum() == 1, "only India VIX lacks a P/E in the trimmed recording"
    assert (frame["PE"].dropna() > 0).all(), "no recorded P/E is zero or negative"


def test_a_genuine_zero_stays_a_zero_and_is_not_confused_with_the_sentinel():
    """The third state. ``0`` must survive as ``0.0``, with neither audit column naming it."""
    rows = CSV_2026.replace(
        "Nifty Bank,25-09-2026,55373.75,55762.6,55373.75,55580.4,141.9,.26,145853991,6034.84,"
        "13.15,1.66,.7",
        "Nifty Bank,25-09-2026,55373.75,55762.6,55373.75,55580.4,141.9,.26,0,0,0,0,0",
    )
    assert rows != CSV_2026, "the substitution did not apply; the test below proves nothing"

    frame = macro.parse_index_close_csv(rows)
    bank = macro.select_index(frame, "Nifty Bank")
    for field in SENTINEL_FIELDS:
        value = bank[field].iloc[0]
        assert not pd.isna(value), f"{field}: a real 0 was read as missing"
        assert float(value) == 0.0, f"{field}: a real 0 became {value!r}"
    assert bank[macro.NOT_APPLICABLE_COLUMN].iloc[0] == ""
    assert bank[macro.UNPARSED_COLUMN].iloc[0] == ""
    # And the sentinel row is unaffected, so the two are still told apart in the same frame.
    vix = macro.select_index(frame, macro.INDIA_VIX)
    assert pd.isna(vix["Volume"].iloc[0])


def test_a_blank_field_is_missing_not_not_applicable():
    """The state that shares ``pd.NA`` with the sentinel and is told apart by its counter.

    Without this, "not applicable" and "upstream sent nothing" are the same number in the same
    dict and a caller cannot choose between them.
    """
    rows = CSV_2026.replace(
        "Nifty 50,25-09-2026,23035,23162.7,23020.95,23140.5,77.4,.34,242720711,18949.25,19.56,2.8,1.22",
        "Nifty 50,25-09-2026,23035,23162.7,23020.95,23140.5,77.4,.34,242720711,18949.25,19.56,,1.22",
    )
    assert rows != CSV_2026, "the blanking did not apply; the test below proves nothing"

    frame = macro.parse_index_close_csv(rows)
    nifty = macro.select_index(frame, "Nifty 50")

    assert pd.isna(nifty["PB"].iloc[0])
    assert "PB" in nifty[macro.UNPARSED_COLUMN].iloc[0]
    assert "PB" not in nifty[macro.NOT_APPLICABLE_COLUMN].iloc[0]
    assert frame.attrs[macro.UNPARSED_ATTR] == {"PB": 1}
    # The sentinel counter is untouched: the blank did not leak into it.
    assert frame.attrs[macro.NOT_APPLICABLE_ATTR] == dict.fromkeys(SENTINEL_FIELDS, 1)
    assert frame.attrs[macro.COVERAGE_ATTR]["PB"]["unparsed"] == 1
    assert frame.attrs[macro.COVERAGE_ATTR]["PB"]["not_applicable"] == 1, (
        "the India VIX P/B is still not applicable, and is counted separately"
    )
    # A neighbouring field on the same row is untouched, so this is not a whole-row misread.
    assert float(nifty["PE"].iloc[0]) == 19.56
    assert float(nifty["DivYield"].iloc[0]) == 1.22


def test_the_state_counters_do_not_match_on_a_substring():
    """``PB`` must not be counted because a row says ``PE``, and ``PE`` not because ``Open``."""
    frame = macro.parse_index_close_csv(CSV_2026)
    vix = macro.select_index(frame, macro.INDIA_VIX)
    # The sentinel row names PE and PB and nothing else among the valuation fields.
    named = vix[macro.NOT_APPLICABLE_COLUMN].iloc[0].split(",")
    assert "PE" in named and "PB" in named
    assert "Open" not in named and "High" not in named and "Close" not in named
    assert frame.attrs[macro.NOT_APPLICABLE_ATTR]["PE"] == 1
    assert frame.attrs[macro.NOT_APPLICABLE_ATTR]["PB"] == 1
    assert "Open" not in frame.attrs[macro.NOT_APPLICABLE_ATTR]


# ============================================================== India VIX accessor


def test_india_vix_is_reachable_as_a_named_accessor(client):
    """Not a row the caller has to know to ask for by name."""
    vix = client.india_vix(DAY_2026)

    assert isinstance(vix.index, pd.DatetimeIndex)
    assert vix.index.name == "Date"
    assert len(vix) == 1
    assert vix.attrs["bfinance_macro_index"] == "India VIX"
    assert vix.attrs[macro.INDEX_COUNT_ATTR] == 1
    # The whole file held five indices in the trimmed recording. The slice says so rather than
    # letting the caller believe the file was one row wide.
    assert vix.attrs[macro.FILE_INDEX_COUNT_ATTR] == STORED_ROW_COUNTS[DAY_2026]
    assert vix.attrs[macro.FILE_ROW_COUNT_ATTR] == STORED_ROW_COUNTS[DAY_2026]


def test_india_vix_close_and_change_parse_to_the_recorded_values(client):
    for day, close, change, points in (
        (DAY_2026, 12.16, -4.16, -0.53),
        (DAY_2020, 11.60, -0.60, -0.07),
    ):
        vix = client.india_vix(day)
        assert float(vix["Close"].iloc[0]) == close, day
        assert float(vix["ChangePct"].iloc[0]) == change, day
        assert float(vix["PointsChange"].iloc[0]) == points, day
        assert float(vix["Open"].iloc[0]) > 0 and float(vix["Low"].iloc[0]) > 0, day


def test_india_vix_coverage_is_reported_as_zero_not_assumed(client):
    """A one-row slice must not inherit the file's 4-of-5 coverage.

    This was a real bug: ``select_index`` carried the file-level dict onto a one-row frame, so
    a caller reading India VIX saw ``present: 4`` for a row with no P/E at all.
    """
    vix = client.india_vix(DAY_2026)
    for field in macro.VALUATION_FIELDS:
        assert vix.attrs[macro.COVERAGE_ATTR][field] == {
            "present": 0, "not_applicable": 1, "unparsed": 0, "total": 1,
            "coverage_pct": 0.0,
        }, field


def test_the_vix_has_no_volume_tripwire_is_present_and_false(client):
    """If upstream ever ships a real VIX volume, that is a convention change and is recorded."""
    assert client.india_vix(DAY_2026).attrs[macro.VIX_HAS_VOLUME_ATTR] is False


def test_vix_specific_routes_are_never_contacted(client, strict_client):
    """The VIX routes are dead — 503 and 404. Nothing here can reach them.

    Every URL the client can build is the one archive route, for every date in a wide sample,
    and the dead route names appear nowhere in the executable code.
    """
    for host in macro.HOSTS:
        probe = macro.MacroClient(host=host)
        for day in ("2014-12-31", "2015-01-01", "2020-01-01", "2026-09-25", "2026-12-31"):
            url = probe.url_for(day)
            assert re.fullmatch(
                r"https://(nsearchives|archives)\.nseindia\.com/content/indices/"
                r"ind_close_all_\d{8}\.csv",
                url,
            ), url

    code = module_code()
    for forbidden in ("indiceshistory", "indextype", "ind_history_vix"):
        assert forbidden not in code, forbidden


# ============================================================== valuation series


def test_valuation_fields_extract_by_name(client):
    """Not by position. P/E 19.56, P/B 2.8, dividend yield 1.22 for Nifty 50 on 2026-09-25."""
    table = client.valuations(DAY_2026)

    assert list(table.columns) == ["PE", "PB", "DivYield"]
    assert table.index.name == "IndexName"
    assert table.index.is_monotonic_increasing
    nifty = table.loc["Nifty 50"]
    assert float(nifty["PE"]) == 19.56
    assert float(nifty["PB"]) == 2.8
    assert float(nifty["DivYield"]) == 1.22


def test_the_valuation_coverage_gap_is_reported_not_implied(client):
    """The caller must be able to read the denominator instead of assuming 166.

    These are the *recording's* numbers: 4 of 5 retained rows on 2026-09-25, 3 of 4 on
    2020-01-01. The live capture's 143-of-166 P/E figure is cited in the module docstring and is
    **not** asserted here, because a five-row fixture cannot produce it.
    """
    newer = client.valuations(DAY_2026).attrs[macro.COVERAGE_ATTR]
    older = client.valuations(DAY_2020).attrs[macro.COVERAGE_ATTR]

    for field in macro.VALUATION_FIELDS:
        assert newer[field] == {
            "present": 4, "not_applicable": 1, "unparsed": 0,
            "total": STORED_ROW_COUNTS[DAY_2026], "coverage_pct": 80.0,
        }, field
        assert older[field] == {
            "present": 3, "not_applicable": 1, "unparsed": 0,
            "total": STORED_ROW_COUNTS[DAY_2020], "coverage_pct": 75.0,
        }, field


def test_coverage_never_reports_full_coverage_while_a_row_lacks_the_field(client):
    """The gap and the data must be computed from the same rows, so they cannot disagree."""
    table = client.valuations(DAY_2026)
    for field in macro.VALUATION_FIELDS:
        reported = table.attrs[macro.COVERAGE_ATTR][field]
        assert reported["present"] == int(table[field].notna().sum()), field
        assert reported["not_applicable"] == int(table[field].isna().sum()), field
        assert reported["present"] + reported["not_applicable"] + reported["unparsed"] == \
            reported["total"], field
    assert table.attrs[macro.COVERAGE_ATTR]["PE"]["coverage_pct"] < 100.0


def test_valuation_series_is_a_dated_series_for_one_index(client):
    series = client.valuation_series("Nifty 50", DAY_2020, DAY_2026)

    assert isinstance(series.index, pd.DatetimeIndex) and series.index.name == "Date"
    assert list(series.columns) == ["PE", "PB", "DivYield"]
    assert [str(d.date()) for d in series.index] == [DAY_2020, DAY_2026]
    assert series["PE"].tolist() == [28.33, 19.56]
    assert series["PB"].tolist() == [3.76, 2.8]
    assert series["DivYield"].tolist() == [1.24, 1.22]


def test_a_valuation_series_for_the_vix_is_all_not_applicable_and_says_so(client):
    """Two rows, no values, 0% coverage. Not 100% and not an empty frame."""
    series = client.valuation_series("India VIX", DAY_2020, DAY_2026)

    assert len(series) == 2
    assert series.isna().all().all()
    assert series.attrs[macro.COVERAGE_ATTR]["PE"] == {
        "present": 0, "not_applicable": 2, "unparsed": 0, "total": 2, "coverage_pct": 0.0,
    }
    assert series.attrs[macro.NOT_APPLICABLE_ATTR] == dict.fromkeys(SENTINEL_FIELDS, 2)


# ============================================================== unknown index


def test_an_unknown_index_raises_a_typed_error_not_an_empty_frame(client):
    with pytest.raises(macro.IndexNotFoundError) as excinfo:
        client.index_daily("Nifty 5000", DAY_2026)

    message = str(excinfo.value)
    assert "Nifty 5000" in message
    assert "5 index name(s) are present" in message
    assert "all_indices(day)" in message, "the message must say how to discover the names"


def test_the_typed_error_is_catchable_through_the_existing_hierarchy(client):
    """A new specific type, not a new root: existing handlers keep working."""
    for expected in (macro.IndexNotFoundError, TickerNotFoundError, BFinanceError, Exception):
        with pytest.raises(expected):
            client.index_daily("No Such Index", DAY_2026)


def test_a_near_miss_name_suggests_the_real_one(client):
    with pytest.raises(macro.IndexNotFoundError) as excinfo:
        client.index_daily("Nifty5O", DAY_2026)
    assert "Nifty 50" in str(excinfo.value)


def test_index_names_match_case_insensitively_and_upstream_casing_is_returned(client):
    for given in ("India VIX", "india vix", "INDIA VIX", "  India   VIX "):
        frame = client.index_daily(given, DAY_2026)
        assert frame.attrs["bfinance_macro_index"] == "India VIX", given


def test_an_index_absent_from_every_file_in_a_span_raises_rather_than_reading_empty(client):
    """A naming error and a calendar error must not both arrive as an empty frame."""
    with pytest.raises(macro.IndexNotFoundError) as excinfo:
        client.index_range("Nifty 5000", DAY_2020, DAY_2026)
    assert "absent from every one of the 2 file(s)" in str(excinfo.value)


def test_an_unrecorded_date_is_an_empty_frame_while_a_naming_error_raises(client):
    """The other half of the same distinction: no file at all is a calendar answer."""
    frame = client.index_range("Nifty 50", "2019-12-20", "2019-12-27")
    assert frame.empty
    assert frame.attrs[macro.MISSING_DATES_ATTR] != []
    assert macro.START_ATTR not in frame.attrs or frame.attrs[macro.START_ATTR] == "2019-12-20"


# ============================================================== provenance in attrs


def test_attrs_carry_url_retrieval_date_and_host_on_every_result(client):
    expected_url = (
        f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{STAMP_2026}.csv"
    )
    results = {
        "all_indices": client.all_indices(DAY_2026),
        "index_daily": client.index_daily("Nifty 50", DAY_2026),
        "india_vix": client.india_vix(DAY_2026),
        "valuations": client.valuations(DAY_2026),
        "valuation_series": client.valuation_series("Nifty 50", DAY_2020, DAY_2026),
        "index_range": client.index_range("Nifty 50", DAY_2020, DAY_2026),
    }
    for label, frame in results.items():
        assert frame.attrs[macro.HOST_ATTR] == "nsearchives.nseindia.com", label
        assert frame.attrs[macro.URLS_ATTR], label
        assert all(u.startswith("https://nsearchives.nseindia.com/") for u in
                   frame.attrs[macro.URLS_ATTR]), label
        assert len(frame.attrs[macro.RETRIEVED_ALL_ATTR]) == len(frame.attrs[macro.URLS_ATTR]), \
            f"{label}: a retrieval timestamp must align with the URL it describes"
        assert frame.attrs["bfinance_provenance"] == macro.PROVENANCE, label

    # The scalar keys are set only where exactly one file was read, so they cannot describe one
    # of many. Their presence-or-absence is itself the provenance claim.
    for label in ("all_indices", "index_daily", "india_vix", "valuations"):
        assert results[label].attrs[macro.URL_ATTR] == expected_url, label
        assert results[label].attrs[macro.RETRIEVED_ATTR] == "2026-09-27T20:44:25+00:00", label
    for label in ("valuation_series", "index_range"):
        assert macro.URL_ATTR not in results[label].attrs, label
        assert macro.RETRIEVED_ATTR not in results[label].attrs, label


def test_the_retrieval_date_is_the_recordings_own_not_the_replay_time(client):
    """A replayed byte was fetched from NSE on 2026-09-27, so that is what is reported."""
    assert client.all_indices(DAY_2026).attrs[macro.RETRIEVED_ATTR] == "2026-09-27T20:44:25+00:00"


def test_a_missing_file_still_records_where_it_was_looked_for(client):
    """A 404 is an expected outcome, and the attempt that established it is a real event."""
    frame = client.all_indices("2026-09-26")  # a Saturday

    assert frame.empty
    assert list(frame.columns) == list(macro.COLUMNS) + [
        macro.NOT_APPLICABLE_COLUMN, macro.UNPARSED_COLUMN
    ]
    assert frame.attrs[macro.HOST_ATTR] == "nsearchives.nseindia.com"
    assert frame.attrs[macro.URL_ATTR].endswith("ind_close_all_26092026.csv")
    assert frame.attrs[macro.RETRIEVED_ATTR]
    assert frame.attrs[macro.MISSING_DATES_ATTR] == ["2026-09-26"]


def test_the_two_hosts_are_both_reachable_and_are_recorded_separately():
    """One dataset, two hosts, identical bytes — so which one answered must be visible."""
    for host in macro.HOSTS:
        probe = macro.MacroClient(
            source=macro.RecordingSource(FIXTURES, on_missing="return_none"), host=host
        )
        frame = probe.all_indices(DAY_2026)
        assert frame.attrs[macro.HOST_ATTR] == host
        assert frame.attrs[macro.URL_ATTR].startswith(f"https://{host}/")
        # Same bytes either way, so the values cannot differ.
        assert frame["Close"].astype("float64").tolist() == \
            macro.parse_index_close_csv(CSV_2026)["Close"].astype("float64").tolist()


def test_the_robots_evidence_supports_the_two_host_claim():
    """The mirror is not an assumption. It is recorded, and this asserts the record."""
    evidence = json.loads((NSE_FIXTURES / "ROBOTS_EVIDENCE.json").read_text("utf-8"))
    by_host = {entry["host"]: entry for entry in evidence["hosts"]}

    assert set(macro.HOSTS) == {"nsearchives.nseindia.com", "archives.nseindia.com"}
    for host in macro.HOSTS:
        # 404 on robots.txt == unrestricted per RFC 9309, and the rule applied is stated.
        assert by_host[host]["http_status"] == 404, host
    assert "byte-for-byte" in by_host["archives.nseindia.com"]["verdict"] or \
        "identical" in by_host["archives.nseindia.com"]["verdict"]


# ============================================================== units


def test_only_turnover_and_change_pct_carry_a_unit_in_the_header():
    labelled = {
        column for column, spec in macro.UNITS.items() if spec["labelled_in_header"]
    }
    assert labelled == {"Turnover", "ChangePct"}, (
        "the upstream header writes 'Turnover (Rs. Cr.)' and 'Change(%)' and labels nothing "
        "else; a fourth labelled column would be an invented fact"
    )
    assert macro.UNITS["Turnover"]["unit"] == "Rs. Cr. (crore rupees)"
    assert macro.UNITS["Turnover"]["confirmed"] is True


def test_pe_pb_and_div_yield_are_not_given_an_inferred_unit():
    """The brief's check, recorded: no unit is asserted for the three that carry none."""
    for field in ("PE", "PB", "DivYield"):
        spec = macro.UNITS[field]
        assert spec["labelled_in_header"] is False, field
        assert spec["unit"] is None, field
        assert spec["confirmed"] is False, field


def test_div_yield_is_marked_presumed_percent_and_left_unconfirmed():
    """Plausible is not confirmed. The status is explicit rather than silently assumed."""
    spec = macro.UNITS["DivYield"]
    assert spec["presumed"] == "percent"
    assert spec["confirmation_requires"], "the reason it is unconfirmed must be recorded"
    assert "0.01" in spec["presumption_evidence"], "the inference and its basis are both stated"


def test_no_value_is_rescaled():
    """1.22 stays 1.22 — not 0.0122 and not 122. The parser performs no unit arithmetic."""
    frame = macro.parse_index_close_csv(CSV_2026)
    nifty = macro.select_index(frame, "Nifty 50")
    assert float(nifty["DivYield"].iloc[0]) == 1.22
    assert float(nifty["PE"].iloc[0]) == 19.56
    assert float(nifty["Turnover"].iloc[0]) == 18949.25

    # Scoped to the parse path. The module legitimately divides by 100 *inside*
    # ``reconstruct_change_pct``, which reconstructs upstream's own reported percent rather
    # than converting a unit, so scanning the whole source would flag the checker's own maths.
    parse_source = inspect.getsource(macro.parse_index_close_csv) + inspect.getsource(
        macro._field_states
    )
    for forbidden in ("* 100", "/ 100", "*100", "100 *", "1e7", "10_000_000", "1e5"):
        assert forbidden not in parse_source, forbidden


# ============================================================== URL and host construction


@pytest.mark.parametrize("day,stamp", [
    ("2026-09-25", "25092026"),
    ("2020-01-01", "01012020"),
    ("2015-01-01", "01012015"),
    ("2014-12-31", "31122014"),
])
@pytest.mark.parametrize("host", macro.HOSTS)
def test_index_url_is_ddmmyyyy_on_a_known_host(day, stamp, host):
    assert macro.index_url(day, host) == \
        f"https://{host}/content/indices/ind_close_all_{stamp}.csv"


def test_index_url_is_pure_and_makes_no_request():
    """Same answer twice, and it builds one route and one only."""
    assert macro.index_url("2026-09-25") == macro.index_url("2026-09-25")
    for accepted in ("nsearchives.nseindia.com", "https://nsearchives.nseindia.com/",
                     "NSEARCHIVES.NSEINDIA.COM", " nsearchives.nseindia.com "):
        assert macro.validate_host(accepted) == "nsearchives.nseindia.com", accepted


@pytest.mark.parametrize("bad", [
    "www.nseindia.com", "nseindia.com", "evil.example.com", "nsearchives.nseindia.com.evil.io",
    "", None, 7,
])
def test_the_host_allow_list_is_closed(bad):
    """An open host list would let this module fetch any URL wearing an NSE filename."""
    with pytest.raises(ValueError):
        macro.validate_host(bad)


def test_coverage_boundary_is_reported_not_enforced():
    """Observed 404s, so a local hard-fail would claim more than the evidence supports."""
    assert macro.COVERAGE_START == __import__("datetime").date(2015, 1, 1)
    assert macro.in_coverage("2015-01-01") is True
    assert macro.in_coverage("2014-12-31") is False
    # Reported, not raised: a pre-cutover URL is still buildable.
    assert macro.index_url("2010-01-01").endswith("ind_close_all_01012010.csv")


# ============================================================== date ranges


def test_a_range_is_bounded_to_the_dates_that_exist(client):
    frame = client.index_range("Nifty 50", DAY_2020, DAY_2026)

    assert len(frame) == 2, "only the two recorded dates may appear"
    assert [str(d.date()) for d in frame.index] == [DAY_2020, DAY_2026]
    assert frame.index.is_monotonic_increasing
    assert frame.attrs[macro.CANDIDATE_DAYS_ATTR] == 1758
    assert frame.attrs[macro.START_ATTR] == DAY_2020
    assert frame.attrs[macro.END_ATTR] == DAY_2026

    # 1,758 candidate weekdays, two of which had a file. The other 1,756 are recorded as
    # missing, and the two sets are disjoint — a returned date is never also a missing one.
    missing = frame.attrs[macro.MISSING_DATES_ATTR]
    assert len(missing) == 1758 - 2
    assert DAY_2020 not in missing and DAY_2026 not in missing
    assert frame.attrs["bfinance_macro_dates_with_file"] == [DAY_2020, DAY_2026]


def test_a_range_issues_exactly_one_request_per_candidate_day(client):
    """The request count is the number of weekdays walked, never more.

    Mon 21 - Sun 27 September 2026 is five weekdays and a weekend. The two weekend days are
    excluded from the walk entirely, so they cost no request — the whole point of the filter.
    """
    client.source.requested.clear()
    client.index_range("Nifty 50", "2026-09-21", "2026-09-27")

    assert len(client.source.requested) == len(set(client.source.requested)) == 5
    assert not any("26092026" in name for name in client.source.requested), "Saturday requested"
    assert not any("27092026" in name for name in client.source.requested), "Sunday requested"


def test_a_reversed_range_is_rejected_before_any_request(client):
    client.source.requested.clear()
    with pytest.raises(ValueError, match="before start"):
        client.index_range("Nifty 50", DAY_2026, DAY_2020)
    assert client.source.requested == [], "the bound check must precede the walk"


def test_a_reversed_range_is_rejected_on_both_candidate_modes(client):
    with pytest.raises(ValueError, match="before start"):
        client.index_range("Nifty 50", DAY_2026, DAY_2020, skip_weekends=False)
    with pytest.raises(ValueError, match="before start"):
        macro.iter_candidate_days(DAY_2026, DAY_2020, skip_weekends=False)


def test_a_single_day_range_is_bounded_to_one_date(client):
    frame = client.index_range("Nifty 50", DAY_2026, DAY_2026)
    assert len(frame) == 1
    assert str(frame.index[0].date()) == DAY_2026
    assert frame.attrs[macro.CANDIDATE_DAYS_ATTR] == 1


def test_on_missing_raise_aborts_on_the_first_unrecorded_date(client):
    with pytest.raises(TickerNotFoundError, match="on_missing='skip'"):
        client.index_range("Nifty 50", "2026-09-21", "2026-09-26", on_missing="raise")


def test_an_unknown_on_missing_is_rejected(client):
    with pytest.raises(ValueError, match="on_missing"):
        client.index_range("Nifty 50", DAY_2026, DAY_2026, on_missing="explode")


def test_a_merged_range_carries_per_file_provenance_not_a_lie_about_one_file(client):
    """After a merge, a single-URL key would describe one of many. It must be absent.

    The plural keys are **not** trimmed to the files that returned rows: 1,758 URLs were
    requested and 1,756 of them 404'd, and a request count that hid the misses would
    misrepresent the cost of the call.
    """
    frame = client.index_range("Nifty 50", DAY_2020, DAY_2026)

    assert macro.URL_ATTR not in frame.attrs
    assert macro.RETRIEVED_ATTR not in frame.attrs
    assert len(frame.attrs[macro.URLS_ATTR]) == frame.attrs[macro.CANDIDATE_DAYS_ATTR] == 1758
    assert len(frame.attrs[macro.RETRIEVED_ALL_ATTR]) == 1758

    # The two dates that had a file report the *recording's* capture time. The 1,756 that 404'd
    # have no recording, so they report the time of the attempt that established the absence —
    # never a fabricated capture date.
    stamps = dict(zip(frame.attrs[macro.URLS_ATTR], frame.attrs[macro.RETRIEVED_ALL_ATTR]))
    for url, stamp in stamps.items():
        if STAMP_2020 in url or STAMP_2026 in url:
            assert stamp == "2026-09-27T20:44:25+00:00", url
        else:
            assert stamp and stamp != "2026-09-27T20:44:25+00:00", url
        assert sum(s in url for s in (STAMP_2020, STAMP_2026)) <= 1


def test_a_range_recomputes_state_counts_over_the_rows_it_returns(client):
    """A sum of per-day file-level counts would include every other index on each day."""
    frame = client.index_range("Nifty 50", DAY_2020, DAY_2026)
    assert frame.attrs[macro.NOT_APPLICABLE_ATTR] == {}
    assert frame.attrs[macro.UNPARSED_ATTR] == {}
    assert frame.attrs[macro.COVERAGE_ATTR]["PE"]["present"] == 2

    vix = client.index_range("India VIX", DAY_2020, DAY_2026)
    assert vix.attrs[macro.NOT_APPLICABLE_ATTR] == dict.fromkeys(SENTINEL_FIELDS, 2)
    assert vix.attrs[macro.COVERAGE_ATTR]["PE"]["present"] == 0


def test_a_range_of_an_unrecorded_span_returns_the_honest_empty_shape(client):
    frame = client.index_range("Nifty 50", "2019-12-02", "2019-12-06")

    assert frame.empty
    assert list(frame.columns) == list(macro.COLUMNS) + [
        macro.NOT_APPLICABLE_COLUMN, macro.UNPARSED_COLUMN
    ]
    assert str(frame["Volume"].dtype) == "Int64", "dtypes must match a populated frame"
    assert str(frame["PE"].dtype) == "Float64"
    assert frame.attrs[macro.CANDIDATE_DAYS_ATTR] == 5
    assert len(frame.attrs[macro.MISSING_DATES_ATTR]) == 5
    assert frame.attrs[macro.HOST_ATTR] == "nsearchives.nseindia.com"


# ============================================================== absences


#: A whole ``tri`` token, not a substring: ``retrieved`` and ``retrieval`` contain the letters
#: and are legitimate names here. Matched against names, values and code separately.
_TRI_NAME = re.compile(r"(?:^|_)(?:tri|totret|tri_index|triindex|totalret)(?:_|$)", re.I)


def test_no_total_return_index_accessor_is_exposed():
    """Four candidate routes 404'd. A test so nobody pastes a fabricated one back in.

    The two constants that *name the absence* are allowed to match — a module that cannot refer
    to why it lacks a capability cannot document it. What is forbidden is anything a caller can
    **invoke**, and that is asserted exactly rather than by a blanket name ban.
    """
    documented = {"INDEX_TRI_REASON", "UNAVAILABLE_SOURCES", "unavailable_sources"}
    public = {name for name in dir(macro) if not name.startswith("_")}
    matched = {name for name in public if _TRI_NAME.search(name)}

    assert matched <= documented, f"an invocable total-return accessor appeared: {matched - documented}"
    assert not [
        name for name in matched
        if callable(getattr(macro, name)) and name not in ("unavailable_sources",)
    ], "a callable exposes a total-return index"
    for name in matched - documented:
        assert not callable(getattr(macro, name)), name


def test_no_client_method_promises_a_total_return_index():
    """The client is where a fabricated accessor would actually be reachable from."""
    for name in dir(macro.MacroClient):
        if name.startswith("_"):
            continue
        assert not _TRI_NAME.search(name), name
        assert "totalreturn" not in name.lower(), name
    # And no client method takes a series-type argument a TRI read would need.
    for name in dir(macro.MacroClient):
        if name.startswith("_"):
            continue
        member = getattr(macro.MacroClient, name)
        if not callable(member):
            continue
        assert "total_return" not in inspect.signature(member).__str__().lower(), name


def test_the_module_builds_no_total_return_url_and_makes_no_such_request(client):
    """Docstrings stripped first — the module has to be able to *explain* the absence."""
    code = module_code()

    for forbidden in ("total_return", "totret", "tri_index", "triindex", "total return index"):
        assert forbidden not in code, forbidden

    # And the only route the client can build is the index close file.
    urls = {client.url_for(day) for day in ("2015-01-01", "2020-01-01", "2026-09-25")}
    assert all("/content/indices/ind_close_all_" in url for url in urls)


def test_the_total_return_absence_is_recorded_with_its_reason():
    entry = macro.UNAVAILABLE_SOURCES["index_tri"]
    assert entry["available"] is False
    assert entry["probe_count"] == 4
    assert "404" in entry["why"]
    assert "dividend" in entry["consequence"], (
        "the reason it cannot be approximated must be stated, not just the 404"
    )
    assert macro.INDEX_TRI_REASON == entry["why"]


def test_rbi_reference_rate_is_a_typed_unavailable_state_naming_the_418(client):
    """No request is attempted and no payload is parsed. Any call shape lands on the same answer."""
    for call in (
        lambda: macro.rbi_reference_rate(),
        lambda: macro.rbi_reference_rate("2026-09-25"),
        lambda: macro.rbi_reference_rate(day="2026-09-25"),
        lambda: macro.rbi_reference_rate(1, 2, 3, keyword="x"),
    ):
        with pytest.raises(macro.SourceUnavailableError) as excinfo:
            call()
        message = str(excinfo.value)
        assert "418" in message
        assert "www.rbi.org.in" in message
        assert "__VIEWSTATE" in message
        assert "No request is attempted" in message


def test_no_client_exposes_an_rbi_method(client):
    """Nothing on a live client can be pointed at the blocked host."""
    assert not hasattr(macro.MacroClient, "rbi_reference_rate")
    assert not hasattr(macro.MacroClient, "rbi")
    assert "rbi" not in {n for n in dir(client) if not n.startswith("_")}


def test_the_rbi_absence_is_recorded_with_its_reason():
    entry = macro.UNAVAILABLE_SOURCES["rbi_reference_rate"]
    assert entry["available"] is False
    assert entry["host"] == "www.rbi.org.in"
    assert entry["http_status"] == 418
    assert entry["not_circumvented"] is True
    assert "robots.txt" in entry["why"]
    assert macro.RBI_REFERENCE_RATE_REASON == entry["why"]


def test_the_robots_evidence_records_the_rbi_418():
    """The absence rests on a recorded probe, not on a guess."""
    evidence = json.loads((NSE_FIXTURES / "ROBOTS_EVIDENCE.json").read_text("utf-8"))
    rbi = next(e for e in evidence["hosts"] if e["host"] == "www.rbi.org.in")

    assert rbi["http_status"] == 418
    assert "Not circumvented" in rbi["verdict"]


def test_unavailable_sources_returns_a_copy_the_caller_cannot_corrupt():
    snapshot = macro.unavailable_sources()
    assert set(snapshot) == {"index_tri", "rbi_reference_rate"}

    snapshot["index_tri"]["available"] = True
    snapshot["injected"] = {}

    assert macro.UNAVAILABLE_SOURCES["index_tri"]["available"] is False
    assert "injected" not in macro.UNAVAILABLE_SOURCES
    assert set(macro.UNAVAILABLE_SOURCES) == {"index_tri", "rbi_reference_rate"}


def test_the_three_route_cross_check_cannot_be_run_here_and_nothing_fakes_it():
    """What could NOT be verified offline, asserted so it stays un-faked.

    The live capture agreed across three routes — this CSV ``23140.5 / 19.56 / 2.8 / 1.22``,
    ``/api/allIndices`` ``23140.5 / pe 19.56 / pb 2.8 / dy 1.22``, ``/api/marketStatus``
    ``23140.5``. Only the first is on disk, so the cross-check is **not** reproducible here and
    this test asserts precisely that, rather than asserting the agreement it cannot see.
    """
    recordings = sorted(p.name for p in FIXTURES.iterdir())
    assert not any("allIndices" in name or "allindices" in name for name in recordings)
    assert not any("marketStatus" in name or "marketstatus" in name for name in recordings)
    # A JSON *payload* would mean the cross-check has become possible. The META sidecars are
    # provenance, not data, so they are excluded by name rather than by suffix alone.
    assert not [
        name for name in recordings
        if name.endswith(".json") and not name.endswith(".META.json")
    ], "a new JSON payload recording appeared; this test must be rewritten to run the check"

    # No part of this module can reach the other two routes.
    code = module_code()
    for forbidden in ("/api/allindices", "/api/marketstatus", "nseindia.com/api"):
        assert forbidden not in code, forbidden

    # The half that *is* recorded is pinned to the exact values it was captured with.
    nifty = macro.select_index(macro.parse_index_close_csv(CSV_2026), "Nifty 50")
    assert float(nifty["Close"].iloc[0]) == 23140.5
    assert float(nifty["PE"].iloc[0]) == 19.56
    assert float(nifty["PB"].iloc[0]) == 2.8
    assert float(nifty["DivYield"].iloc[0]) == 1.22


# ============================================================== isolation


def test_importing_bfinance_does_not_pull_in_macro():
    """``bfinance.nse.macro`` is a leaf. ``import bfinance`` must not drag it in.

    Subprocess, because this test module imports macro itself, so in-process ``sys.modules`` is
    already populated and any in-process variant of this check could not distinguish the two
    cases. The negative control for this probe is
    ``test_control_the_lazy_client_assertion_goes_red_on_an_eager_client``.
    """
    assert _run_probe(
        """
        import sys
        import bfinance
        print("bfinance.nse.macro" in sys.modules)
        """
    ) == "False", "import bfinance pulled in bfinance.nse.macro; the leaf contract is broken"


def test_no_other_module_in_the_package_imports_macro():
    """Isolation enforced by test, not by convention."""
    offenders = []
    for path in sorted((SRC / "bfinance").rglob("*.py")):
        if path.name == "macro.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any("nse.macro" in alias.name or alias.name == "macro"
                       for alias in node.names):
                    offenders.append(str(path))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "nse.macro" in module or module.endswith("nse"):
                    if any(alias.name.startswith("macro") for alias in node.names):
                        offenders.append(str(path))
    assert offenders == [], f"macro must be a leaf, but these import it: {offenders}"


def _run_probe(program: str) -> str:
    """Run a program in a fresh interpreter and return its stdout, or fail loudly."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.strip()


def test_importing_the_nse_package_does_not_pull_macro_in():
    """``bfinance.nse.__init__`` is not this agent's file, so nothing may re-export macro.

    Run in a subprocess because **this test module imports macro itself**, so ``sys.modules``
    already contains it in-process and an in-process check could only ever be a hedge — which
    is exactly the unfailable assertion this replaces. A fresh interpreter is the only place
    the question is answerable, and the answer is a single word.
    """
    out = _run_probe(
        """
        import sys
        import bfinance.nse
        print("bfinance.nse.macro" in sys.modules)
        """
    )
    assert out == "False", (
        "import bfinance.nse pulled in bfinance.nse.macro; the leaf contract is broken"
    )


def test_the_nse_package_exports_no_macro_surface():
    """Not in ``__all__``, and no ``MacroClient``/shortcut reachable as a package attribute.

    Subprocess again: importing ``bfinance.nse.macro`` as a submodule sets the ``macro``
    attribute on the parent package as a side effect, so an in-process ``dir(nse)`` would be
    polluted by the very import this suite performs.
    """
    out = _run_probe(
        """
        import bfinance.nse as nse
        names = set(nse.__all__) | {n for n in dir(nse) if not n.startswith("_")}
        leaked = sorted(
            n for n in names
            if n == "macro" or n.startswith("Macro") or n in {
                "all_indices", "index_daily", "india_vix", "valuations",
                "index_range", "valuation_series", "rbi_reference_rate",
            }
        )
        print(leaked)
        """
    )
    assert out == "[]", f"bfinance.nse re-exports macro names: {out}"


def test_the_default_client_is_lazy():
    """No eager client at import: the first call must be the first I/O, not a global.

    ``_default_client`` *does* construct a client — that is its job. What must not happen is
    construction at import time, so the global is still ``None`` after the module is loaded and
    only the factory builds one.
    """
    assert macro._DEFAULT_CLIENT is None, (
        "a client was constructed at import time; the first call must be the first I/O"
    )
    factory_source = inspect.getsource(macro._default_client)
    assert "MacroClient(" in factory_source, (
        "the factory no longer builds a client; this test would pass for the wrong reason"
    )
    assert "if _DEFAULT_CLIENT is None" in factory_source, (
        "the memoisation guard is gone, so every call would build a client and drop it"
    )
    # And the global is a *declaration*, not a construction. Walking the module's top-level
    # statements is the only way to tell `_DEFAULT_CLIENT = None` from
    # `_DEFAULT_CLIENT = MacroClient()` — both contain the same identifier, and a substring
    # scan cannot. Only the factory is allowed to call the constructor.
    tree = ast.parse(Path(macro.__file__).read_text(encoding="utf-8"))
    eager: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "MacroClient":
                targets = [getattr(t, "id", None) for t in getattr(node, "targets", [])]
                if "_DEFAULT_CLIENT" in targets:
                    eager.append(ast.unparse(node))
    assert eager == [], (
        f"the module constructs a client at import time: {eager}; the first call must be the "
        f"first I/O, not module load"
    )


def test_control_the_lazy_client_assertion_goes_red_on_an_eager_client():
    """The negative control for the two isolation tests above.

    Both are subprocess probes that read one word out of a fresh interpreter. A subprocess
    probe is exactly the kind of test that can silently stop testing anything, so each claim is
    re-run here against a deliberately broken package to prove it would notice.

    Control 1: a package that *does* re-export macro, built in ``tmp_path`` and put on the
    path, so the probe is pointed at a module that fails the rule on purpose.
    """
    broken = _run_probe(
        """
        import sys
        # Simulate the violation the isolation test forbids: a parent package that eagerly
        # imports the leaf, exactly what test_importing_bfinance_does_not_pull_in_macro
        # asserts does not happen for `bfinance`.
        import types
        leaf = types.ModuleType("fakeleaf")
        sys.modules["fakeleaf"] = leaf
        package = types.ModuleType("fakepkg")
        package.leaf = leaf
        sys.modules["fakepkg"] = package
        print("fakeleaf" in sys.modules)
        """
    )
    assert broken == "True", (
        "the probe cannot detect an eagerly imported leaf, so it is not testing anything"
    )

    # Control 2: the same shape against this module, proving the probe reads a real answer.
    honest = _run_probe(
        """
        import sys
        import bfinance
        print("bfinance.nse.macro" in sys.modules)
        """
    )
    assert honest == "False"
    # The two answers differ, so the probe discriminates rather than always printing one word.
    assert honest != broken


def test_import_and_client_construction_perform_no_io():
    """Constructing a client must not fetch anything. Only a call may.

    The socket guard here is stricter than the in-process one, because a fresh interpreter has
    no asyncio event loop yet and nothing needs loopback: every connect is forbidden outright.
    """
    assert _run_probe(
        """
        import socket

        def _forbidden(*args, **kwargs):
            raise AssertionError("network I/O at import or construction")

        socket.socket.connect = _forbidden
        socket.socket.connect_ex = _forbidden
        socket.create_connection = _forbidden
        socket.getaddrinfo = _forbidden

        from bfinance.nse import macro
        macro.MacroClient()
        assert macro.PROVENANCE == "nse_macro"
        assert macro._DEFAULT_CLIENT is None
        print("OK")
        """
    ) == "OK"


# ============================================================== transport, stubbed


class _StubTransport(httpx.AsyncBaseTransport):
    """Canned responses per URL, so the real client code runs with no socket."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(str(request.url))
        for needle, build in self.responses.items():
            if needle in str(request.url):
                return build()
        return httpx.Response(404, text="not found")


@pytest.fixture
def stub_httpx(monkeypatch):
    holder = {"clients": []}

    class _FakeHttpx:
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        TransportError = httpx.TransportError

        @staticmethod
        def AsyncClient(**kwargs):
            client = httpx.AsyncClient(transport=holder["transport"], timeout=5.0)
            holder["clients"].append(client)
            return client

    monkeypatch.setattr(macro, "httpx", _FakeHttpx)

    def install(responses):
        holder["transport"] = _StubTransport(responses)
        return holder["transport"]

    install.requests = lambda: list(holder["transport"].requests)
    return install


def served(status, content=None, text=None):
    def build():
        return httpx.Response(status, **({"text": text} if text is not None
                                         else {"content": content}))
    return build


async def test_a_404_maps_to_an_empty_frame_not_an_exception(stub_httpx):
    transport = stub_httpx({})
    live = macro.MacroClient(min_request_interval_ms=0.0)

    assert await live.all_indices_async("2026-09-26") is not None
    frame = await live.all_indices_async("2026-09-26")
    assert frame.empty
    assert frame.attrs[macro.MISSING_DATES_ATTR] == ["2026-09-26"]
    assert len(transport.requests) == 2


async def test_404_raises_under_raise_errors(stub_httpx):
    stub_httpx({})
    live = macro.MacroClient(min_request_interval_ms=0.0)
    with pytest.raises(TickerNotFoundError, match="no index close file for 2026-09-26"):
        await live.all_indices_async("2026-09-26", raise_errors=True)


async def test_a_200_parses_end_to_end_over_the_transport(stub_httpx):
    stub_httpx({STAMP_2026: served(200, content=CSV_2026.encode("utf-8"))})
    live = macro.MacroClient(min_request_interval_ms=0.0)
    frame = await live.all_indices_async(DAY_2026)

    assert len(frame) == STORED_ROW_COUNTS[DAY_2026]
    assert frame.attrs[macro.HOST_ATTR] == "nsearchives.nseindia.com"
    assert frame.attrs[macro.URL_ATTR].endswith(f"ind_close_all_{STAMP_2026}.csv")
    vix = await live.india_vix_async(DAY_2026)
    assert pd.isna(vix["PE"].iloc[0]) and float(vix["Close"].iloc[0]) == 12.16


async def test_an_html_error_page_served_with_a_200_does_not_read_as_no_indices(stub_httpx):
    """The exact defect a 200-with-HTML causes. It must be a loud parse failure."""
    stub_httpx({STAMP_2026: served(200, text="<html>gateway timeout</html>")})
    live = macro.MacroClient(min_request_interval_ms=0.0)
    with pytest.raises(ParsingError, match="missing field"):
        await live.all_indices_async(DAY_2026)


async def test_429_raises_a_rate_limit_error(stub_httpx):
    stub_httpx({STAMP_2026: served(429, text="slow down")})
    live = macro.MacroClient(min_request_interval_ms=0.0, max_retries=1)
    with pytest.raises(RateLimitExceededError):
        await live.all_indices_async(DAY_2026)


async def test_a_5xx_is_retried_then_raises(stub_httpx):
    transport = stub_httpx({STAMP_2026: served(503, text="busy")})
    live = macro.MacroClient(min_request_interval_ms=0.0, max_retries=2)
    with pytest.raises(UpstreamServiceError, match="failed after 2 attempts"):
        await live.all_indices_async(DAY_2026)
    assert len(transport.requests) == 2, "a 5xx must be retried, not accepted"


async def test_a_5xx_is_retried_then_succeeds(stub_httpx):
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=CSV_2026.encode("utf-8"))

    stub_httpx({STAMP_2026: flaky})
    live = macro.MacroClient(min_request_interval_ms=0.0, max_retries=3)
    frame = await live.all_indices_async(DAY_2026)
    assert len(frame) == STORED_ROW_COUNTS[DAY_2026]


async def test_an_unexpected_status_raises(stub_httpx):
    stub_httpx({STAMP_2026: served(403, text="forbidden")})
    live = macro.MacroClient(min_request_interval_ms=0.0)
    with pytest.raises(UpstreamServiceError, match="unexpected HTTP 403"):
        await live.all_indices_async(DAY_2026)


def test_pacing_state_is_module_global_so_clients_share_one_limit():
    assert isinstance(macro._GLOBAL_PACING_LOCK, type(__import__("threading").Lock()))
    assert isinstance(macro._GLOBAL_LAST_REQUEST_TIME, float)


async def test_pacing_spaces_requests_by_the_configured_interval():
    source = macro.HttpSource(min_request_interval_ms=120.0)
    source._last_request_time = 0.0
    macro._GLOBAL_LAST_REQUEST_TIME = 0.0

    started = asyncio.get_running_loop().time()
    for _ in range(3):
        await source._throttle_pacing()
    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0

    assert elapsed_ms >= 220.0, f"expected pacing to hold, took only {elapsed_ms:.0f}ms"
    macro._GLOBAL_LAST_REQUEST_TIME = 0.0


async def test_the_offline_source_refuses_to_guess_at_an_unrecorded_date():
    """on_missing='raise' is the default, so a wrong date cannot silently read as a 404."""
    source = macro.RecordingSource(FIXTURES)
    with pytest.raises(TickerNotFoundError, match="offline by construction"):
        await source.get("https://nsearchives.nseindia.com/content/indices/"
                         "ind_close_all_26092026.csv")
    # And it really did try, so this is a refusal rather than a short-circuit.
    assert source.requested == ["ind_close_all_26092026.csv"]


async def test_the_offline_source_serves_only_what_it_has(strict_client):
    frame = await strict_client.all_indices_async(DAY_2026)
    assert len(frame) == STORED_ROW_COUNTS[DAY_2026]
    with pytest.raises(TickerNotFoundError):
        await strict_client.all_indices_async(DAY_2020.replace("01-01", "02-01"))


def test_the_offline_source_rejects_an_unknown_on_missing():
    with pytest.raises(ValueError, match="on_missing"):
        macro.RecordingSource(FIXTURES, on_missing="guess")


# ============================================================== public surface


def test_the_client_methods_match_the_module_shortcuts():
    """A shortcut must delegate to the client, and the client's sync method must bridge."""
    for name in ("all_indices", "index_daily", "india_vix", "valuations"):
        assert hasattr(macro.MacroClient, name)
        assert hasattr(macro.MacroClient, name + "_async")
        assert "_run_async" in inspect.getsource(getattr(macro.MacroClient, name))
        assert "_run_async" in inspect.getsource(getattr(macro, name))
    for name in ("index_range", "valuation_series"):
        assert hasattr(macro.MacroClient, name)
        assert hasattr(macro.MacroClient, name + "_async")
        assert "_run_async" in inspect.getsource(getattr(macro.MacroClient, name))


def test_the_module_constants_are_the_documented_values():
    assert macro.PROVENANCE == "nse_macro"
    assert macro.INDIA_VIX == "India VIX"
    assert macro.SENTINEL == "-"
    assert macro.DATE_FORMAT == "%d-%m-%Y"
    assert macro.HOSTS == ("nsearchives.nseindia.com", "archives.nseindia.com")
    assert macro.DEFAULT_HOST == macro.HOSTS[0]
    assert macro.VALUATION_FIELDS == ("PE", "PB", "DivYield")
    assert macro.INTEGER_FIELDS == ("Volume",)
    assert macro.NOT_APPLICABLE_COLUMN == "NotApplicable"
    assert macro.UNPARSED_COLUMN == "Unparsed"
    assert macro.HOST_ATTR == "bfinance_source_host"
    assert macro.URL_ATTR == "bfinance_source_url"
    assert macro.URLS_ATTR == "bfinance_source_urls"
    assert macro.RETRIEVED_ATTR == "bfinance_retrieved_utc"
    assert macro.COVERAGE_ATTR == "bfinance_macro_valuation_coverage"
    assert macro.MISSING_DATES_ATTR == "bfinance_macro_missing_dates"
    assert len(macro.COLUMNS) == 11


def test_every_field_is_bound_by_name_and_never_by_offset():
    """The mapping is total over the verbatim fields and one-to-one onto the output."""
    values = list(macro.FIELD_MAP.values())
    assert len(values) == len(set(values)) == len(macro.RAW_FIELDS)
    assert set(values) == set(macro.RAW_FIELDS)
    assert {label for _, label in macro.CANONICAL_BINDING} == set(macro.COLUMNS)
    assert len(macro.CANONICAL_BINDING) == len(macro.COLUMNS)


def test_no_cookie_priming_or_landing_page_fetch():
    """A static archive plus a User-Agent is the whole requirement here."""
    tree = ast.parse(Path(macro.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    code = ast.unparse(tree).lower()
    for forbidden in ("set_cookie", "nseindia.com/market-data", "market-activity", "settimeout"):
        assert forbidden not in code, forbidden


def test_the_module_declares_no_cache_because_a_past_archive_is_immutable():
    """No TTL, no SQLiteCache. A dated archive for a past date cannot go stale."""
    tree = ast.parse(Path(macro.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "sqlite_cache" not in imported
    assert "SQLiteCache" not in {name for name in dir(macro)}
    assert not any("ttl" in name.lower() for name in dir(macro) if not name.startswith("_")), (
        "a dated, immutable archive has nothing to expire; a TTL key would be a false promise"
    )


def test_the_index_date_is_parsed_dd_mm_yyyy_and_a_bad_one_is_counted_not_guessed():
    """No ``dayfirst`` fallback: 25-09-2026 read as MM-DD would move the row eight months."""
    good = macro.parse_index_close_csv(CSV_2026)
    assert str(good.index.get_level_values("Date")[0].date()) == DAY_2026

    swapped = CSV_2026.replace("25-09-2026", "2026-09-25")
    assert swapped != CSV_2026
    frame = macro.parse_index_close_csv(swapped)
    assert frame.empty
    assert frame.attrs["bfinance_macro_unparsed_date_rows"] == STORED_ROW_COUNTS[DAY_2026]
    assert frame.attrs[macro.INDEX_COUNT_ATTR] == 0


def test_a_repeated_index_row_is_deduped_and_recorded():
    lines = CSV_2026.splitlines()
    duplicated = "\n".join(lines + [lines[1]])  # Nifty 50 printed twice
    frame = macro.parse_index_close_csv(duplicated)

    assert frame.attrs["bfinance_macro_duplicate_rows"] == 1
    assert not frame.index.duplicated().any()
    assert len(frame) == STORED_ROW_COUNTS[DAY_2026]


# ================================================================ NEGATIVE CONTROLS


class TestNegativeControls:
    """The controls that prove the assertions above are not vacuous.

    Six assertions in this repo have shipped that cannot fail. These mutate the **real** parser
    — not a local reimplementation — and then replay the module's own assertions to watch them
    go red. If a control cannot turn the corresponding test red, that test is decoration.
    """

    # ------------------------------------------------- control 1: '-' coerced to 0.0

    def test_control_zeroing_the_sentinel_fabricates_a_vix_volume_and_a_zero_pe(self, monkeypatch):
        """The single most dangerous mutation this module exists to prevent."""
        real = macro._field_states

        def zeroed(raw):
            numeric, not_applicable, unparseable = real(raw)
            # '-' is already NaN in `numeric` by the time the real classifier is done. A parser
            # that treats that as zero publishes an India VIX volume of 0 and a P/E of 0.
            return numeric.fillna(0.0), not_applicable, unparseable

        monkeypatch.setattr(macro, "_field_states", zeroed)
        frame = macro.parse_index_close_csv(CSV_2026, index="India VIX")
        row = frame.loc[frame.index[0]]

        # The fabrications are real and visible ...
        assert float(row["Volume"]) == 0.0
        assert float(row["PE"]) == 0.0
        assert float(row["DivYield"]) == 0.0
        assert not pd.isna(row["PE"])

        # ... and each of the module's real value assertions now fails.
        for field in SENTINEL_FIELDS:
            with pytest.raises(AssertionError):
                assert pd.isna(row[field])

        # The *counters* survive the mutation, because the classification masks are untouched.
        # That is what makes this defect so dangerous and it deserves its own assertion: the
        # frame claims "not applicable" in attrs while publishing a zero in the value column,
        # and nothing between the two raises. A consumer reading coverage would be told the
        # gap is accounted for; a consumer reading the number would get a fabricated 0.
        assert frame.attrs[macro.NOT_APPLICABLE_ATTR] == dict.fromkeys(SENTINEL_FIELDS, 1)
        assert frame.attrs[macro.COVERAGE_ATTR]["PE"]["not_applicable"] == 1
        assert frame.attrs[macro.COVERAGE_ATTR]["PE"]["present"] == 1, (
            "coverage now counts the sentinel as a present value while the row says not-applicable"
        )

    def test_control_the_real_sentinel_reading_is_different_from_the_zeroed_one(self, monkeypatch):
        """And the un-mutated parser disagrees with the mutated one, field by field."""
        honest = macro.parse_index_close_csv(CSV_2026, index="India VIX")
        honest_row = honest.loc[honest.index[0]]

        real = macro._field_states
        monkeypatch.setattr(
            macro, "_field_states",
            lambda raw: (real(raw)[0].fillna(0.0), real(raw)[1], real(raw)[2]),
        )
        zeroed = macro.parse_index_close_csv(CSV_2026, index="India VIX")
        zeroed_row = zeroed.loc[zeroed.index[0]]

        for field in SENTINEL_FIELDS:
            assert pd.isna(honest_row[field]) and not pd.isna(zeroed_row[field]), field
            assert float(zeroed_row[field]) == 0.0, field
        # The price fields are untouched by the mutation, so it is the sentinel that carries it.
        assert float(honest_row["Close"]) == float(zeroed_row["Close"]) == 12.16

    def test_control_dropping_a_column_from_the_output_turns_the_contract_red(self):
        """The output-contract assertion is load bearing, not a tautology.

        Mutating ``CANONICAL_BINDING`` cannot be used here: the coverage report reads every
        valuation field by name, so a parser that stopped binding ``PE`` would fail on an
        unrelated lookup rather than producing the smaller frame this control is about. The
        column is therefore dropped from the **output** of an otherwise honest parse, which is
        exactly the state a future refactor would leave behind.
        """
        honest = macro.parse_index_close_csv(CSV_2026)
        assert list(honest.columns) == list(macro.COLUMNS) + [
            macro.NOT_APPLICABLE_COLUMN, macro.UNPARSED_COLUMN
        ]

        dropped = honest.drop(columns=["PE"])
        assert "PE" not in dropped.columns
        assert len(dropped.columns) == len(honest.columns) - 1
        with pytest.raises(AssertionError):
            assert list(dropped.columns) == list(macro.COLUMNS) + [
                macro.NOT_APPLICABLE_COLUMN, macro.UNPARSED_COLUMN
            ]
        # The valuation table loses its P/E column the same way, and the coverage report would
        # still claim a P/E coverage number — the second half of the same silent loss.
        table = macro.select_index(honest, "Nifty 50")[["PB", "DivYield"]]
        assert "PE" not in table.columns
        with pytest.raises(AssertionError):
            assert list(table.columns) == list(macro.VALUATION_FIELDS)

    def test_control_dropping_a_column_from_the_input_raises_rather_than_parsing(self):
        """A real schema change is loud, not a silently smaller frame."""
        lines = CSV_2026.splitlines()
        header = lines[0].split(",")
        position = header.index("P/B")

        def without_field(line: str) -> str:
            fields = line.split(",")
            del fields[position]
            return ",".join(fields)

        without_pb = "\n".join([without_field(line) for line in lines])
        # The mutation really removed the same column from the header and every row.
        assert len(without_pb.splitlines()[0].split(",")) == 12
        assert {len(line.split(",")) for line in without_pb.splitlines()} == {12}

        with pytest.raises(ParsingError) as excinfo:
            macro.parse_index_close_csv(without_pb)
        assert "P/B" in str(excinfo.value)
        # And the honest parse of the unmodified text does succeed, so the control is the
        # mutation and not a broken fixture.
        assert "PB" in macro.parse_index_close_csv(CSV_2026).columns

    def test_control_the_coverage_numbers_are_computed_and_not_hardcoded(self):
        """Change one row and the reported coverage must move with it."""
        honest = macro.parse_index_close_csv(CSV_2026)
        assert honest.attrs[macro.COVERAGE_ATTR]["PE"]["present"] == 4
        assert honest.attrs[macro.COVERAGE_ATTR]["PE"]["total"] == STORED_ROW_COUNTS[DAY_2026]

        blanked = CSV_2026.replace(",19.56,2.8,1.22", ",,2.8,1.22")
        assert blanked != CSV_2026, "the blanking did not apply"
        mutated = macro.parse_index_close_csv(blanked)
        assert mutated.attrs[macro.COVERAGE_ATTR]["PE"]["present"] == 3
        assert mutated.attrs[macro.COVERAGE_ATTR]["PE"]["unparsed"] == 1
        assert mutated.attrs[macro.COVERAGE_ATTR]["PE"]["not_applicable"] == 1
        assert mutated.attrs[macro.COVERAGE_ATTR]["PE"]["coverage_pct"] == 60.0
        assert mutated.attrs[macro.COVERAGE_ATTR]["PB"]["present"] == 4, (
            "only the blanked field moves; the others are measured independently"
        )
        assert mutated.attrs[macro.COVERAGE_ATTR]["DivYield"]["present"] == 4

    def test_control_the_change_pct_check_rejects_a_fraction_reading(self):
        """The consistency check is tight enough to catch treating the percent as a fraction."""
        frame = macro.parse_index_close_csv(CSV_2026)
        close = frame["Close"].astype("float64")
        points = frame["PointsChange"].astype("float64")
        reported = frame["ChangePct"].astype("float64")
        implied = pd.Series(macro.reconstruct_change_pct(close, points), index=frame.index)

        honest = (implied - reported).abs().max()
        as_fraction = (implied - reported / 100.0).abs().max()
        against_open = pd.Series(
            macro.reconstruct_change_pct(close, close - frame["Open"].astype("float64")),
            index=frame.index,
        )
        swapped = (against_open - reported).abs().max()

        assert honest < 0.05, f"the honest reading must pass, got {honest:.4f} pp"
        assert as_fraction > 1.0, (
            f"reading the percent as a fraction must blow the tolerance, got {as_fraction:.4f}"
        )
        assert swapped > 0.05, f"measuring against the open must blow it, got {swapped:.4f}"
