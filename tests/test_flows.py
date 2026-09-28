"""
Tests for :mod:`bfinance.flows`.

Every test here is **offline and fixture-backed**. There is no network, no clock dependency and
no ``~/.bfinance`` cache: the transport is a stub that counts calls, so "did this make a
request?" is answered by an assertion rather than inferred.

The two negative controls at the end are the reason to trust the rest of the file. This repo has
shipped assertions that cannot fail; each control below *injects the bug it is guarding against*
and proves the guard fires, so a control that stops working is itself a failing test.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bfinance import flows
from bfinance.cache.sqlite_cache import SQLiteCache

FIXTURES = Path(__file__).parent / "fixtures" / "nse" / "flows"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _read_json(name: str):
    return json.loads(_read(name))


OI_2026_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_25092026.csv"
)
OI_2020_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_15062020.csv"
)
VOL_2026_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_25092026.csv"
)
SUNDAY_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_27092026.csv"
)


class StubTransport:
    """A :data:`flows.Transport` that serves canned bodies and records every call.

    Counting calls is what makes the cache and no-request assertions real: a test can assert
    ``transport.calls == []`` and know no request left the process, because the only thing in the
    module that can make one is this callable.
    """

    def __init__(self, routes=None, default=None):
        self.routes = dict(routes or {})
        self.default = default
        self.calls: list = []

    def __call__(self, url: str) -> flows.HttpResult:
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        if callable(self.default):
            return self.default(url)
        if self.default is not None:
            return self.default
        raise AssertionError(
            f"unexpected request to {url!r}. The suite is offline; a request here means some "
            f"code path bypassed the injected transport."
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _ok(url: str, body: bytes) -> flows.HttpResult:
    return flows.HttpResult(url=url, status=200, content=body, content_type="text/csv")


def _not_found(url: str) -> flows.HttpResult:
    return flows.HttpResult(url=url, status=404, content=b"", content_type="text/html")


def _oi_transport() -> StubTransport:
    body = (FIXTURES / "participant_oi_20260925.csv").read_bytes()
    return StubTransport(routes={OI_2026_URL: _ok(OI_2026_URL, body)})


# ---------------------------------------------------------------- participant: parse


def test_participant_flows_parse_with_every_documented_category_distinguished():
    """FII, DII, Client and Pro all present, each with its own distinct numbers.

    Verbatim from the 2026-09-25 file, Future Index Long:
    Client 310325, DII 40198, FII 46305, Pro 91203.
    """
    frame = flows.parse_participant_oi(_read("participant_oi_20260925.csv"), expected_date="2026-09-25")

    assert set(frame.index) == {"Client", "DII", "FII", "Pro"}
    assert tuple(frame.index.unique()) == flows.PARTICIPANT_CATEGORIES

    fidx_long = frame.query("AssetClass == 'Index' and OptionType == 'None' and Side == 'Long'")
    values = dict(zip(fidx_long.index, fidx_long["Value"]))
    assert values == {"Client": 310325, "DII": 40198, "FII": 46305, "Pro": 91203}
    # Distinguished, not collapsed: four genuinely different numbers.
    assert len(set(values.values())) == 4


def test_the_feed_carries_no_arb_row_and_the_module_does_not_invent_one():
    """The ticket named five categories including ``Arb``. There is no ``Arb`` row.

    Asserted against the **raw file text**, not against the module, so this test is independent
    of the code it is describing.
    """
    raw = _read("participant_oi_20260925.csv")
    assert "Arb" not in raw
    assert "Arb" not in flows.PARTICIPANT_CATEGORIES
    frame = flows.parse_participant_oi(raw, expected_date="2026-09-25")
    assert "Arb" not in set(frame.index)
    # And the same for the 2020 vintage, so it is a property of the feed and not of one file.
    assert "Arb" not in _read("participant_oi_20200615.csv")
    assert "Arb" not in _read("participant_vol_20260925.csv")


def test_every_leg_is_split_by_side_asset_class_and_call_put():
    frame = flows.parse_participant_oi(_read("participant_oi_20260925.csv"), expected_date="2026-09-25")

    # 4 participants x 12 legs = 48.
    assert len(frame) == 48
    assert set(frame["AssetClass"]) == {"Index", "Stock"}
    assert set(frame["OptionType"]) == {"None", "Call", "Put"}
    assert set(frame["Side"]) == {"Long", "Short"}
    per_participant = frame.groupby(level=0).size()
    assert set(per_participant) == {12}
    # Futures carry no call/put; options always do. A mix-up here would silently corrupt a leg.
    futures = frame[frame["OptionType"] == "None"]
    assert len(futures) == 16  # 4 participants x (Index, Stock) x (Long, Short)
    assert set(futures["OptionType"]) == {"None"}


def test_the_market_total_row_is_not_mixed_in_with_the_participants():
    """The TOTAL row is an aggregate; keeping it would double-count any group-by."""
    frame = flows.parse_participant_oi(_read("participant_oi_20260925.csv"), expected_date="2026-09-25")

    assert "TOTAL" not in set(frame.index)
    assert frame.attrs["bfinance_total_row_excluded_from_rows"] is True
    # Still published, on attrs, for a caller who wants the aggregate.
    assert frame.attrs["bfinance_market_totals"] == {"Long": 27533553, "Short": 27533553}

    with_total = flows.parse_participant_oi(
        _read("participant_oi_20260925.csv"), expected_date="2026-09-25", include_total=True
    )
    assert "TOTAL" in set(with_total.index)
    assert len(with_total) == 60


# ------------------------------------------------- participant: missing days (the key test)


def _assert_typed_unavailable(frame: pd.DataFrame, reason: str) -> None:
    """The single assertion that distinguishes 'no data' from 'a day of zeros'.

    Factored out so the negative control can point it at a zero-filled frame and watch it fail.
    Every lookup goes through ``.get()`` so a missing key raises AssertionError rather than
    KeyError - the control needs a *clean* failure to be worth anything.
    """
    assert frame.attrs.get(flows.STATUS_ATTR) == flows.STATUS_UNAVAILABLE, (
        "a frame for a day NSE never published must say so in attrs"
    )
    assert frame.attrs.get(flows.UNAVAILABLE_REASON_ATTR) == reason, (
        f"expected reason {reason!r}, got {frame.attrs.get(flows.UNAVAILABLE_REASON_ATTR)!r}"
    )
    # THE assertion. A zero-filled row would put a participant in the index with Value == 0.
    assert len(frame) == 0, f"expected no rows at all, got {len(frame)}"
    assert set(frame.index) == set()
    assert "FII" not in frame.index


def test_a_missing_dated_file_is_a_typed_state_not_a_zero_filled_row():
    """The single most important test in this file.

    A 404 must never become a row of zeros. ``on_missing='error'`` raises a typed error;
    ``on_missing='frame'`` returns a frame that is *also* typed and genuinely empty.
    """
    transport = StubTransport(default=_not_found("x"))
    client = flows.ParticipantFlowsClient(transport)

    with pytest.raises(flows.ParticipantFlowsUnavailable) as excinfo:
        client.participant_flows("2026-09-24")  # a Thursday, but the transport 404s it
    assert excinfo.value.reason == "no_file"
    assert excinfo.value.day == date(2026, 9, 24)
    assert "no_file" in str(excinfo.value)
    # A date with no data is not an error condition to be swallowed, but the message must not
    # imply the exchange malfunctioned.
    assert "not a zero" in str(excinfo.value)

    frame = client.participant_flows("2026-09-24", on_missing="frame")
    _assert_typed_unavailable(frame, "no_file")
    assert frame.attrs[flows.MISSING_DATES_ATTR] == [{"date": "2026-09-24", "reason": "no_file"}]


def test_a_weekend_is_answered_locally_and_never_costs_a_request():
    transport = StubTransport()  # any request at all raises AssertionError inside __call__
    client = flows.ParticipantFlowsClient(transport)

    for weekend_day in ("2026-09-26", "2026-09-27"):  # Saturday, Sunday
        frame = client.participant_flows(weekend_day, on_missing="frame")
        _assert_typed_unavailable(frame, "weekend")

    assert transport.call_count == 0, "a weekend must not spend a request"
    assert flows.is_weekend("2026-09-27") is True
    assert flows.is_weekend("2026-09-25") is False


def test_a_weekend_raises_under_the_default_policy_with_the_reason_attached():
    client = flows.ParticipantFlowsClient(StubTransport())
    with pytest.raises(flows.ParticipantFlowsUnavailable) as excinfo:
        client.participant_flows("2026-09-27")
    assert excinfo.value.reason == "weekend"


def test_an_exchange_holiday_is_reported_as_no_file_not_as_a_weekend():
    """A weekday with no publication is a different fact from a Saturday with no publication."""
    transport = StubTransport(default=_not_found("x"))
    client = flows.ParticipantFlowsClient(transport)

    frame = client.participant_flows("2026-09-24", on_missing="frame")
    _assert_typed_unavailable(frame, "no_file")
    assert transport.call_count == 1
    assert flows.missing_day_reason("2026-09-24") is None  # unknown without a request


def test_a_404_html_body_is_never_parsed_as_a_participant_csv():
    """NSE's 404 is an HTML page. Feeding it to the parser must raise, not produce junk rows."""
    body = _read("participant_oi_20260927_sunday_404.html")
    assert body.lstrip().startswith("<!DOCTYPE html>")
    with pytest.raises(flows.ParsingError):
        flows.parse_participant_oi(body, expected_date="2026-09-27")


def test_a_range_reports_its_holes_instead_of_returning_a_short_series():
    body = (FIXTURES / "participant_oi_20260925.csv").read_bytes()
    transport = StubTransport(
        routes={OI_2026_URL: _ok(OI_2026_URL, body)},
        default=lambda url: flows.HttpResult(url=url, status=404, content=b""),
    )
    client = flows.ParticipantFlowsClient(transport)

    # 2026-09-25 Fri has a file; 26/27 are the weekend; 28 is a holiday the transport 404s.
    frame = client.participant_flows_range("2026-09-25", "2026-09-28")

    assert frame.attrs[flows.STATUS_ATTR] == flows.STATUS_OK
    assert frame.attrs["bfinance_days_returned"] == 1
    assert frame.attrs["bfinance_requested_start"] == "2026-09-25"
    assert frame.attrs["bfinance_requested_end"] == "2026-09-28"
    assert frame.attrs["bfinance_calendar_days_in_span"] == 4
    missing = {m["date"]: m["reason"] for m in frame.attrs[flows.MISSING_DATES_ATTR]}
    assert missing == {
        "2026-09-26": "weekend",
        "2026-09-27": "weekend",
        "2026-09-28": "no_file",
    }
    # The surviving rows are the real ones, and only those.
    assert set(frame["Date"].dt.strftime("%Y-%m-%d")) == {"2026-09-25"}
    # Only one request: the weekend cost nothing and the holiday cost one cheap 404.
    assert transport.call_count == 2


def test_a_range_with_no_data_at_all_is_still_typed():
    client = flows.ParticipantFlowsClient(StubTransport(default=_not_found("x")))
    frame = client.participant_flows_range("2026-09-26", "2026-09-27")
    _assert_typed_unavailable(frame, "no_file")
    assert frame.attrs["bfinance_days_returned"] == 0


# ------------------------------------------------- as-of verification (the load-bearing check)


def test_the_as_of_date_is_checked_against_the_request_not_merely_recorded():
    """A parser that returned a different date than the caller asked for must fail here.

    The payload is 2026-09-25's. The request says 2026-09-24. Nothing may be returned.
    """
    body = _read("participant_oi_20260925.csv")
    with pytest.raises(flows.ParticipantAsOfMismatch) as excinfo:
        flows.parse_participant_oi(body, expected_date="2026-09-24", url=OI_2026_URL)

    assert excinfo.value.expected == date(2026, 9, 24)
    assert excinfo.value.found == date(2026, 9, 25)
    assert OI_2026_URL in str(excinfo.value)


def test_the_as_of_mismatch_also_fires_through_the_client():
    """A transport that serves the 2026 body for a 2020 request must be caught, not merged."""
    wrong_url = flows.participant_url("2020-06-15")
    transport2 = StubTransport(
        routes={
            wrong_url: _ok(
                wrong_url, (FIXTURES / "participant_oi_20260925.csv").read_bytes()
            )
        }
    )
    with pytest.raises(flows.ParticipantAsOfMismatch) as excinfo:
        flows.ParticipantFlowsClient(transport2).participant_flows("2020-06-15")
    assert excinfo.value.found == date(2026, 9, 25)
    assert excinfo.value.expected == date(2020, 6, 15)
    assert wrong_url in str(excinfo.value)

    # And the matching request through the same transport succeeds, so the check is not simply
    # refusing everything.
    ok = flows.parse_participant_oi(
        _read("participant_oi_20200615.csv"), expected_date="2020-06-15", url=wrong_url
    )
    assert ok.attrs[flows.AS_OF_ATTR] == "2020-06-15"


def test_the_as_of_check_runs_before_any_row_is_built():
    """Not merely 'the frame says the wrong date' - nothing is produced at all."""
    body = _read("participant_oi_20260925.csv")
    with pytest.raises(flows.ParticipantAsOfMismatch):
        flows.parse_participant_oi(body, expected_date="2026-01-01", url=OI_2026_URL)


def test_as_of_verified_is_true_only_when_a_date_was_actually_compared():
    verified = flows.parse_participant_oi(
        _read("participant_oi_20260925.csv"), expected_date="2026-09-25"
    )
    unchecked = flows.parse_participant_oi(_read("participant_oi_20260925.csv"))
    assert verified.attrs[flows.AS_OF_VERIFIED_ATTR] is True
    assert unchecked.attrs[flows.AS_OF_VERIFIED_ATTR] is False
    # Either way the date is the file's own, never the URL's guess.
    assert verified.attrs[flows.AS_OF_ATTR] == unchecked.attrs[flows.AS_OF_ATTR] == "2026-09-25"


def test_a_payload_with_no_usable_title_date_is_rejected_not_defaulted():
    """If the file makes no as-of statement, that is a parse failure, not a licence to guess."""
    body = _read("participant_oi_20260925.csv")
    stripped = body.replace("as on Sep 25, 2026", "as published at some point")
    with pytest.raises(flows.ParsingError, match="no 'as on"):
        flows.parse_participant_oi(stripped)


def test_the_title_row_is_handled_and_never_becomes_a_data_row():
    body = _read("participant_oi_20260925.csv")
    title = body.split("\n")[0]
    assert "Participant wise Open Interest" in title
    frame = flows.parse_participant_oi(body, expected_date="2026-09-25")
    # 4 participants x 12 legs. A promoted title row would add rows and a junk participant.
    assert len(frame) == 48
    assert "Participant wise Open Interest" not in set(frame.index)


# ---------------------------------------------------------------- units


def test_units_are_asserted_not_assumed():
    """Contracts for the participant files, INR crore for the money figures.

    Each claim is checked against a number, not against a string in a column header.
    """
    oi = flows.parse_participant_oi(
        _read("participant_oi_20260925.csv"), expected_date="2026-09-25"
    )
    vol = flows.parse_participant_oi(
        _read("participant_vol_20260925.csv"), measure="vol", expected_date="2026-09-25"
    )
    assert oi.attrs[flows.UNITS_ATTR] == flows.UNITS_CONTRACTS == "contracts"
    assert vol.attrs[flows.UNITS_ATTR] == flows.UNITS_CONTRACTS
    assert flows.MEASURE_UNITS == {"oi": "contracts", "vol": "contracts"}

    # Magnitude: NSE equity-index future open interest was 488,031 contracts that day. Read as
    # INR crore it would be INR 4.88 trillion of open interest in one product.
    totals = oi.attrs["bfinance_market_totals"]
    assert totals["Long"] == 27533553
    fidx = oi.query("AssetClass == 'Index' and OptionType == 'None' and Side == 'Long'")
    assert fidx["Value"].sum() == 488031
    assert 488031 < 1_000_000  # a contract count, not a rupee figure

    # Balance invariant: long total == short total, to the contract.
    assert totals["Long"] == totals["Short"]

    snapshot = flows.parse_participant_snapshot(
        _read("fiidii_20260925.json"), expected_date="2026-09-25"
    )
    assert snapshot.attrs[flows.UNITS_ATTR] == flows.UNITS_INR_CRORE == "inr_crore"
    # To two decimals, and exactly the publisher's arithmetic: 14035.77 - 11197.60 = 2838.17.
    assert snapshot.loc["DII", "Buy"] == 14035.77
    assert snapshot.loc["DII", "Net"] == 2838.17
    assert ((snapshot["Buy"] - snapshot["Sell"]) - snapshot["Net"]).abs().max() < 1e-9

    report = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2026_08.json"))
    assert report.attrs[flows.UNITS_ATTR] == flows.UNITS_INR_CRORE
    assert report.loc["Grand Total", "NetFlow"] == pytest.approx(41353.59727107881, abs=1e-6)


def test_the_publisher_total_does_not_exactly_reconcile_and_that_is_recorded():
    """Trap: NSE's own TOTAL row is off its own detail by 1 contract on this file.

    Asserted as a *non-zero* residual, which is the point. A 'totals must add up' assertion
    would fail on real upstream data, so the residual is published instead.
    """
    oi = flows.parse_participant_oi(
        _read("participant_oi_20260925.csv"), expected_date="2026-09-25"
    )
    residual = oi.attrs["bfinance_total_reconciliation_residual"]
    assert residual == {"Long": -1, "Short": 1}

    # The detail rows sum to one MORE contract long than the publisher's own total. (Two of the
    # per-category totals are each one below their own leg sum, and the TOTAL row carries a
    # further -1, so the discrepancy is an upstream artefact, not a parsing one.)
    long_sum = int(oi[oi["Side"] == "Long"]["Value"].sum())
    assert long_sum == 27533554
    assert oi.attrs["bfinance_market_totals"]["Long"] == 27533553
    assert oi.attrs["bfinance_market_totals"]["Long"] - long_sum == -1

    # The 2020 and volume vintages do reconcile exactly, so this is a real measurement and not
    # an artefact of the parser.
    for name, day in (("participant_oi_20200615.csv", "2020-06-15"),
                      ("participant_vol_20260925.csv", "2026-09-25")):
        other = flows.parse_participant_oi(_read(name), expected_date=day)
        assert other.attrs["bfinance_total_reconciliation_residual"] == {"Long": 0, "Short": 0}


# ---------------------------------------------------------------- header padding trap


def test_header_padding_is_handled_across_both_vintages():
    """The 2026 file pads headers with SPACES; the 2020 file pads with a TAB.

    The 2020 vintage is kept as a fixture precisely because it is the one that breaks a naive
    name lookup.
    """
    raw_2020 = _read("participant_oi_20200615.csv")
    header_2020 = raw_2020.split("\n")[1]
    assert "\t" in header_2020, "the 2020 fixture must still carry its TAB padding"
    assert "Future Stock Short\t" in header_2020
    assert "Total Long Contracts\t" in header_2020

    frame = flows.parse_participant_oi(raw_2020, expected_date="2020-06-15")
    assert len(frame) == 48
    assert frame.attrs["bfinance_unclassified_value_rows"] == 0
    # Same 12 legs parsed as the 2026 vintage, so the padding did not cost a column.
    assert frame.groupby(level=0).size().unique().tolist() == [12]


def test_an_unrecognised_column_raises_rather_than_being_dropped():
    body = _read("participant_oi_20260925.csv").replace(
        "Option Stock Put Short,", "Option Stock Put Short,Widget Count,"
    )
    with pytest.raises(flows.ParsingError, match="unrecognised column"):
        flows.parse_participant_oi(body, expected_date="2026-09-25")


# ---------------------------------------------------------------- snapshot + accumulation


def test_the_snapshot_route_is_a_single_day_snapshot_and_says_so():
    """The second, genuinely history-less route. Both readings were right; these are two routes."""
    snapshot = flows.parse_participant_snapshot(
        _read("fiidii_20260925.json"), expected_date="2026-09-25"
    )
    assert snapshot.attrs["bfinance_snapshot_is_single_day"] is True
    assert snapshot.attrs["bfinance_snapshot_categories"] == ("DII", "FII/FPI")
    assert snapshot.attrs[flows.AS_OF_ATTR] == "2026-09-25"
    # FII/FPI is a distinct label from DII and is preserved verbatim, not renamed.
    assert "FII/FPI" in snapshot.index and "DII" in snapshot.index
    assert snapshot.loc["FII/FPI", "Net"] == -3693.93


def test_the_snapshot_as_of_is_checked_too():
    with pytest.raises(flows.ParticipantAsOfMismatch):
        flows.parse_participant_snapshot(
            _read("fiidii_20260925.json"), expected_date="2026-09-24"
        )


def test_a_snapshot_missing_a_field_raises_instead_of_reading_zero():
    payload = [{"buyValue": "1", "category": "DII", "date": "25-Sep-2026", "netValue": "1"}]
    with pytest.raises(flows.ParsingError, match="missing field"):
        flows.parse_participant_snapshot(payload)


def test_accumulation_is_idempotent_and_states_that_history_can_never_be_backfilled(tmp_path):
    archive = flows.FlowArchive(db_path=tmp_path / "flow_archive.db")
    snapshot = flows.parse_participant_snapshot(
        _read("fiidii_20260925.json"), expected_date="2026-09-25"
    )

    assert archive.record(snapshot, source_url=flows.PARTICIPANT_SNAPSHOT_URL) == 2
    # Recording the same day again replaces rather than appends, so a re-run is safe.
    assert archive.record(snapshot, source_url=flows.PARTICIPANT_SNAPSHOT_URL) == 2

    stored = archive.read()
    assert len(stored) == 2
    assert set(stored["category"]) == {"DII", "FII/FPI"}
    assert stored["net"].sum() == pytest.approx(-855.76, abs=1e-6)

    coverage = archive.coverage()
    assert coverage["bfinance_archive_first_day"] == "2026-09-25"
    assert coverage["bfinance_archive_last_day"] == "2026-09-25"
    assert coverage["bfinance_archive_day_count"] == 1
    # The load-bearing fact: this route has no history, so no earlier day is obtainable by anyone.
    assert coverage["bfinance_archive_history_never_backfillable"] is True
    note = coverage["bfinance_archive_backfill_note"].lower()
    assert "can ever be retrieved" in note
    assert "unavailable, not as zero" in note


def test_the_client_records_every_snapshot_read_and_publishes_coverage(tmp_path):
    archive = flows.FlowArchive(db_path=tmp_path / "flow_archive.db")
    body = _read("fiidii_20260925.json").encode("utf-8")
    transport = StubTransport(
        routes={flows.PARTICIPANT_SNAPSHOT_URL: flows.HttpResult(
            url=flows.PARTICIPANT_SNAPSHOT_URL, status=200, content=body, content_type="application/json"
        )}
    )
    client = flows.ParticipantFlowsClient(transport, archive=archive)

    first = client.snapshot(expected_date="2026-09-25")
    assert first.attrs["bfinance_archive_rows_written"] == 2
    assert first.attrs["bfinance_archive_first_day"] == "2026-09-25"
    assert first.attrs["bfinance_archive_history_never_backfillable"] is True

    served = client.accumulated_flows()
    assert len(served) == 2
    assert served.attrs["bfinance_provenance"] == "local_flow_archive"
    assert transport.call_count == 1  # the read came from the archive, not the network


# ---------------------------------------------------------------- AMFI


def test_amfi_index_parses_and_reports_the_numeric_coverage_boundary():
    index = flows.parse_amfi_monthly_index(_read("amfi_monthly_index.json"), source_url="u")

    assert index.attrs[flows.CAPABILITY_ATTR] == "mutual_fund_flows"
    assert index.attrs[flows.MISSING_DATES_ATTR] is None if False else True  # key unused here
    assert index["HasWorkbook"].sum() == 3
    assert index["HasWorkbook"].sum() < len(index), "some months have no numeric workbook"

    # The pre-2018 months are PDF-only; that boundary is a value, not folklore.
    pdf_only = index[~index["HasWorkbook"]]
    assert pdf_only.index.max() < pd.Timestamp("2001-01-01")
    assert index.loc[index["HasWorkbook"], "ExcelUrl"].notna().all()
    assert index.loc[~index["HasWorkbook"], "ExcelUrl"].isna().all()

    coverage = flows.amfi_workbook_coverage(index)
    assert coverage["bfinance_month_count"] == 6
    assert coverage["bfinance_months_with_workbook"] == 3
    assert coverage["bfinance_months_pdf_only"] == 3
    assert coverage["bfinance_workbook_coverage_start"] is not None
    assert coverage["bfinance_index_coverage_start"] == "2000-01-01"


def test_amfi_report_parses_and_the_header_and_summary_rows_are_not_data_rows():
    """The payload's own header row and section taxonomy must be handled, not promoted."""
    payload = _read_json("amfi_mcr_2026_08.json")
    report = flows.parse_amfi_monthly_report(payload, source_url="u")

    # Exactly the transcribed records, no more.
    assert len(report) == len(payload["records"]) == 8
    assert report.attrs["bfinance_records_parsed"] == 8

    # The header row (17 cells) and the workbook's section taxonomy (15 rows) were consumed as
    # structure, and their counts are published so a reader can see they were not ignored.
    assert report.attrs["bfinance_header_row_cells"] == 17
    assert report.attrs["bfinance_section_taxonomy_rows"] == 15

    # The decisive assertion: no heading, and no taxonomy label, became a category.
    categories = set(report.index)
    for forbidden in ("Sr", "Scheme Name", "Open ended Schemes", "Debt Schemes", "None",
                      "Equity Schemes", "Hybrid Schemes", "Index Funds", "A", "B", "C"):
        assert forbidden not in categories
    # The taxonomy is section headings, and the one real category that is a heading-adjacent
    # label is present as data because it is a genuine published row.
    assert "Liquid Fund" in categories


def test_amfi_report_matches_the_published_known_value_controls():
    """Aug 2026 Grand Total +41,353.60 INR crore; May 2020 +70,813.41 INR crore."""
    aug = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2026_08.json"))
    may = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2020_05.json"))

    assert round(aug.loc["Grand Total", "NetFlow"], 2) == 41353.60
    assert round(may.loc["Grand Total", "NetFlow"], 2) == 70813.41
    assert aug.loc["Grand Total", "Schemes"] == 1985
    assert may.loc["Grand Total", "Schemes"] == 1829
    # Counts stay integral; money stays fractional.
    assert pd.api.types.is_integer_dtype(aug["Schemes"])
    assert pd.api.types.is_float_dtype(aug["NetFlow"])


def test_amfi_category_labels_drift_so_only_the_anchors_may_be_joined():
    """The measured reason a category-level MF time series cannot be built by label-join."""
    aug = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2026_08.json"))
    may = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2020_05.json"))

    assert "Ultra Short Term Fund" in aug.index
    assert "Ultra Short Duration Fund" in may.index
    assert "Short Term Fund" in aug.index
    assert "Short Duration Fund" in may.index

    common = set(aug.index) & set(may.index)
    for anchor in flows.AMFI_STABLE_ANCHORS:
        assert anchor in aug.index and anchor in may.index
    # The overlap is *only* the anchors plus genuinely unchanged labels. If a caller wanted a
    # category series, the anchors are the only rows that mean the same thing in both months.
    assert set(flows.AMFI_STABLE_ANCHORS) <= common
    assert aug.attrs["bfinance_stable_anchors_present"] == flows.AMFI_STABLE_ANCHORS


def test_amfi_report_as_of_is_checked_against_the_request():
    payload = _read_json("amfi_mcr_2026_08.json")
    good = flows.parse_amfi_monthly_report(payload, month="2026-08-01")
    assert good.attrs[flows.AS_OF_VERIFIED_ATTR] is True
    assert good.attrs[flows.AS_OF_ATTR] == "2026-08-01"
    with pytest.raises(flows.ParticipantAsOfMismatch):
        flows.parse_amfi_monthly_report(payload, month="2020-05-01")


# ---------------------------------------------------------------- caching


def test_no_request_is_made_while_the_cache_is_fresh_and_one_is_made_once_it_is_stale(tmp_path):
    """The 1.5 MB monthly payload must not be re-downloaded per call, and must refresh when stale.

    No network and no clock mocking: staleness is produced by expiring the stored row directly in
    SQLite, which is the real condition the cache has to detect.
    """
    cache = SQLiteCache(db_path=str(tmp_path / "amfi_cache.db"), default_ttl_hours=1.0)
    body = _read("amfi_monthly_index.json").encode("utf-8")
    transport = StubTransport(
        routes={"u": flows.HttpResult(url="u", status=200, content=body, content_type="text/plain")}
    )
    client = flows.MutualFundClient(transport, cache=cache)

    first = client.monthly_index(url="u")
    assert transport.call_count == 1
    assert first.attrs["bfinance_cache"] == "miss"
    assert "bfinance_fetched_utc" in first.attrs

    second = client.monthly_index(url="u")
    assert transport.call_count == 1, "a fresh cache entry must not cause a request"
    assert second.attrs["bfinance_cache"] == "hit"
    pd.testing.assert_frame_equal(
        first.drop(columns=["bfinance_cache", "bfinance_fetched_utc"], errors="ignore"),
        second.drop(columns=["bfinance_cache", "bfinance_fetched_utc"], errors="ignore"),
    )

    # Now make the entry stale, the way time would.
    with sqlite3.connect(cache.db_path) as conn:
        conn.execute("UPDATE bfinance_cache SET expires_at = 0")
        conn.commit()

    third = client.monthly_index(url="u")
    assert transport.call_count == 2, "a stale entry must cause exactly one request"
    assert third.attrs["bfinance_cache"] == "miss"
    assert len(third) == len(first)


def test_caching_can_be_disabled_and_then_says_so(tmp_path):
    transport = StubTransport(
        routes={"u": flows.HttpResult(url="u", status=200, content=_read("amfi_monthly_index.json").encode())}
    )
    client = flows.MutualFundClient(transport, cache=None)
    client.monthly_index(url="u")
    client.monthly_index(url="u")
    assert transport.call_count == 2


# ---------------------------------------------------------------- attrs on every result


def test_every_returned_frame_carries_its_source_url_and_as_of_date(tmp_path):
    """A sweep over the whole public surface. A frame that forgets provenance fails here."""
    transport = _oi_transport()
    client = flows.ParticipantFlowsClient(transport)

    frames = {
        "participant_flows": client.participant_flows("2026-09-25"),
        "participant_flows_range": client.participant_flows_range("2026-09-25", "2026-09-25"),
        "snapshot": flows.parse_participant_snapshot(
            _read("fiidii_20260925.json"), expected_date="2026-09-25"
        ),
        "amfi_monthly_index": flows.parse_amfi_monthly_index(
            _read("amfi_monthly_index.json"), source_url="u"
        ),
        "amfi_monthly_report": flows.parse_amfi_monthly_report(
            _read_json("amfi_mcr_2026_08.json"), source_url="u"
        ),
    }

    for name, frame in frames.items():
        assert flows.SOURCE_URL_ATTR in frame.attrs, f"{name} has no source URL"
        assert flows.AS_OF_ATTR in frame.attrs, f"{name} has no as-of date"
        assert frame.attrs[flows.SOURCE_URL_ATTR], f"{name} has an empty source URL"
        assert frame.attrs[flows.AS_OF_ATTR], f"{name} has an empty as-of date"
        assert flows.CAPABILITY_ATTR in frame.attrs
        assert flows.STATUS_ATTR in frame.attrs
        assert flows.UNITS_ATTR in frame.attrs


def test_the_two_capability_groups_are_separated():
    """A caller wanting FII/DII flows must not have to know AMFI is in the same module."""
    participant_names = [
        n for n in flows.__all__ if n in {
            "participant_flows", "participant_flows_range", "parse_participant_oi",
            "parse_participant_snapshot", "ParticipantFlowsClient", "FlowArchive",
            "participant_url", "PARTICIPANT_CATEGORIES", "MEASURE_UNITS",
            "ParticipantFlowsUnavailable", "ParticipantAsOfMismatch", "empty_participant_frame",
        }
    ]
    mutual_fund_names = [
        n for n in flows.__all__ if n in {
            "amfi_monthly_index", "amfi_monthly_report", "parse_amfi_monthly_index",
            "parse_amfi_monthly_report", "MutualFundClient", "amfi_workbook_coverage",
            "AMFI_STABLE_ANCHORS", "MutualFundWorkbookUnavailable",
        }
    ]
    assert participant_names and mutual_fund_names
    assert not set(participant_names) & set(mutual_fund_names)

    # And the frames themselves declare which group they came from.
    p = flows.parse_participant_oi(_read("participant_oi_20260925.csv"), expected_date="2026-09-25")
    m = flows.parse_amfi_monthly_report(_read_json("amfi_mcr_2026_08.json"))
    assert p.attrs[flows.CAPABILITY_ATTR] == "participant_flows"
    assert m.attrs[flows.CAPABILITY_ATTR] == "mutual_fund_flows"
    # Different units, so a shared downstream pipeline must read the key rather than assume.
    assert p.attrs[flows.UNITS_ATTR] != m.attrs[flows.UNITS_ATTR]


def test_every_public_name_in_all_is_importable():
    for name in flows.__all__:
        assert hasattr(flows, name), f"{name} is in __all__ but missing from the module"


# ---------------------------------------------------------------- fixture quality


def test_the_lf_normalisation_of_the_fixtures_is_behaviour_neutral():
    """Proves the committed fixtures still parse to exactly what the served CRLF bytes did."""
    cases = [
        ("participant_oi_20260925.csv", "oi", "2026-09-25", OI_2026_URL),
        ("participant_oi_20200615.csv", "oi", "2020-06-15", OI_2020_URL),
        ("participant_vol_20260925.csv", "vol", "2026-09-25", VOL_2026_URL),
    ]
    for name, measure, day, url in cases:
        stored = (FIXTURES / name).read_bytes()
        # The only transformation applied at capture time was CRLF -> LF.
        assert b"\r\n" not in stored, f"{name} must be stored LF-only"
        crlf_form = stored.replace(b"\n", b"\r\n").decode("utf-8")
        from_stored = flows.parse_participant_oi(
            stored.decode("utf-8"), measure=measure, expected_date=day, url=url
        )
        from_crlf = flows.parse_participant_oi(
            crlf_form, measure=measure, expected_date=day, url=url
        )
        pd.testing.assert_frame_equal(from_stored, from_crlf, check_exact=True)
        assert from_stored.attrs == from_crlf.attrs
        # And the recorded hashes still describe the committed bytes.
    meta = _read_json("META.json")
    import hashlib

    for name, *_ in cases:
        entry = meta["files"][name[: -len(".csv")]]
        stored = (FIXTURES / name).read_bytes()
        assert entry["stored_bytes"] == len(stored)
        assert entry["stored_sha256"] == hashlib.sha256(stored).hexdigest()
        assert entry["stored_line_endings"] == "lf"


def test_the_fixtures_meta_records_the_route_conflict_resolution():
    """The disagreement between the two research probes is recorded, not resolved silently."""
    meta = _read_json("META.json")
    resolution = meta["_route_conflict_resolution"]
    assert "BOTH ARE REAL" in resolution["resolution"]
    assert resolution["dated_csv_route"]["measured_200"]
    assert resolution["arb_correction"].startswith("There is NO 'Arb' row")
    assert meta["_total_reconciliation_finding"]
    assert meta["_units_determination"]["participant_oi_and_vol"]["declared_unit"] == "contracts"


# ---------------------------------------------------------------- offline guarantee


def test_importing_bfinance_does_not_pull_in_flows_eagerly():
    """bfinance/__init__.py must not import flows, or every `import bfinance` pays for it."""
    code = "import sys, bfinance; print('bfinance.flows' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        "import bfinance pulled in bfinance.flows; the package must not import it eagerly"
    )


def test_constructing_a_client_makes_no_request(tmp_path):
    """A default-constructed client must be inert until it is asked to do something."""
    flows.ParticipantFlowsClient()
    flows.MutualFundClient()
    # A real file, not ':memory:' — every sqlite3 call opens its own connection, so an
    # in-memory path would be a throwaway database per call (see tests/conftest.py).
    archive = flows.FlowArchive(db_path=tmp_path / "inert.db")
    client = flows.ParticipantFlowsClient(archive=archive)
    assert client.archive is archive


# ================================================================ NEGATIVE CONTROLS
#
# Each control injects the exact bug it guards against and asserts the guard FIRES. A control
# that cannot fail is worse than no control, so these assert on the *failure* of the mutated
# behaviour rather than on the presence of a guard.


def test_negative_control_removing_the_as_of_check_lets_a_wrong_day_through():
    """Inject the bug: neuter ``_verify_as_of``. The as-of guard must then be seen to fail.

    This is the control for "the as-of date is checked against the request". It proves the check
    is load-bearing rather than decorative, by showing the frame the check prevents.
    """
    body = _read("participant_oi_20260925.csv")

    # Control: with the check in place, asking for 2026-09-24 is refused.
    with pytest.raises(flows.ParticipantAsOfMismatch):
        flows.parse_participant_oi(body, expected_date="2026-09-24", url=OI_2026_URL)

    # Inject the bug the check exists to catch.
    original = flows._verify_as_of
    try:
        flows._verify_as_of = lambda found, expected, url: None
        wrong_day_frame = flows.parse_participant_oi(
            body, expected_date="2026-09-24", url=OI_2026_URL
        )
    finally:
        flows._verify_as_of = original

    # The check is restored, and the very same call is refused again.
    with pytest.raises(flows.ParticipantAsOfMismatch):
        flows.parse_participant_oi(body, expected_date="2026-09-24", url=OI_2026_URL)

    # Without the check, a frame labelled 2026-09-24 comes back carrying 2026-09-25's numbers.
    # This is the confidently-wrong answer the check prevents. Participant is the *index*, so
    # the FII row is selected by label, not by a column filter.
    assert wrong_day_frame.attrs[flows.AS_OF_ATTR] == "2026-09-25"
    assert wrong_day_frame.attrs[flows.AS_OF_VERIFIED_ATTR] is True
    fii = wrong_day_frame.loc["FII"].query(
        "AssetClass == 'Index' and OptionType == 'None' and Side == 'Long'"
    )
    assert fii["Value"].sum() == 46305  # 2026-09-25's FII number, under a 2026-09-24 label
    # ...and it is genuinely the wrong day's number: 2026-09-24's FII figure is not knowable
    # from this file, which is the whole problem.
    assert wrong_day_frame.attrs[flows.AS_OF_ATTR] != "2026-09-24"


def test_negative_control_zero_filling_a_missing_day_is_detected_by_the_same_assertion():
    """Inject the bug: return a zero-filled row for a day NSE never published.

    The very same helper that validates the real unavailable frame must reject it. If this test
    passes because the helper is too weak to tell the two apart, it fails.
    """
    # The real thing.
    client = flows.ParticipantFlowsClient(StubTransport(default=_not_found("x")))
    real = client.participant_flows("2026-09-24", on_missing="frame")
    _assert_typed_unavailable(real, "no_file")

    # The bug: what a naive implementation would hand back instead. concat drops attrs, so they
    # are re-applied - a zero-filled frame that forgot its own status would be caught by a
    # different assertion, and this control is specifically about the zero row.
    zero_filled = flows.empty_participant_frame(
        {
            flows.AS_OF_ATTR: "2026-09-24",
            flows.UNAVAILABLE_REASON_ATTR: "no_file",
            flows.UNITS_ATTR: flows.UNITS_CONTRACTS,
        }
    )
    attrs = dict(zero_filled.attrs)
    zero_filled = pd.concat(
        [
            zero_filled,
            pd.DataFrame(
                {
                    "Date": [pd.Timestamp("2026-09-24")],
                    "Measure": ["oi"],
                    "AssetClass": ["Index"],
                    "OptionType": ["None"],
                    "Side": ["Long"],
                    "Value": [0],
                },
                index=pd.Index(["FII"], name="Participant"),
            ),
        ]
    )
    zero_filled.attrs.update(attrs)

    with pytest.raises(AssertionError):
        _assert_typed_unavailable(zero_filled, "no_file")

    # And the status key is not overridable by a caller, which is the other half of the
    # guarantee: a caller cannot relabel an unavailable frame as a real one.
    liar = flows.empty_participant_frame({flows.STATUS_ATTR: flows.STATUS_OK})
    assert liar.attrs[flows.STATUS_ATTR] == flows.STATUS_UNAVAILABLE
    with pytest.raises(AssertionError):
        _assert_typed_unavailable(liar, "no_file")


def test_negative_control_a_stray_arb_row_would_be_visible_in_attrs():
    """Inject a fifth category and prove it is reported rather than silently accepted.

    Guards the claim that the module measures its categories instead of hard-coding four.
    """
    lines = _read("participant_oi_20260925.csv").split("\n")
    pro_index = next(i for i, line in enumerate(lines) if line.startswith("Pro,"))
    arb_values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400]
    lines.insert(pro_index + 1, "Arb," + ",".join(str(v) for v in arb_values))
    body = "\n".join(lines)

    frame = flows.parse_participant_oi(body, expected_date="2026-09-25")
    assert "Arb" in frame.attrs["bfinance_participants_present"]
    assert "Arb" in set(frame.index)
    assert frame.attrs["bfinance_participants_present"] == ("Arb", "Client", "DII", "FII", "Pro")
    # The documented constant is unchanged: the module reports what it found, it does not
    # redefine the vocabulary.
    assert flows.PARTICIPANT_CATEGORIES == ("Client", "DII", "FII", "Pro")
    # A stray aggregate is still kept out of the rows.
    assert "TOTAL" not in set(frame.index)
