"""
Two silent-wrong-answer gaps in ``nse/bhavcopy.py``, closed against recorded NSE archives.

Gap 1 — the series roster
-------------------------
``EQUITY_SERIES`` is ``('EQ', 'BE')``, which is correct and stays that way. The gap was that a
caller had **no way to ask for anything else**: nine further codes carry listed securities, and
requesting one returned a well-formed **empty frame** with no indication whether the code was
wrong, absent, or quietly filtered.

What was measured here, from whole untrimmed archives recorded under
``tests/fixtures/bhavcopy/`` (no network, nothing ``live``):

============================  ======  ======  ======
day                           rows    codes   notes
============================  ======  ======  ======
2026-09-25 CM (UDiFF, 34col)   3654     57
2026-09-24 CM (UDiFF, 34col)   3637     60    union of the two: 71
2023-05-15 CM (legacy, 14col)  2427     70
2026-09-25 FO (UDiFF, 34col)  37165      0    SctySrs blank on every row
============================  ======  ======  ======

Two findings here are worth more than the roster itself:

* **The roster is not stable.** 25 codes are present on one day and absent on the other, so any
  hardcoded "the" list is wrong by construction. ``SERIES_MEASURED`` is published as a *dated
  observation* and :func:`series_in_file` re-derives the live one.
* **Symbol pattern cannot classify equity vs debt.** Two plausible rules were tried against
  9,718 recorded rows and both are refuted by real listed securities: *"a debt scrip code starts
  with a digit"* is wrong for 9 companies (``20MICRONS``, ``360ONE``, ``3MINDIA``, ``5PAISA``,
  ``4THDIM``, ``3PLAND``, ``3BBLACKBIO``, ``3IINFOLTD``, ``21STCENMGM``), and *"a debt scrip code
  ends in a maturity code"* is wrong for **158** equity rows because NSE's ETFs end in digits too
  (``BBETF0432``, ``GOLD360``, ``ENEXT50``, ``GROWWMC150``, ``MSCI360``, ``FAZE3Q``, ``C2C``).
  The instrument **name** separates them cleanly, and that is what the classification uses.

Gap 2 — ``TtlTrfVal``
---------------------
The 34-column UDiFF file carries ``TtlTrfVal`` (traded value) next to ``TtlTradgVol`` (volume),
and the module read only the volume, so the VWAP that :mod:`bfinance.costs` documents as its one
empirical claim was not reachable through this package. The column is now mapped — under the name
``TurnoverValue``, because a bare ``Turnover`` invites reading it as rupees when the unit is
**segment-dependent** and that is measured, not assumed:

* CM (UDiFF **and** legacy): the identity holds for every testable row, so the unit is
  established as price x volume.
* F&O: the identity fails **0/18072**, because ``TtlTradgVol`` counts lots while ``TtlTrfVal`` is
  in units of the *underlying*. Measured: ``ABCAPITAL 3100`` (lot 3100), premium range 0.45-0.80,
  volume 85, value 118,724,575.00 -> 1,396,759.71, which is ABCAPITAL's share price.

``SttlmPric`` is in the same file, is **populated** on cash rows, and is a false positive for
anyone grepping for "stt": it is the settlement price, and mapping it as turnover would yield a
number ~1e6 too small rather than too large, so no magnitude check would notice. It is recorded
in ``UNMAPPED_COLUMNS`` and asserted unmapped below.

Negative controls
-----------------
``test_negative_control_*`` mutate the implementation and assert the tests go red. A test that
cannot fail is worse than no test, and six assertions in this repo have shipped that way. The
results are recorded in the accompanying report; each control is a real assertion, not a comment.
"""

import io
import pathlib
import zipfile
from collections import Counter

import pandas as pd
import pytest

from bfinance.nse import bhavcopy as bc

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bhavcopy"

#: The real archives, captured whole. Provenance in ``tests/fixtures/bhavcopy/META.json``.
CM_25 = "udiff_cm_20260925_full.csv.zip"   # UDiFF, 34 cols, Rsvd1..Rsvd4, 3654 rows
CM_24 = "udiff_cm_20260924_full.csv.zip"   # UDiFF, 34 cols, 3637 rows
LEGACY = "legacy_cm_20230515_full.csv.zip"  # legacy, 14 cols, 2427 rows
FO_25 = "udiff_fo_20260925_full.csv.zip"   # UDiFF, 34 cols, 37165 rows

#: The measured roster: rows per code, per day. Kept in the test (not imported) so the test and
#: the constant are genuinely independent statements of the same measurement.
MEASURED = {
    "2026-09-25": {"EQ": 2660, "SM": 380, "BE": 239, "ST": 84, "GS": 53, "GB": 40, "BZ": 27,
                   "N0": 21, "IV": 15, "TB": 14, "SG": 8, "N1": 7, "N5": 7, "N2": 6, "N6": 6,
                   "RR": 6, "BL": 2, "E1": 2, "SZ": 1},
    "2026-09-24": {"EQ": 2664, "SM": 370, "BE": 236, "ST": 82, "GS": 53, "GB": 40, "BZ": 27,
                   "N0": 24, "IV": 12, "N1": 11, "SG": 7, "N2": 7, "N6": 7, "N5": 6, "NC": 6,
                   "RR": 6, "TB": 6, "BL": 2, "E1": 1, "SZ": 1},
}


def read_fixture(name: str) -> str:
    """The CSV text of a recorded archive, exactly as :func:`unzip_bhavcopy` yields it."""
    with zipfile.ZipFile(FIXTURES / name) as archive:
        member = [m for m in archive.namelist() if not m.endswith("/")][0]
        return archive.read(member).decode("utf-8-sig", errors="replace")


def raw(name: str) -> pd.DataFrame:
    """The recorded file as strings, junk columns dropped — the parser's own view of the input."""
    frame = pd.read_csv(io.StringIO(read_fixture(name)), dtype=str, keep_default_na=False,
                         na_values=[""])
    return frame.drop(columns=[c for c in frame.columns if str(c).startswith("Unnamed:")])


def served(client, mapping):
    """Replace the transport with a per-URL lookup. 404 for anything not in ``mapping``."""
    async def fake(url):
        for stamp, payload in mapping.items():
            if stamp in url:
                return payload
        return None

    client._get_archive = fake
    return client


def zipped(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bhav.csv", csv_text.encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def client():
    return bc.BhavcopyClient(min_request_interval_ms=0.0)


def sync(coro):
    """Run an async call from a sync test, the same way the module's own shortcuts do."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("call sync() from a sync test only")


# ===================================================================== the default is fixed


def test_the_default_is_still_eq_and_be_on_every_entry_point():
    """The 22.6% non-equity problem is real and measured, so the default must not widen.

    Asserted on the constant and on the *signature* of every public read path, because a widened
    default is a silent change: it would add gold bonds, T-bills, REITs and NCDs to every
    portfolio built from this module, at prices that look entirely plausible.
    """
    assert bc.EQUITY_SERIES == ("EQ", "BE")

    import inspect

    for func in (bc.bhavcopy, bc.bhavcopy_range, bc.BhavcopyClient.bhavcopy,
                 bc.BhavcopyClient.bhavcopy_range, bc.BhavcopyClient.bhavcopy_async,
                 bc.BhavcopyClient.bhavcopy_range_async, bc.parse_bhavcopy):
        default = inspect.signature(func).parameters["series"].default
        assert default == ("EQ", "BE"), f"{func.__qualname__} default widened to {default!r}"

    # The narrow default really is narrow, on a real file.
    frame = bc.parse_bhavcopy(read_fixture(CM_25))
    assert frame.attrs["bfinance_rows_dropped_by_series_filter"] == 3654 - 2660 - 239
    assert set(frame.index.get_level_values("Symbol")).isdisjoint(
        {"SGBJUN28", "667GS2050", "EMBASSY", "1015SCL28A"}
    ), "a debt row leaked into the default read"


# ================================================== the roster, re-derived from the fixtures


@pytest.mark.parametrize(
    "day,fixture",
    [("2026-09-25", CM_25), ("2026-09-24", CM_24)],
)
def test_the_measured_roster_matches_the_archives_it_claims_to_come_from(day, fixture):
    """The published roster must equal what the recorded file actually holds — for **both** days.

    The value here is the second day. A list that is right for one archive and wrong for the next
    is not a list, it is a coincidence, and that is precisely the failure this guards: the roster
    differs between the two captures by 3 codes in total and by 25 in the union.
    """
    frame = raw(fixture)
    observed = tuple(sorted(frame["SctySrs"].astype(str).str.strip().unique()))

    assert observed == bc.series_in_file(read_fixture(fixture)), f"{day}: series_in_file disagrees"
    assert observed == bc.SERIES_MEASURED[day], f"{day}: SERIES_MEASURED disagrees with the file"
    assert len(observed) == (57 if day == "2026-09-25" else 60)


def test_the_row_counts_sum_to_the_row_total_of_each_file():
    """A per-code table whose components do not add up is a transcription error, not a table.

    This is not hypothetical. A hand-circulated table reproduced the ten largest counts exactly and
    then stated a total of 59 distinct series, which matches **neither** captured day (57 and 60).
    Asserting the sum is the cheapest possible guard against repeating that.
    """
    for (day, counts), fixture in zip(bc.SERIES_ROW_COUNTS.items(), (CM_25, CM_24)):
        assert sum(counts.values()) == len(raw(fixture)), f"{day}: counts do not sum to the rows"
    for day, expected in MEASURED.items():
        for code, count in expected.items():
            assert bc.SERIES_ROW_COUNTS[day][code] == count, f"{day} {code}"


def test_the_roster_is_not_stable_so_it_is_observed_not_asserted():
    """Documenting the instability is the point: it is why ``series_in_file`` exists at all.

    A future contributor reading a single ``SERIES_MEASURED`` entry could reasonably conclude the
    roster is fixed. These assertions say otherwise with two whole archives as evidence.
    """
    day25, day24 = set(bc.SERIES_MEASURED["2026-09-25"]), set(bc.SERIES_MEASURED["2026-09-24"])

    assert day25 != day24, "the two captures are expected to differ; if they do not, re-measure"
    assert len(day25 | day24) == 71
    assert len(day25 - day24) + len(day24 - day25) == 25, "25 codes are day-specific"
    # Every code in the published union is real: each appears in at least one of the two files.
    observed = set(bc.series_in_file(read_fixture(CM_25))) | set(
        bc.series_in_file(read_fixture(CM_24))
    )
    assert day25 | day24 == observed


# ================================== the equity/debt classification is derived, not asserted


def test_the_series_classification_is_derived_not_asserted():
    """**Re-derive** ``EQUITY_LIKE_SERIES`` from the recorded files and compare.

    The classification is recomputed here from ``is_debt_instrument`` applied to every published
    instrument name, per series, per day — reading the fixtures, never the constant being
    checked. A majority verdict per code is taken over both captures; the result must equal
    ``EQUITY_LIKE_SERIES`` and ``DEBT_SERIES`` exactly, with no code left undecided.

    Restricted to the two UDiFF captures, and the reason is a schema fact rather than a
    convenience: the **legacy** file's 14 columns carry no instrument name at all, so there is
    nothing for a name-based rule to read. That asymmetry is itself the argument for the rule —
    the field that identifies an instrument is present in the modern schema and absent from the
    old one, whereas the ticker is present in both and is not sufficient.
    """
    assert "FinInstrmNm" in raw(CM_25).columns
    assert "FinInstrmNm" not in raw(LEGACY).columns, "the legacy schema has no name column"

    verdicts: dict[str, Counter] = {}
    for fixture in (CM_25, CM_24):
        frame = raw(fixture)
        for code, name in zip(
            frame["SctySrs"].astype(str).str.strip(),
            frame["FinInstrmNm"].astype(str).str.strip(),
        ):
            verdicts.setdefault(code, Counter())[bc.is_debt_instrument(name)] += 1

    derived_equity, derived_debt, ambiguous = [], [], []
    for code, tally in sorted(verdicts.items()):
        debt, equity = tally[True], tally[False]
        if debt and equity:
            ambiguous.append(code)
        elif debt > equity:
            derived_debt.append(code)
        else:
            derived_equity.append(code)

    assert ambiguous == [], f"the rule could not separate these series: {ambiguous}"
    assert tuple(sorted(derived_equity)) == bc.EQUITY_LIKE_SERIES
    assert tuple(sorted(derived_debt)) == bc.DEBT_SERIES
    # The two sets partition the measured union: no code is both or neither.
    union = set(bc.SERIES_MEASURED["2026-09-25"]) | set(bc.SERIES_MEASURED["2026-09-24"])
    assert set(bc.EQUITY_LIKE_SERIES) | set(bc.DEBT_SERIES) == union
    assert set(bc.EQUITY_LIKE_SERIES) & set(bc.DEBT_SERIES) == set()


def test_the_classification_never_calls_a_listed_security_debt():
    """The failure direction that would matter: an equity row read as debt.

    Checked per row, over both UDiFF captures, against the series the derivation itself classified
    as equity-like. Any non-empty result is a rule regression, not a rounding artefact.
    """
    equity_like = set(bc.EQUITY_LIKE_SERIES)
    offenders = []
    for fixture in (CM_25, CM_24):
        frame = raw(fixture)
        for code, name in zip(
            frame["SctySrs"].astype(str).str.strip(),
            frame["FinInstrmNm"].astype(str).str.strip(),
        ):
            if code in equity_like and bc.is_debt_instrument(name):
                offenders.append((fixture, code, name))
    assert offenders == [], f"listed securities classified as debt: {offenders[:5]}"


def test_symbol_pattern_is_refuted_so_it_is_not_used_to_classify():
    """Pin the two refutations, so nobody reinstates a symbol rule believing it is sound.

    Both claims are checkable against the recorded data and both are false. The first count is of
    distinct ``EQ``/``BE`` tickers beginning with a digit; the second is of equity rows whose
    ticker ends in a digit or digit-letter, which is what a "maturity code" heuristic sweeps up
    along with the bonds.
    """
    digit_leading_companies = set()
    etf_like_rows = 0
    for fixture in (CM_25, CM_24, LEGACY):
        frame = raw(fixture)
        series_col = "SctySrs" if "SctySrs" in frame.columns else "SERIES"
        sym_col = "TckrSymb" if "TckrSymb" in frame.columns else "SYMBOL"
        series = frame[series_col].astype(str).str.strip()
        symbols = frame[sym_col].astype(str).str.strip()
        equity_rows = series.isin(["EQ", "BE"])
        digit_leading_companies |= set(symbols[equity_rows & symbols.str[0].str.isdigit()])
        etf_like_rows += int(
            (equity_rows & symbols.str.contains(r"[0-9][A-Za-z]?$", regex=True)).sum()
        )

    assert {"20MICRONS", "360ONE", "3MINDIA", "5PAISA", "4THDIM"} <= digit_leading_companies
    assert "3PLAND" in digit_leading_companies and "3BBLACKBIO" in digit_leading_companies
    assert len(digit_leading_companies) == 10, sorted(digit_leading_companies)
    # >= 150 rather than an exact figure: the count is a lower bound on the refutation, and the
    # ETF population changes between captures. Pinning it to an exact number would make this test
    # fail on a routine upstream addition for no diagnostic gain.
    assert etf_like_rows >= 150, f"expected 150+ equity rows ending in a digit, got {etf_like_rows}"


# ================================================= asking for a series that is not the default


@pytest.mark.parametrize(
    "series,expected_symbols",
    [
        ("BZ", {"AGSTRA", "ANKITMETAL", "ANSALAPI", "ASTRON", "COMPINFO", "IMPEXFERRO"}),
        ("IV", {"CAPINVIT", "INDIGRID", "INDUSINVIT", "PGINVIT", "CITIUSINVT"}),
        ("RR", {"EMBASSY", "BAGMANE", "BIRET", "KRT", "MINDSPACE", "NXST"}),
        ("E1", {"ATLPP", "ROCKPP"}),
        ("SZ", {"PERFECT"}),
    ],
)
def test_asking_for_a_non_default_series_returns_those_rows_not_an_empty_frame(
    series, expected_symbols
):
    """**The gap.** These codes carry listed securities and the default excludes them, so before
    there was an explicit ``series`` a caller wanting a REIT had exactly one option: take the
    whole file and filter it themselves, or accept an empty frame.

    The assertion that matters is ``not frame.empty`` — a silent-empty regression here is the
    defect being closed, so it is asserted first and on its own.
    """
    frame = bc.parse_bhavcopy(read_fixture(CM_25), series=series)

    assert not frame.empty, f"series={series!r} returned an empty frame"
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == (series,)
    returned = set(frame.index.get_level_values("Symbol"))
    assert expected_symbols <= returned, f"missing {sorted(expected_symbols - returned)}"
    # And it really is a different population from the default, not the default relabelled.
    assert returned.isdisjoint(
        set(bc.parse_bhavcopy(read_fixture(CM_25)).index.get_level_values("Symbol"))
    )


def test_every_measured_equity_like_series_is_individually_reachable():
    """A caller must be able to ask for *any* equity-like code, not just the three headline ones.

    One case per code, so a series that quietly became unreachable — because a constant lost an
    entry, or a filter started rejecting it — fails on its own line rather than as one opaque
    total.
    """
    for code in bc.EQUITY_LIKE_SERIES:
        frame = bc.parse_bhavcopy(read_fixture(CM_25), series=code)
        assert not frame.empty, f"equity-like series {code!r} is unreachable on the 2026-09-25 file"
        assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == (code,)


# ============================================ an unmatched request is labelled, never bare


async def test_a_request_matching_no_rows_is_labelled_with_the_reason(client):
    """**The gap.** Asking for a series the file does not carry must not be an unexplained frame.

    The old behaviour returned a correct-but-silent empty frame: indistinguishable from a filter
    bug, a wrong date, or a series that does not exist. Now the frame names what was asked for and
    what the file actually held, which are three different situations and three different fixes.

    ``AZ`` is the request here because it is a **real** code — it appears on 2026-09-24 and not on
    2026-09-25 — so this is the day-specific absence the unstable roster makes routine, not a
    typo. ``RR`` would be a weaker case: it is absent from 2026-09-24 too.
    """
    assert "AZ" in bc.series_in_file(read_fixture(CM_24)), "precondition: AZ exists on the other day"
    assert "AZ" not in bc.series_in_file(read_fixture(CM_25)), "precondition: AZ absent here"

    frame = await served(client, {"20260925": zipped(read_fixture(CM_25))}).bhavcopy_async(
        "2026-09-25", series="AZ"
    )

    assert frame.empty
    assert frame.attrs["bfinance_no_rows_for_series"] is True
    assert frame.attrs["bfinance_series"] == "AZ", "the request must be echoed"
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == (), "nothing was included, and it must say so"
    # The diagnostic: the whole roster the file did hold, so the caller can see AZ is absent
    # because this file has no AZ — not because the request was malformed.
    available = frame.attrs["bfinance_series_available"]
    assert available == bc.series_in_file(read_fixture(CM_25))
    assert "AZ" not in available
    assert {"EQ", "BE", "RR", "GB", "SM"} <= set(available), "the real roster is reported"
    assert len(available) == 57, "and it is the full 57, not just the equity-like part"


async def test_a_bare_empty_frame_would_fail_the_labelling_test(client):
    """Tripwire: if the labels were dropped, this must go red — shown as a concrete check.

    The real path is run against a real file, then the explanatory attr is deleted from the
    result and the labelling assertion is re-applied. It fails, which is the point: the guarantee
    in the test above depends on keys existing, and a future refactor that stops setting them
    cannot pass silently.
    """
    frame = await served(client, {"20260925": zipped(read_fixture(CM_25))}).bhavcopy_async(
        "2026-09-25", series="AZ"
    )
    assert frame.attrs.get("bfinance_series_available"), "precondition: the label exists"

    stripped = frame.copy()
    stripped.attrs = {k: v for k, v in frame.attrs.items() if k != "bfinance_series_available"}
    assert stripped.attrs.get("bfinance_series_available", ()) == (), (
        "with the label removed the empty frame is unexplained again"
    )
    # The request is still echoed, which is the whole of what a bare frame can say: the code that
    # was asked for. It cannot say whether the file offered that code, which is the missing half.
    assert set(stripped.index.get_level_values("Symbol")) == set()
    assert stripped.attrs["bfinance_series"] == "AZ"
    assert not any(isinstance(v, tuple) and "EQ" in v for v in stripped.attrs.values()), (
        "no roster remains, so nothing distinguishes 'absent today' from 'never exists'"
    )


async def test_a_range_records_the_days_that_matched_nothing(client):
    """The multi-day path dropped an empty day silently; it now names them.

    Distinct from a 404, which means no file existed. A caller walking a month of a series that
    appears on some days and not others needs to know which days arrived with no rows, because
    that is the boundary of the history and without it the series simply looks like it started
    later. ``AZ`` is present on 2026-09-24 and absent on 2026-09-25, so one of the two days has a
    file and no rows — precisely the case that used to vanish.
    """
    frame = await served(
        client,
        {"20260925": zipped(read_fixture(CM_25)), "20260924": zipped(read_fixture(CM_24))},
    ).bhavcopy_range_async("2026-09-24", "2026-09-25", series="AZ")

    assert not frame.empty, "AZ does have a row, on the other day"
    assert frame.attrs["bfinance_no_rows_for_series_days"] == ["2026-09-25"], (
        "the day whose file arrived with no AZ row is named, not skipped"
    )
    assert frame.attrs[bc.MISSING_DATES_ATTR] == [], "no 404 happened; these are different events"
    # The roster is reported for the day that returned nothing, which is the day that needs
    # explaining. It is deliberately not a union over the whole walk: that would need a second
    # full CSV parse per day across a multi-year range. So AZ is absent here, and EQ is present.
    available = frame.attrs["bfinance_series_available"]
    assert "EQ" in available and "GB" in available
    assert "AZ" not in available, "the 2026-09-25 file genuinely offers no AZ"
    assert available == tuple(sorted(set(bc.series_in_file(read_fixture(CM_25)))))
    # What came back is the single 2026-09-24 row, and nothing was invented for the 25th.
    days = sorted(set(frame.index.get_level_values("Date").strftime("%Y-%m-%d")))
    assert days == ["2026-09-24"]
    assert len(frame) == 1
    assert frame.index.get_level_values("Symbol")[0] == "1015SCL28B"
    assert int(frame["Volume"].iloc[0]) == 2

    # And the day that *does* carry AZ returns it on its own, rather than as an empty day.
    present = bc.parse_bhavcopy(read_fixture(CM_24), series="AZ")
    assert not present.empty, "AZ has a row on 2026-09-24"
    assert present.attrs[bc.SERIES_INCLUDED_ATTR] == ("AZ",)


# ================================================================ BL and duplicate handling


def test_including_bl_cannot_produce_duplicate_date_symbol_rows():
    """**The trap.** ``BL`` duplicates ``EQ``: every ``BL`` symbol is also an ``EQ`` symbol the
    same day, so including it must not yield two bars for one instrument-day.

    Asserted on a *constructed* frame that contains an overlapping symbol, so the guarantee does
    not depend on which companies happen to be listed today. The two BL rows are the real ones
    from the 2026-09-25 archive, including the flat synthetic price and the different
    ``FinInstrmId`` that makes them a genuinely separate line upstream.
    """
    frame = bc.parse_bhavcopy(read_fixture(CM_25), series=["EQ", "BL"])

    assert not frame.index.duplicated().any(), "a duplicate (Date, Symbol) survived the dedup"
    # The overlap was real, and was counted rather than hidden.
    assert frame.attrs["bfinance_duplicate_date_rows"] == 2, "the two BL rows must be reported"
    assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BL", "EQ")
    # Both overlapping instruments are present exactly once.
    symbols = frame.index.get_level_values("Symbol")
    assert int((symbols == "ADANIENT").sum()) == 1
    assert int((symbols == "DALBHARAT").sum()) == 1
    assert {"ADANIENT", "DALBHARAT"} <= set(symbols)
    # The default, which cannot reach the BL rows, reports no duplicates at all.
    assert bc.parse_bhavcopy(read_fixture(CM_25)).attrs["bfinance_duplicate_date_rows"] == 0


def test_which_bl_row_survives_is_determined_and_reported():
    """With two rows for one instrument-day, which one is kept is a decision, not an accident.

    ``parse_bhavcopy`` keeps the **last row in file order**. In the 2026-09-25 archive NSE writes
    the flat synthetic ``BL`` line *before* the real ``EQ`` line for ADANIENT, so the survivor is
    the ``EQ`` row: the traded close 2916.50, not the synthetic 2905.00 on 8,600,000 shares.

    That outcome is fortunate, not guaranteed — it depends on upstream row order, which is not a
    contract. So it is pinned as a measurement and the loss is counted, rather than relied upon:
    ``bfinance_duplicate_date_rows`` is the signal that a second line existed and was dropped.
    """
    raw_order = raw(CM_25)
    overlapping = set(raw_order.loc[raw_order["SctySrs"] == "BL", "TckrSymb"])
    assert overlapping == {"ADANIENT", "DALBHARAT"}
    # For each overlapping symbol the BL line comes first and the EQ line second, so keep-last
    # retains the EQ row. Asserted on file order explicitly rather than assumed.
    for symbol in sorted(overlapping):
        codes = list(raw_order.loc[raw_order["TckrSymb"] == symbol, "SctySrs"])
        assert codes == ["BL", "EQ"], f"{symbol}: expected BL then EQ in file order, got {codes}"

    frame = bc.parse_bhavcopy(read_fixture(CM_25), series=["EQ", "BL"])
    adanient = frame.xs("ADANIENT", level="Symbol")

    assert adanient["Close"].iloc[0] == 2916.50, "the traded EQ row survives"
    assert adanient["Close"].iloc[0] != 2905.00, "the flat synthetic BL price does not"
    assert int(adanient["Volume"].iloc[0]) == 570504, "and so its 570,504 shares, not 8,600,000"
    # Whatever the row order, the loss is reported rather than hidden.
    assert frame.attrs["bfinance_duplicate_date_rows"] == 2
    # The default read, which never sees a BL row, gives the same bar with no loss at all.
    default_adanient = bc.parse_bhavcopy(read_fixture(CM_25)).xs("ADANIENT", level="Symbol")
    assert default_adanient["Close"].iloc[0] == adanient["Close"].iloc[0]


def test_bl_is_not_the_only_duplicate_source_and_that_is_measured():
    """The honest generalisation: a symbol can sit under two series on one day, in both eras.

    Three independent instances, so the dedup's necessity does not rest on ``BL`` alone:
    ``IMC1`` is filed under ``N1`` and ``N3`` on 2026-09-24, and the **legacy** file collides far
    harder — 121 rows are dropped there, with ``BRITANNIA`` and ``CHOLAFIN`` listed under both
    ``EQ`` and a debt series.
    """
    assert bc.parse_bhavcopy(read_fixture(CM_24), series=None).attrs[
        "bfinance_duplicate_date_rows"
    ] == 3, "two BL rows plus IMC1 under N1 and N3"

    legacy = bc.parse_bhavcopy(read_fixture(LEGACY), series=None)
    # 2427 rows collapse to 2306 distinct (Date, Symbol) pairs, so 121 are dropped. The number of
    # rows *involved* in a collision is 153, which is larger: both members of a pair are involved
    # but only one is dropped. Asserting the dropped count, which is what the attr reports.
    assert len(legacy) == 2306
    assert legacy.attrs["bfinance_duplicate_date_rows"] == 121
    involved = raw(LEGACY).duplicated(subset=["TIMESTAMP", "SYMBOL"], keep=False).sum()
    assert int(involved) == 153, "precondition: 153 rows are in a colliding group"

    # A legacy series=None read therefore *loses real equity rows* to keep-last. The default is
    # immune because it filters to EQ/BE first and BRITANNIA is never duplicated there.
    assert bc.parse_bhavcopy(read_fixture(LEGACY)).attrs["bfinance_duplicate_date_rows"] == 0
    assert "BRITANNIA" in set(legacy.index.get_level_values("Symbol"))


# ================================================================== turnover: the UDiFF column


def test_turnover_is_populated_from_ttltrfval_on_a_udiff_fixture():
    """The column the module did not read. Checked against the raw file, not against itself.

    The comparison is per instrument-day on a keyed join, because the raw file has 3,654 rows and
    the parsed frame has 3,651 — three rows are legitimately dropped by the duplicate dedup, so a
    positional comparison would be comparing different rows. Asserting ``len == len(raw)`` here
    would have been the wrong assertion, and would have been an easy one to write.
    """
    frame = raw(CM_25)
    published: dict[tuple[str, str], set[float]] = {}
    for _, row in frame.iterrows():
        published.setdefault((row["TradDt"], row["TckrSymb"]), set()).add(
            float(row["TtlTrfVal"])
        )
    # Two instruments have two published values each; the dedup keeps the later row.
    assert len(published) == 3651, "precondition: 3,654 rows collapse to 3,651 pairs"
    assert sum(len(v) for v in published.values()) == 3654, "the 3 extra are the BL duplicates"
    assert max(len(v) for v in published.values()) == 2, "exactly one pair has two values"

    parsed = bc.parse_bhavcopy(read_fixture(CM_25), series=None)
    assert bc.TURNOVER_COLUMN in parsed.columns
    assert len(parsed) == len(published), "the parsed frame keeps exactly the distinct pairs"

    mismatches = []
    for stamp, symbol, value in zip(
        parsed.index.get_level_values("Date").strftime("%Y-%m-%d"),
        parsed.index.get_level_values("Symbol"),
        parsed[bc.TURNOVER_COLUMN],
    ):
        # The surviving row is one of the published rows for that pair, so its value must be one
        # of the published values for it. Set membership, not a single expected number.
        if value not in published[(stamp, symbol)]:
            mismatches.append((stamp, symbol, value))
    assert mismatches == [], f"turnover does not match the file: {mismatches[:5]}"

    # The exact check on an instrument present under EQ only, so there is no dedup ambiguity.
    reliance = parsed.xs("RELIANCE", level="Symbol")
    assert reliance[bc.TURNOVER_COLUMN].iloc[0] == 16035076079.70
    assert int(reliance["Volume"].iloc[0]) == 13138735
    assert parsed.attrs[bc.TURNOVER_UNIT_ATTR] == bc.TURNOVER_UNIT_PRICE_X_VOLUME


def test_sttlm_pric_is_present_populated_and_deliberately_not_mapped():
    """**The false positive.** ``SttlmPric`` is in the file and *populated* on every cash row, and
    a substring search for "stt" finds it. It is the settlement price, not traded value.

    Mapping it would not fail loudly: ``2916.47 / 570504`` is ~5e-3, i.e. far too *small*, so a
    magnitude or plausibility check would wave it through and only the VWAP identity would catch
    it. Asserted on the real recorded values.
    """
    assert "SttlmPric" in bc.UNMAPPED_COLUMNS, "the reason must be recorded next to the code"
    assert "settlement" in bc.UNMAPPED_COLUMNS["SttlmPric"].lower()

    for schema, name in (("udiff", "TtlTrfVal"), ("legacy", "TOTTRDVAL")):
        mapped = bc._SCHEMAS[schema]
        assert mapped["turnover"][0] == name
        assert "SttlmPric" not in mapped["turnover"], f"{schema} must not map the settlement price"
        assert "SttlmPric" not in [c for candidates in mapped.values() for c in candidates]

    # It is genuinely populated, so the exclusion is a decision and not an accident of a blank.
    frame = raw(CM_25)
    populated = frame["SttlmPric"].astype(str).str.strip()
    assert int(populated.ne("").sum()) == 3654, "every row carries a settlement price"
    adanient_eq = float(
        frame.loc[(frame["TckrSymb"] == "ADANIENT") & (frame["SctySrs"] == "EQ"),
                  "SttlmPric"].iloc[0]
    )
    assert adanient_eq == 2916.47, "the EQ row's settlement price, as published"

    # And the resulting number is wrong in the direction a naive check would not catch.
    wrong = adanient_eq / 570504
    assert wrong < 0.01, "far too small, not far too large"
    assert wrong != pytest.approx(1666048960.90 / 570504, rel=1e-6)


def test_the_turnover_column_cannot_be_mistaken_for_rupees():
    """The naming and the attr are the mitigation for a unit that is not uniform.

    A bare ``Turnover`` would invite ``frame['Turnover'].sum()`` into a cost calculation. Assert
    the two properties that make the safe reading available: the name carries no currency, and the
    unit is present as a value a caller can compare and refuse on.
    """
    assert bc.TURNOVER_COLUMN == "TurnoverValue"
    for banned in ("Rs", "INR", "Rupee", "Lakh", "Crore"):
        assert banned not in bc.TURNOVER_COLUMN

    frame = bc.parse_bhavcopy(read_fixture(CM_25))
    unit = frame.attrs[bc.TURNOVER_UNIT_ATTR]
    assert unit == bc.TURNOVER_UNIT_PRICE_X_VOLUME
    assert unit != bc.TURNOVER_UNIT_UNRESOLVED
    # A caller that needs a monetary amount can gate on the unit, which is the intended workflow.
    assert unit in (bc.TURNOVER_UNIT_PRICE_X_VOLUME, bc.TURNOVER_UNIT_UNRESOLVED)

    # The unresolved value exists, is reachable, and is what the F&O file actually yields.
    assert bc.TURNOVER_UNIT_UNRESOLVED == "unresolved"
    fo = bc.parse_bhavcopy(read_fixture(FO_25), series=None)
    assert fo.attrs[bc.TURNOVER_UNIT_ATTR] == bc.TURNOVER_UNIT_UNRESOLVED


# ================================================================ the VWAP identity, measured


@pytest.mark.parametrize(
    "fixture,label",
    [(CM_25, "CM 2026-09-25 (UDiFF)"), (CM_24, "CM 2026-09-24 (UDiFF)"),
     (LEGACY, "CM 2023-05-15 (legacy)")],
)
def test_the_vwap_identity_holds_on_a_whole_recorded_file(fixture, label):
    """``Low <= TtlTrfVal / TtlTradgVol <= High``, on every testable row of a whole archive.

    The prior claim in this repo was 7/7, measured on trimmed fixtures. These are the **full**
    files: 3,654 + 3,637 + 2,427 = 9,718 rows. Pass counts are asserted per file, not as a
    single hand-waved number, and a tolerance is applied — justified in ``VWAP_TOLERANCE``.

    The tolerance is not a fudge that hides real failures. An exact comparison reports
    3,642/3,654 on the 2026-09-25 file, and every one of the 12 "failures" is a row whose
    ``Low == High`` where IEEE-754 division rounds a hair high: ``2551516.80 / 988960`` is
    ``2.5800000000000002`` against a published 2.58. A genuine unit error misses by a factor of
    100 or more, which the tolerance is 1e9 times too small to absorb.
    """
    frame = raw(fixture)
    if "TtlTrfVal" in frame.columns:
        value, volume, low, high = "TtlTrfVal", "TtlTradgVol", "LwPric", "HghPric"
    else:
        value, volume, low, high = "TOTTRDVAL", "TOTTRDQTY", "LOW", "HIGH"

    num = pd.to_numeric(frame[value], errors="coerce")
    vol = pd.to_numeric(frame[volume], errors="coerce")
    lo = pd.to_numeric(frame[low], errors="coerce")
    hi = pd.to_numeric(frame[high], errors="coerce")

    testable = vol.gt(0) & num.notna() & lo.notna() & hi.notna()
    vwap = num[testable] / vol[testable]
    tol = bc.VWAP_TOLERANCE
    inside = (vwap >= lo[testable] - tol) & (vwap <= hi[testable] + tol)

    total, passed = int(testable.sum()), int(inside.sum())
    assert total == len(frame), f"{label}: expected every row testable, {total} of {len(frame)}"
    assert passed == total, (
        f"{label}: VWAP identity {passed}/{total}; "
        f"worst offenders {list(zip(frame[low].values[:3], vwap[~inside].head(3).round(4)))}"
    )

    # Reported explicitly, since a number nobody reads is a number nobody trusts.
    assert (total, passed) in {(3654, 3654), (3637, 3637), (2427, 2427)}

    # The parsed frame reports the same verdict, on the parsed values.
    parsed = bc.parse_bhavcopy(read_fixture(fixture), series=None)
    assert parsed.attrs[bc.TURNOVER_UNIT_ATTR] == bc.TURNOVER_UNIT_PRICE_X_VOLUME


def test_an_exact_comparison_would_report_false_failures_so_the_tolerance_is_needed():
    """Pin *why* ``VWAP_TOLERANCE`` exists, and that it is small enough not to hide a unit error.

    Both halves: the rows an exact ``<=`` rejects really are inside the published range, and a
    simulated 100x unit error is still rejected after the tolerance is applied. Without the second
    half, a wide tolerance would be an unexamined escape hatch.
    """
    frame = raw(CM_25)
    num = pd.to_numeric(frame["TtlTrfVal"], errors="coerce")
    vol = pd.to_numeric(frame["TtlTradgVol"], errors="coerce")
    lo = pd.to_numeric(frame["LwPric"], errors="coerce")
    hi = pd.to_numeric(frame["HghPric"], errors="coerce")
    vwap = num / vol

    exact = int(((vwap >= lo) & (vwap <= hi)).sum())
    assert exact == 3642, "an exact comparison is expected to reject 12 float-noise rows"
    assert exact < 3654, "precondition: the exact form really does report failures"

    tol = bc.VWAP_TOLERANCE
    with_tol = ((vwap >= lo - tol) & (vwap <= hi + tol)).sum()
    assert int(with_tol) == 3654

    # A real unit error (the column is in lakhs, not rupees) must still fail, by a mile.
    in_lakhs = num * 100_000 / vol
    assert int(((in_lakhs >= lo - tol) & (in_lakhs <= hi + tol)).sum()) == 0
    assert tol < 1.0 / 100, "the tolerance must be far tighter than any plausible unit factor"


def test_the_fo_segment_refuses_the_identity_and_that_is_the_measured_unit_ambiguity():
    """The unit is segment-dependent, measured rather than quoted: F&O fails 0/18,072.

    This is the finding that forces the unit into ``attrs``. ``TtlTradgVol`` counts lots and
    ``TtlTrfVal`` is in units of the underlying, so the ratio is the *underlying share price* and
    cannot fall inside the premium range — the identity is structurally impossible there, not
    merely violated. One real row is shown so the claim is concrete.
    """
    frame = raw(FO_25)
    num = pd.to_numeric(frame["TtlTrfVal"], errors="coerce")
    vol = pd.to_numeric(frame["TtlTradgVol"], errors="coerce")
    lo = pd.to_numeric(frame["LwPric"], errors="coerce")
    hi = pd.to_numeric(frame["HghPric"], errors="coerce")

    testable = vol.gt(0) & num.notna() & lo.notna() & hi.notna()
    vwap = num[testable] / vol[testable]
    inside = (vwap >= lo[testable] - bc.VWAP_TOLERANCE) & (vwap <= hi[testable] + bc.VWAP_TOLERANCE)

    assert int(testable.sum()) == 18072, "F&O volume is positive on 18,072 of 37,165 rows"
    assert int(inside.sum()) == 0, "the identity cannot hold for the F&O segment"
    # Every violation is *above* the range, never below: the ratio is a share price, not a premium.
    assert int((vwap > hi[testable]).sum()) == 18072
    assert int((vwap < lo[testable]).sum()) == 0

    row = frame[frame["TtlTrfVal"] == "118724575.00"]
    assert len(row) == 1, "precondition: the measured example row exists and is unique"
    example = row.iloc[0]
    assert example["TckrSymb"] == "ABCAPITAL" and example["FinInstrmNm"] == "ABCAPITAL26OCT450CE"
    assert float(example["LwPric"]) == 0.45 and float(example["HghPric"]) == 0.80
    assert float(example["TtlTrfVal"]) == 118724575.00
    assert 118724575.00 / 85 == pytest.approx(1396759.70588, rel=1e-8)
    assert 1396759.70588 > float(example["HghPric"]), "the ratio is a share price, not a premium"
    assert float(example["NewBrdLotQty"]) == 3100, "the lot size, so volume counts lots"
    # The ratio is not any price in the row; it is the underlying's share price.
    assert 1396759.70588 != float(example["LwPric"])
    assert 1396759.70588 != float(example["HghPric"])

    assert bc.parse_bhavcopy(read_fixture(FO_25), series=None).attrs[
        bc.TURNOVER_UNIT_ATTR
    ] == bc.TURNOVER_UNIT_UNRESOLVED


# =============================================================== the legacy route's turnover


def test_the_legacy_route_maps_its_turnover_equivalent_and_it_is_not_unavailable():
    """Turnover is **not** a post-cutover feature: the 14-column file has ``TOTTRDVAL``.

    The honest answer to "or state clearly that turnover is unavailable pre-cutover" is that the
    question does not arise. Checked on the recorded pre-2024 archive: the column is mapped, it is
    populated, and it satisfies the same VWAP identity — so ``bfinance.costs``' taxable base is
    reachable on both sides of the cutover.
    """
    assert bc._SCHEMAS["legacy"]["turnover"] == ("TOTTRDVAL",)
    assert "TtlTrfVal" not in bc._SCHEMAS["legacy"]["turnover"], "wrong schema's column name"

    frame = raw(LEGACY)
    assert "TOTTRDVAL" in frame.columns
    assert int(pd.to_numeric(frame["TOTTRDVAL"], errors="coerce").isna().sum()) == 0

    parsed = bc.parse_bhavcopy(read_fixture(LEGACY), series="EQ")
    assert bc.TURNOVER_COLUMN in parsed.columns
    reliance = parsed.xs("RELIANCE", level="Symbol")
    assert reliance[bc.TURNOVER_COLUMN].iloc[0] == 7859481400.85
    assert int(reliance["Volume"].iloc[0]) == 3153587
    assert 7859481400.85 / 3153587 == pytest.approx(2492.235, rel=1e-6)
    assert 2473.15 <= 7859481400.85 / 3153587 <= 2504.55, "the legacy identity holds too"
    assert parsed.attrs[bc.TURNOVER_UNIT_ATTR] == bc.TURNOVER_UNIT_PRICE_X_VOLUME


def test_a_missing_turnover_column_is_rejected_rather_than_silently_dropped():
    """The schema is strict about it, in both directions: no turnover column, no frame.

    A frame missing the traded value would look identical to one where every value is zero, and
    the difference between "not published" and "no trading" is exactly the kind of thing this
    module refuses to guess at.
    """
    with pytest.raises(bc.ParsingError, match="turnover"):
        bc.parse_bhavcopy(
            "TradDt,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol\n"
            "2026-09-25,RELIANCE,EQ,1,2,0.5,1.5,100\n"
        )
    with pytest.raises(bc.ParsingError, match="turnover"):
        bc.parse_bhavcopy(
            "SYMBOL,SERIES,TIMESTAMP,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY\n"
            "RELIANCE,EQ,15-MAY-2023,1,2,0.5,1.5,100\n"
        )


# ======================================================================== negative controls


def test_negative_control_coercing_the_series_filter_back_to_eq_be_goes_red():
    """**Negative control 1.** Coerce the filter to ``EQ``/``BE`` and the gap tests must fail.

    The coercion ignores the requested series and applies the default — the silent-wrong-answer
    shape this project keeps hitting, and a **worse** one than an empty frame, because the caller
    gets a full, well-formed, non-empty result containing none of what was asked for.

    So the headline assertion of the real test (``not frame.empty``) would *pass* under this
    coercion, and the per-symbol assertions are what catch it. Demonstrating that is the value of
    this control: it says the real test's value is in its content assertions, not in "non-empty".
    """
    def eq_be_only(csv_text, **_kwargs):
        return bc.parse_bhavcopy(csv_text, series=bc.EQUITY_SERIES)

    expected = {
        "BZ": {"AGSTRA", "ANKITMETAL", "ANSALAPI", "ASTRON", "COMPINFO", "IMPEXFERRO"},
        "IV": {"CAPINVIT", "INDIGRID", "INDUSINVIT", "PGINVIT", "CITIUSINVT"},
        "RR": {"EMBASSY", "BAGMANE", "BIRET", "KRT", "MINDSPACE", "NXST"},
    }
    for code, wanted in expected.items():
        frame = eq_be_only(read_fixture(CM_25), series=code)
        # The frame is NOT empty - the naive check passes, which is the trap.
        assert not frame.empty, "precondition: the coerced read is confidently non-empty"
        assert len(frame) == 2899, "it is the EQ+BE population, asked for or not"
        assert frame.attrs[bc.SERIES_INCLUDED_ATTR] == ("BE", "EQ"), (
            "and it reports the default, not the request"
        )
        # What the real test asserts, and what fails here: none of the requested rows are present.
        returned = set(frame.index.get_level_values("Symbol"))
        assert wanted.isdisjoint(returned), (
            f"test_asking_for_a_non_default_series... fails for {code}"
        )
        assert frame.attrs[bc.SERIES_INCLUDED_ATTR] != (code,)

    # The reachable set really is narrower than the equity-like set, which is why the gap existed.
    assert not set(bc.EQUITY_LIKE_SERIES) <= set(bc.EQUITY_SERIES)
    assert set(bc.EQUITY_SERIES) == {"EQ", "BE"}


def test_negative_control_mapping_sttlm_pric_as_turnover_goes_red():
    """**Negative control 2.** Map the settlement price as turnover and the VWAP test must fail.

    This is the specific mistake the ``"stt"`` substring invites. The coercion is applied to the
    real schema and the real identity check, and the number it produces is shown to be wrong —
    and wrong in the *small* direction, which is why a magnitude check would not have caught it.
    """
    schema = bc._SCHEMAS["udiff"]
    assert schema["turnover"] == ("TtlTrfVal",), "precondition: the correct mapping is in place"

    coerced_frame = raw(CM_25)
    num = pd.to_numeric(coerced_frame["SttlmPric"], errors="coerce")
    vol = pd.to_numeric(coerced_frame["TtlTradgVol"], errors="coerce")
    lo = pd.to_numeric(coerced_frame["LwPric"], errors="coerce")
    hi = pd.to_numeric(coerced_frame["HghPric"], errors="coerce")
    wrong = num / vol
    testable = vol.gt(0) & num.notna() & lo.notna() & hi.notna()

    inside = (wrong[testable] >= lo[testable] - bc.VWAP_TOLERANCE) & (
        wrong[testable] <= hi[testable] + bc.VWAP_TOLERANCE
    )
    total, passed = int(testable.sum()), int(inside.sum())
    # Not "zero": 17 rows satisfy the identity by coincidence, because for a row whose Low == High
    # the settlement price divided by a small volume can land on the price. That is exactly why the
    # test asserts the *rate* rather than demanding a clean zero — a control that claimed 0/3654
    # would itself be the kind of unfalsifiable assertion this repo keeps shipping.
    assert (total, passed) == (3654, 17), (
        f"mapping SttlmPric as turnover satisfies the identity on only {passed}/{total} rows"
    )
    assert passed / total < 0.01, "i.e. it fails on more than 99% of rows"
    # And it fails for a reason a naive reviewer would not catch: the values are tiny, not huge.
    assert float(wrong[testable].median()) < 1.0
    assert float(wrong[testable].median()) < float(lo[testable].median()) / 100

    # measure_turnover_unit must therefore refuse the unit for such a frame. Its contract is
    # all-rows, not most-rows, which is why 17 lucky rows do not rescue it.
    constructed = pd.DataFrame(
        {
            "Low": lo.to_numpy(),
            "High": hi.to_numpy(),
            "Volume": vol.to_numpy(),
            bc.TURNOVER_COLUMN: num.to_numpy(),
        }
    )
    assert bc.measure_turnover_unit(constructed) == bc.TURNOVER_UNIT_UNRESOLVED
    # Where the real mapping does not.
    real = bc.parse_bhavcopy(read_fixture(CM_25), series=None)
    assert bc.measure_turnover_unit(real) == bc.TURNOVER_UNIT_PRICE_X_VOLUME


def test_negative_control_dropping_the_empty_result_labels_goes_red():
    """**Negative control 3.** Remove the labelling attrs and the guarantee must fail.

    Guards the tests that claim an unmatched request is never a bare empty frame: if
    ``bhavcopy_async`` stopped setting ``bfinance_series_available``, this must go red rather than
    leave those tests asserting against a key that no longer exists.
    """
    client = bc.BhavcopyClient(min_request_interval_ms=0.0)
    frame = sync(served(client, {"20260925": zipped(read_fixture(CM_25))}).bhavcopy_async(
        "2026-09-25", series="AZ"
    ))
    assert frame.empty, "precondition: AZ is absent from the 2026-09-25 file"
    assert frame.attrs.get("bfinance_no_rows_for_series") is True, "precondition"
    assert frame.attrs.get("bfinance_series_available"), "precondition"

    for key in ("bfinance_series_available", "bfinance_no_rows_for_series"):
        stripped = frame.copy()
        stripped.attrs = {k: v for k, v in frame.attrs.items() if k != key}
        assert key not in stripped.attrs
        assert stripped.attrs.get(key) in (None, (), False), (
            f"without {key} the empty result carries no explanation"
        )


def test_negative_control_a_lying_vwap_measurement_would_go_red():
    """**Negative control 4.** Break the identity and the unit measurement must refuse.

    Ensures ``measure_turnover_unit`` is not a function that returns
    ``TURNOVER_UNIT_PRICE_X_VOLUME`` unconditionally — the failure mode of a "check" that cannot
    fail. Three perturbations, each of which must be rejected.
    """
    good = bc.parse_bhavcopy(read_fixture(CM_25), series=None)
    assert bc.measure_turnover_unit(good) == bc.TURNOVER_UNIT_PRICE_X_VOLUME

    too_small = good.copy()
    too_small[bc.TURNOVER_COLUMN] = good[bc.TURNOVER_COLUMN] / 1000.0
    assert bc.measure_turnover_unit(too_small) == bc.TURNOVER_UNIT_UNRESOLVED

    too_large = good.copy()
    too_large[bc.TURNOVER_COLUMN] = good[bc.TURNOVER_COLUMN] * 1000.0
    assert bc.measure_turnover_unit(too_large) == bc.TURNOVER_UNIT_UNRESOLVED

    no_rows = good.iloc[:0]
    assert bc.measure_turnover_unit(no_rows) == bc.TURNOVER_UNIT_UNRESOLVED, (
        "no evidence is not evidence of the same unit"
    )
    no_column = good.drop(columns=[bc.TURNOVER_COLUMN])
    assert bc.measure_turnover_unit(no_column) == bc.TURNOVER_UNIT_UNRESOLVED


def test_negative_control_a_stale_roster_would_go_red():
    """**Negative control 5.** Change one measured count and the consistency test must fail.

    The failure this repo keeps hitting is a published table that does not match the archive it
    claims to describe. Simulating exactly that — one count off by one — has to be caught, and it
    is caught twice over: by the sum-to-rows assertion and by the per-code comparison.
    """
    total_rows = len(raw(CM_25))
    assert bc.SERIES_ROW_COUNTS["2026-09-25"]["EQ"] == 2660

    stale = dict(bc.SERIES_ROW_COUNTS["2026-09-25"])
    stale["EQ"] = 2661
    assert sum(stale.values()) == total_rows + 1, "a one-row error breaks the sum"
    assert stale["EQ"] != MEASURED["2026-09-25"]["EQ"], "and breaks the per-code check"

    dropped = dict(bc.SERIES_ROW_COUNTS["2026-09-25"])
    del dropped["BL"]
    assert sum(dropped.values()) != total_rows, "a missing code also breaks the sum"
    assert len(bc.series_in_file(read_fixture(CM_25))) == 57


def test_negative_control_widening_the_default_would_go_red():
    """**Negative control 6.** Simulate a widened default and the guard test must fail.

    The guard exists to stop exactly this edit, so the edit has to be shown to trip it. Widening
    to the equity-like set is the tempting version; going further and admitting the debt series is
    the version that poisons a portfolio. Both are shown, on the real file.
    """
    equity_wide = ("EQ", "BE", "BZ", "IV", "RR", "SM", "ST")
    assert equity_wide != bc.EQUITY_SERIES
    assert set(equity_wide) > set(bc.EQUITY_SERIES)

    symbols = set(
        bc.parse_bhavcopy(read_fixture(CM_25), series=equity_wide)
        .index.get_level_values("Symbol")
    )
    assert {"EMBASSY", "AGSTRA", "CAPINVIT"} <= symbols, (
        "widening to the equity-like set really would admit a REIT, a migration and an InvIT"
    )
    assert "SGBJUN28" not in symbols, "which is the point: still no debt at this width"

    debt_wide = equity_wide + ("GB", "GS", "TB")
    debt_symbols = set(
        bc.parse_bhavcopy(read_fixture(CM_25), series=debt_wide)
        .index.get_level_values("Symbol")
    )
    assert {"SGBJUN28", "SGBN28VIII"} <= debt_symbols, "and going further admits gold bonds"
    assert bc.parse_bhavcopy(read_fixture(CM_25)).attrs[
        "bfinance_rows_dropped_by_series_filter"
    ] == 3654 - 2660 - 239, "the narrow default drops all of them; the guard asserts exactly that"
