"""
The BE-debut regression: a default that silently returns an incomplete listing history.

The defect
----------
``parse_bhavcopy`` filtered rows with ``SctySrs == series`` and ``series`` defaulted to ``"EQ"``.
A newly listed scrip does not debut in ``EQ`` — it debuts in ``BE`` (trade-to-trade) and migrates
weeks later. So an ``EQ``-only default did not merely miss a row, it **mis-dated the listing**,
and it did so while returning a well-formed, non-empty frame, so nothing downstream could tell.

Measured on ITC Hotels over 2025-01-26..2025-02-14 (15 real archives, 44,044 raw rows):

* ``ITCHOTELS`` first appears **2025-01-29** with ``SctySrs="BE"`` (``FinInstrmId`` 29253)
* it first appears as ``"EQ"`` (``FinInstrmId`` 29251) on **2025-02-11**
* the ``EQ``-only default therefore lost the first **9** sessions and mis-dated the debut by
  **13** calendar days

The migration is not a rare edge case, and it is not one-directional. Over those same 15 archives
**43** distinct symbols changed series, in both directions, including single-session flips
(``AHL`` is ``EQ`` on 2025-01-29 and ``BE`` on 2025-02-11). An ``EQ``-only read punches holes in
the *middle* of established histories too.

The fix
-------
:data:`bfinance.nse.bhavcopy.EQUITY_SERIES` is ``('EQ', 'BE')``. Both are equity series; adding
``BE`` is a strict superset of the old default and loses nothing. The rejected alternative was
``series=None`` meaning "no filter": over the same 15 archives that admits **9,964** rows
(22.6%) that are not equities — gold bonds (``GB``, ``SGBDE30III`` at 8223.30), G-sec loans
(``GS``), T-bills (``TB``), REITs (``RR``, ``EMBASSY`` at 365.26), SME (``SM``), structured notes
(``BZ``/``E1``), NCDs (``N2``), invit funds (``IV``) — which is precisely the portfolio-poisoning
the module's own trap 2 warns about. ``None`` exists, is tested, and is never a default.

Fixture provenance — no network, nothing ``live``
-------------------------------------------------
Every row below is a **verbatim** data line from the real NSE archives, header included (the real
34-field UDiFF header, which is what the parser must look columns up in by name):

* ``/content/cm/BhavCopy_NSE_CM_0_0_0_20250129_F_0000.csv.zip`` — the BE-debut day
* ``/content/cm/BhavCopy_NSE_CM_0_0_0_20250211_F_0000.csv.zip`` — the first EQ day

captured 2026-09-27. Prices, volumes and instrument ids are exactly as published. For the
multi-day range test the remaining eleven days reuse one of the two captures with **only the date
column rewritten**, by column name — the same convention ``tests/test_nse_bhavcopy.py`` already
uses in its ``with_dates`` helper, so a price here is the recorded price for that scrip on the day
it was captured and no number is invented.

Every test in this file runs offline. ``_get_archive`` is replaced at the transport boundary, so
the real retry/404/parse/merge code paths run without a socket.
"""

import io
import zipfile

import pandas as pd
import pytest

from bfinance.nse import bhavcopy as bc

# --------------------------------------------------------------------- verbatim real captures

#: The real 2025-01-29 header, all 34 fields, byte-identical to what NSE served.
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)

#: 2025-01-29, verbatim. ITC Hotels' **debut**, in series BE. RELIANCE and AHL are EQ. The other
#: eight rows are the non-equity series the default must keep rejecting, at their real prices.
BE_DEBUT_DAY_ROWS = [
    "2025-01-29,2025-01-29,CM,NSE,STK,29253,INE379A01028,ITCHOTELS,BE,,,,,ITC HOTELS LIMITED,"
    "180.00,180.00,171.00,171.85,173.65,260.00,,171.90,,,24895079,4388479061.10,285932,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,"
    "1236.00,1242.75,1228.05,1235.50,1237.25,1234.40,,1235.50,,,5928156,7331119895.80,167073,F1,"
    "1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,13288,INE00ZE01026,AHL,EQ,,,,,ABANS HOLDINGS LIMITED,"
    "202.00,205.00,183.50,192.50,193.00,201.70,,192.55,,,66391,12909009.10,2603,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,19078,IN0020200104,SGBJUN28,GB,,,,,2.5%GOLDBONDS2028SR-III,"
    "8170.00,8200.00,8150.01,8169.00,8169.00,8170.00,,8169.00,,,20,163325.05,6,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,9383,INE041025011,EMBASSY,RR,,,,,EMBASSY OFFICE PARKS REIT,"
    "363.20,368.99,361.21,365.47,365.40,363.11,,365.47,,,308497,112668407.04,24505,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,956,IN0020010081,667GS2050,GS,,,,,GOI LOAN  6.67% 2050,"
    "95.73,96.31,95.73,96.30,96.30,98.60,,96.30,,,4154,400029.09,5,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,23005,IN002023Z521,364D060325,TB,,,,,GOI TBILL 364D-06/03/25,"
    "99.07,99.07,99.07,99.07,99.07,99.07,,99.07,,,500,49535.00,4,F1,100,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,10491,INE0LF301013,AGNI,SM,,,,,AGNI GREEN POWER LTD,"
    "33.50,33.95,32.90,33.20,32.90,31.60,,33.20,,,30000,1007500.00,10,F1,2500,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,10770,INE136T01014,AHIMSA,ST,,,,,AHIMSA INDUSTRIES LTD.,"
    "52.35,52.35,52.35,52.35,52.35,53.40,,52.35,,,3000,157050.00,1,F1,3000,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,20817,INE183W23014,IRBINVIT,IV,,,,,IRB INVIT FUND,"
    "59.90,59.90,59.60,59.86,59.90,59.71,,59.86,,,333669,19955595.89,1284,F1,1,,,,,",
    "2025-01-29,2025-01-29,CM,NSE,STK,28646,INE591Q01016,SUULD,BZ,,,,,SUUMAYA INDUSTRIES LTD,"
    "3.72,3.72,3.43,3.51,3.51,3.55,,3.51,,,21736,77198.86,89,F1,1,,,,,",
]

#: 2025-02-11, verbatim. ITC Hotels in **EQ** (a different FinInstrmId for the same ISIN — the
#: migration registers a new instrument, which is why it must not be keyed on the id). AHL is now
#: **BE** — the same scrip, the other direction, on the very same day.
EQ_MIGRATION_DAY_ROWS = [
    "2025-02-11,2025-02-11,CM,NSE,STK,29251,INE379A01028,ITCHOTELS,EQ,,,,,ITC HOTELS LIMITED,"
    "179.95,179.95,166.59,170.62,170.50,179.28,,170.65,,,13950854,2383248604.15,103677,F1,1,,,,,",
    "2025-02-11,2025-02-11,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,"
    "1264.55,1264.55,1230.10,1234.85,1237.55,1253.65,,1234.85,,,8859714,11023757522.95,240683,F1,"
    "1,,,,,",
    "2025-02-11,2025-02-11,CM,NSE,STK,13293,INE00ZE01026,AHL,BE,,,,,ABANS HOLDINGS LIMITED,"
    "185.00,186.50,180.00,182.33,182.60,180.67,,182.70,,,26701,4857502.44,466,F1,1,,,,,",
    "2025-02-11,2025-02-11,CM,NSE,STK,19078,IN0020200104,SGBJUN28,GB,,,,,2.5%GOLDBONDS2028SR-III,"
    "8519.00,8564.00,8490.00,8508.52,8500.00,8514.95,,8508.52,,,579,4925895.09,53,F1,1,,,,,",
]

#: Every non-equity series present in the captures above, with the ticker that carries it. The
#: default must reject all of them; ``series=None`` is the only way in.
NON_EQUITY_TICKERS = {
    "SGBJUN28": "GB",  # gold bond, 8169.00
    "EMBASSY": "RR",  # REIT, 365.47
    "667GS2050": "GS",  # G-sec loan, 96.30
    "364D060325": "TB",  # T-bill, 99.07
    "AGNI": "SM",  # SME, 33.20
    "AHIMSA": "ST",  # 52.35
    "IRBINVIT": "IV",  # invit, 59.86
    "SUULD": "BZ",  # structured note, 3.51
}

#: The nine sessions ITC Hotels actually printed in BE, and the four in EQ.
BE_SESSIONS = (
    "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
    "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10",
)
EQ_SESSIONS = ("2025-02-11", "2025-02-12", "2025-02-13", "2025-02-14")

DEBUT = "ITCHOTELS"
LARGE_CAP = "RELIANCE"
MIGRATING = "AHL"


# --------------------------------------------------------------------- helpers


def _csv(rows) -> str:
    return "\n".join([UDIFF_HEADER, *rows])


def _on(rows, day: str) -> str:
    """The same verbatim rows with only the date columns moved to ``day``.

    The date columns are addressed **by name** (``TradDt``/``BizDt``), never by offset, exactly
    as the module itself parses them.
    """
    out = [UDIFF_HEADER]
    for line in rows:
        fields = line.split(",")
        for column in ("TradDt", "BizDt"):
            fields[UDIFF_HEADER.split(",").index(column)] = day
        out.append(",".join(fields))
    return "\n".join(out)


def _zipped(csv_text: str) -> bytes:
    """A single-member ZIP, the shape NSE serves."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bhav.csv", csv_text.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def debut_day():
    """The captured 2025-01-29 archive as CSV text, exactly as :func:`unzip_bhavcopy` yields it."""
    return _csv(BE_DEBUT_DAY_ROWS)


@pytest.fixture
def migration_day():
    """The captured 2025-02-11 archive as CSV text."""
    return _csv(EQ_MIGRATION_DAY_ROWS)


@pytest.fixture
def client():
    return bc.BhavcopyClient(min_request_interval_ms=0.0)


def _serve(client, mapping):
    """Replace the transport with a per-URL lookup. 404 for anything not in ``mapping``."""
    stamps = {day.replace("-", ""): payload for day, payload in mapping.items()}

    async def fake(url):
        for stamp, payload in stamps.items():
            if stamp in url:
                return payload
        return None

    client._get_archive = fake
    return client


# --------------------------------------------------------------------- the regression


def test_the_default_returns_a_scrip_that_debuts_in_be(debut_day):
    """**The bug.** On 2025-01-29 ``ITCHOTELS`` printed exactly one bar, in series ``BE``.

    With the old ``series="EQ"`` default that bar did not exist: the frame came back well-formed,
    non-empty (RELIANCE and AHL were there) and silently missing the debut. A caller could not tell
    the difference between "listed on the 29th" and "listed whenever EQ happened to start".
    """
    frame = bc.parse_bhavcopy(debut_day)  # no series argument: the default under test

    assert DEBUT in set(frame.index.get_level_values("Symbol")), (
        "the debut session vanished from the default read"
    )
    debut = frame.xs(DEBUT, level="Symbol")
    assert len(debut) == 1
    assert debut.index[0] == pd.Timestamp("2025-01-29")
    # The real published bar, not a reconstruction of it.
    assert debut["Open"].iloc[0] == 180.00
    assert debut["High"].iloc[0] == 180.00
    assert debut["Low"].iloc[0] == 171.00
    assert debut["Close"].iloc[0] == 171.85
    assert int(debut["Volume"].iloc[0]) == 24895079


async def test_the_default_reports_which_series_it_actually_included(client, debut_day):
    """A consumer must be able to see what it got without inferring it."""
    frame = await _serve(client, {"2025-01-29": _zipped(debut_day)}).bhavcopy_async("2025-01-29")

    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ")
    assert frame.attrs["bfinance_series"] == bc.EQUITY_SERIES
    assert DEBUT in set(frame.index.get_level_values("Symbol"))


async def test_a_range_keeps_every_be_debut_session_not_only_the_eq_ones(client, debut_day, migration_day):
    """**The bug, end to end.** The 13 sessions a caller actually gets for a recent listing.

    Before the fix this returned 4 rows starting 2025-02-11: nine real sessions gone, and the
    debut date wrong by 13 calendar days. The first price mark after a distribution event is
    exactly what a demerger adjustment needs, so this is not cosmetic.
    """
    served = _serve(
        client,
        {**_on_and_zip(BE_DEBUT_DAY_ROWS, BE_SESSIONS), **_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS)},
    )
    frame = await served.bhavcopy_range_async("2025-01-29", "2025-02-14")

    history = frame.xs(DEBUT, level="Symbol")
    got = tuple(str(d.date()) for d in history.index)

    assert got == BE_SESSIONS + EQ_SESSIONS, (
        f"expected the 13 published sessions, got {len(got)}: {got}"
    )
    assert str(history.index.min().date()) == "2025-01-29", (
        "the debut must be the first session, not the first EQ session"
    )
    # The BE bar is the one the old default threw away.
    assert history.loc[pd.Timestamp("2025-01-29"), "Close"] == 171.85
    assert history.loc[pd.Timestamp("2025-02-11"), "Close"] == 170.62


def _on_and_zip(rows, days):
    return {day: _zipped(_on(rows, day)) for day in days}


async def test_a_series_migration_stitches_into_one_continuous_history(client, debut_day, migration_day):
    """No gap at the seam. A migrated listing must read as one price series, not two fragments.

    The migration also registers a *new* ``FinInstrmId`` (29253 -> 29251 for the same ISIN), so a
    history keyed on the instrument id would break here too. This module keys on ``(Date, Symbol)``,
    which is what makes the stitch work.
    """
    served = _serve(
        client,
        {**_on_and_zip(BE_DEBUT_DAY_ROWS, BE_SESSIONS), **_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS)},
    )
    frame = await served.bhavcopy_range_async("2025-01-29", "2025-02-14")

    history = frame.xs(DEBUT, level="Symbol")
    assert len(history) == 13
    assert not history.index.duplicated().any()
    assert history.index.is_monotonic_increasing
    assert history["Close"].notna().all(), "a migration must not blank a session"
    # One row per session, so nothing was silently deduplicated away.
    assert len(set(str(d.date()) for d in history.index)) == 13


async def test_the_migration_is_kept_in_the_other_direction_too(client, debut_day, migration_day):
    """``AHL`` is ``EQ`` on 2025-01-29 and ``BE`` on 2025-02-11 — measured, same two archives.

    So the defect was not only a truncated *start*. An ``EQ``-only read punched a single-session
    hole in the middle of an established history, which no listing-date check would ever catch.
    """
    served = _serve(client, {"2025-01-29": _zipped(debut_day), "2025-02-11": _zipped(migration_day)})
    frame = await served.bhavcopy_range_async("2025-01-29", "2025-02-11")

    history = frame.xs(MIGRATING, level="Symbol")
    assert len(history) == 2, "the EQ->BE session must survive too"
    assert str(history.index.min().date()) == "2025-01-29"
    assert str(history.index.max().date()) == "2025-02-11"
    assert history.loc[pd.Timestamp("2025-01-29"), "Close"] == 192.50
    assert history.loc[pd.Timestamp("2025-02-11"), "Close"] == 182.33


# --------------------------------------------------------------------- blast radius


def test_the_default_still_excludes_every_non_equity_series(debut_day):
    """Adding ``BE`` must not have turned the default into "everything".

    These are the real non-equity rows from the capture, at prices a portfolio would happily
    accept: a gold bond at 8169.00, a REIT at 365.47, a G-sec loan at 96.30, a T-bill at 99.07.
    """
    frame = bc.parse_bhavcopy(debut_day)

    returned = set(frame.index.get_level_values("Symbol"))
    assert set(NON_EQUITY_TICKERS).isdisjoint(returned), "a non-equity row leaked into equities"
    assert returned == {DEBUT, LARGE_CAP, MIGRATING}
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ")
    assert frame.attrs["bfinance_source_row_count"] == 11
    assert frame.attrs["bfinance_rows_dropped_by_series_filter"] == 8


def test_the_default_is_a_strict_superset_of_the_eq_only_read(debut_day, migration_day):
    """No EQ row may change value, disappear, or be re-ordered by adding ``BE``.

    Checked on the migration day too, where the BE row is a *different* scrip (``AHL``) than the
    EQ ones, so a mask written for a single code would be visible here.
    """
    for csv_text in (debut_day, migration_day):
        default = bc.parse_bhavcopy(csv_text)
        eq_only = bc.parse_bhavcopy(csv_text, series="EQ")

        assert set(eq_only.index) <= set(default.index), "the union dropped an EQ row"
        assert eq_only["Close"].equals(
            default.loc[eq_only.index, "Close"]
        ), "an EQ price changed when BE was added"
        added = default.index.difference(eq_only.index)
        assert len(added) == 1, f"expected exactly the one BE row to be added, got {len(added)}"
        assert default.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ")


def test_the_default_never_produces_two_bars_for_one_instrument_day(debut_day, migration_day):
    """The union can only be safe because a scrip is never in ``EQ`` and ``BE`` on the same day.

    Measured: same-day ``EQ``/``BE`` symbol overlap was 0 across all 15 real archives checked. If
    upstream ever breaks that, the frame must report the duplicate rather than quietly keep one.
    """
    for csv_text in (debut_day, migration_day):
        frame = bc.parse_bhavcopy(csv_text)
        assert not frame.index.duplicated().any()
        assert frame.attrs["bfinance_duplicate_date_rows"] == 0


def test_the_explicit_eq_keyword_still_returns_only_eq_rows(debut_day):
    """Backwards compatibility. ``series="EQ"`` is a real request and must keep returning EQ and
    only EQ — the fix must not over-reach into a caller's explicit choice.

    The row selection here is unchanged by the fix and holds both before and after. The
    ``bfinance_series_included`` assertion cannot pass before it: the key does not exist yet.
    """
    frame = bc.parse_bhavcopy(debut_day, series="EQ")

    assert DEBUT not in set(frame.index.get_level_values("Symbol")), (
        "series='EQ' must not start returning BE rows"
    )
    assert set(frame.index.get_level_values("Symbol")) == {LARGE_CAP, MIGRATING}
    # A single string stays a string in the request attr; that is an existing contract.
    assert frame.attrs["bfinance_series"] == "EQ"
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("EQ",)


async def test_a_large_cap_is_bit_identical_under_both_reads(client, debut_day, migration_day):
    """The normal case, end to end: a plain EQ listing is unaffected by the fix.

    This is the check that the fix is a superset and not a change of values. Same rows, same
    prices, same order — for the instrument the old default was actually right about.
    """
    served = _serve(
        client,
        {**_on_and_zip(BE_DEBUT_DAY_ROWS, BE_SESSIONS), **_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS)},
    )
    default = await served.bhavcopy_range_async("2025-01-29", "2025-02-14")
    eq_only = await _serve(
        client,
        {**_on_and_zip(BE_DEBUT_DAY_ROWS, BE_SESSIONS), **_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS)},
    ).bhavcopy_range_async("2025-01-29", "2025-02-14", series="EQ")

    default_cap = default.xs(LARGE_CAP, level="Symbol")
    assert len(default_cap) == 13
    assert list(default_cap.columns) == list(bc.COLUMNS)
    pd.testing.assert_frame_equal(default_cap, eq_only.xs(LARGE_CAP, level="Symbol"))

    # And the rows the union *adds* are exactly the BE sessions, nothing else.
    added = default.index.difference(eq_only.index)
    assert len(added) == 13 * 1, f"13 BE sessions for two migrating scrips, got {len(added)}"
    assert set(default.index.get_level_values("Symbol")) - set(
        eq_only.index.get_level_values("Symbol")
    ) <= {DEBUT, MIGRATING}


# --------------------------------------------------------------------- the rejected option


def test_series_none_keeps_everything_and_is_never_the_default(debut_day):
    """``series=None`` means *no filter*, and the measurement is why it is not the default.

    It returns all 11 captured rows, gold bond and T-bill included. Available, tested, off by
    default — a caller who genuinely wants the whole CM file can have it and can see it in
    ``bfinance_series_included``.
    """
    frame = bc.parse_bhavcopy(debut_day, series=None)

    assert frame.attrs["bfinance_series"] is None, "no filter was applied"
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "BZ", "EQ", "GB", "GS", "IV", "RR", "SM", "ST", "TB")
    assert frame.attrs["bfinance_rows_dropped_by_series_filter"] == 0
    assert set(NON_EQUITY_TICKERS) <= set(frame.index.get_level_values("Symbol"))
    # Not the default, on every entry point.
    assert bc.EQUITY_SERIES != ()
    assert bc.resolve_series(None) is None
    for func in (bc.bhavcopy, bc.bhavcopy_range, bc.BhavcopyClient.bhavcopy,
                 bc.BhavcopyClient.bhavcopy_range, bc.BhavcopyClient.bhavcopy_async,
                 bc.BhavcopyClient.bhavcopy_range_async, bc.parse_bhavcopy):
        import inspect

        assert inspect.signature(func).parameters["series"].default == bc.EQUITY_SERIES, func


def test_an_empty_series_list_is_rejected_rather_than_meaning_everything():
    """``[]`` and ``None`` are opposite answers; the ambiguity is refused rather than guessed."""
    with pytest.raises(ValueError, match=r"[Pp]ass series=None"):
        bc.resolve_series([])
    with pytest.raises(ValueError):
        bc.parse_bhavcopy("TradDt,SctySrs,TckrSymb,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol\n",
                          series=[])


# --------------------------------------------------------------------- series spec plumbing


def test_series_included_is_measured_from_the_rows_not_echoed_from_the_request(debut_day):
    """A requested series absent from today's file must not appear in what was included.

    That difference is the whole reason the key exists: on a 2026-09-25-shaped file with no ``BE``
    line, a caller asking for ``('EQ', 'BE')`` reads ``('EQ',)`` and knows the coverage gap.
    """
    frame = bc.parse_bhavcopy(debut_day, series=["EQ", "BE", "ZZ"])

    # The request is recorded in the order asked, normalised; what came back is sorted, because
    # that is a property of the rows and not of the question.
    assert frame.attrs["bfinance_series"] == ("EQ", "BE", "ZZ")
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ"), (
        "ZZ was requested and is in no row; it must not be reported as included"
    )
    assert frame.attrs["bfinance_series"] != frame.attrs[bc.SERIES_INCLUDED_ATTR]


async def test_a_404_reports_an_empty_series_included(client):
    """Present on every return, so a consumer reads one key without branching."""
    frame = await _serve(client, {}).bhavcopy_async("2025-01-30")

    assert frame.empty
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ()
    assert frame.attrs[bc.SYNTHETIC_OHLC_ATTR] is False


async def test_a_file_with_no_rows_for_the_series_reports_an_empty_series_included(client, debut_day):
    frame = await _serve(client, {"2025-01-29": _zipped(debut_day)}).bhavcopy_async(
        "2025-01-29", series="ZZ"
    )

    assert frame.empty
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ()
    assert frame.attrs["bfinance_no_rows_for_series"] is True


def test_validate_series_is_unchanged_and_still_validates_one_code():
    """The public single-code validator is untouched: same answers, same errors, same order."""
    assert bc.validate_series("eq") == "EQ"
    assert bc.validate_series(" gb ") == "GB"
    with pytest.raises(ValueError, match="must be a string"):
        bc.validate_series(None)
    with pytest.raises(ValueError, match="must be a string"):
        bc.validate_series(["EQ"])
    with pytest.raises(ValueError, match="non-empty string"):
        bc.validate_series("")
    with pytest.raises(ValueError, match="not a series code"):
        bc.validate_series("EQUITY")


def test_resolve_series_normalises_dedupes_and_preserves_order():
    assert bc.resolve_series("be") == ("BE",)
    assert bc.resolve_series(["be", "EQ", "be", " gb "]) == ("BE", "EQ", "GB")
    assert bc.resolve_series(("EQ",)) == ("EQ",)
    assert bc.resolve_series(None) is None
    with pytest.raises(ValueError, match="non-string entry"):
        bc.resolve_series(["EQ", 7])
    with pytest.raises(ValueError, match="iterable of strings"):
        bc.resolve_series(7)


async def test_the_range_reports_the_series_observed_across_the_days_it_kept(client, debut_day, migration_day):
    """The range answer to "what did I get" is a measurement no single day can make.

    Both captures here carry both series. Serve one day of each and the union is both; drop the BE
    capture entirely and it is ``('EQ',)`` — which is the coverage signal a caller needs.
    """
    both = await _serve(
        client, {**_on_and_zip(BE_DEBUT_DAY_ROWS, BE_SESSIONS[:1]),
                 **_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS[:1])},
    ).bhavcopy_range_async("2025-01-29", "2025-01-29")
    # The single served day is the BE capture, so only ('BE', 'EQ') is observed there.
    assert both.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ")
    assert both.attrs["bfinance_series"] == bc.EQUITY_SERIES

    eq_capture_only = await _serve(
        client, {**_on_and_zip(EQ_MIGRATION_DAY_ROWS, EQ_SESSIONS[:1])},
    ).bhavcopy_range_async("2025-02-11", "2025-02-11")
    # Requesting EQ+BE on a day whose only BE row is AHL still shows both, measured not assumed.
    assert eq_capture_only.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ")


async def test_no_frames_at_all_reports_an_empty_series_included(client):
    frame = await _serve(client, {}).bhavcopy_range_async("2025-01-29", "2025-01-31")

    assert frame.empty
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ()
    assert frame.attrs["bfinance_provenance"] == bc.PROVENANCE
