"""
``bfinance.ownership`` — NSE shareholding patterns, bulk/short/block deals, and two refusals.

Fully offline. Three verbatim response bodies under ``tests/fixtures/nse/ownership/`` are the
only inputs, the client is exercised through ``httpx.MockTransport``, and every parser is pure.
No test in this file may make a network request; :class:`TestNoNetworkAtImportOrParse` enforces
the stronger version of that in a subprocess, by making ``socket.connect`` fatal.

The three payloads, and why each is here:

``shareholding_RELIANCE.json`` (40,154 B)
    A **bare JSON array**, 22 rows, 33 keys. It answers "what does a filing look like", which is
    the only reason it is stored whole.
``deals_snapshot_20260925.json`` (54,901 B)
    All three deal kinds in one request, **string** counters, and two measured defects: row dates
    that do not agree with ``as_on_date``, and a ``SHORT_DEALS_DATA`` array that is half null.
    Trimming it would have destroyed the evidence for both.
``pledge_yesbank_EMPTY.json`` (28 B)
    A **deliberate negative fixture**: a live route answering 200 with nothing. It is here to
    prove that "reachable" and "has data" are different facts, and to keep this package from
    ever shipping a pledge parser written against a payload that has never existed.

Every assertion below is checked against the measured fixture, and where a test would be vacuous
if the fixture stopped exhibiting the defect, a companion control assertion says so out loud
(see :class:`TestNegativeControls`) — a green test that cannot go red is worse than no test.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pandas as pd
import pytest

import bfinance.ownership as own
from bfinance.utils.exceptions import NotAnNseSymbolError, ParsingError, UpstreamServiceError

FIXTURES = Path(__file__).parent / "fixtures" / "nse" / "ownership"

SHAREHOLDING_BYTES = (FIXTURES / "shareholding_RELIANCE.json").read_bytes()
DEALS_BYTES = (FIXTURES / "deals_snapshot_20260925.json").read_bytes()
PLEDGE_BYTES = (FIXTURES / "pledge_yesbank_EMPTY.json").read_bytes()
META = json.loads((FIXTURES / "META.json").read_text(encoding="utf-8"))

SHAREHOLDING_URL = own.shareholding_url("RELIANCE")
DEALS_URL = own.deals_url()
PLEDGE_URL = own.pledge_url("YESBANK", "2018-01-01", "2026-09-27")

#: The snapshot's own ``as_on_date`` and the other day that appears in the same payload.
AS_ON = "2026-09-25"
OFF_AS_ON = "2026-09-24"

SOURCE = Path(own.__file__).read_text(encoding="utf-8")


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------- offline transport


class _StubHttpx:
    """Stands in for the ``httpx`` module inside :mod:`bfinance.ownership`.

    Every attribute is the real one **except** ``AsyncClient`` construction, which is redirected
    at a :class:`httpx.MockTransport`. The module builds its own client per call, so this is the
    only way to drive the public methods offline — and because ``__getattr__`` proxies everything
    else, ``httpx.TimeoutException`` and ``httpx.TransportError`` keep working in the retry
    paths this never reaches.

    :attr:`requests` records every URL actually requested, which is what turns "no request when
    cached" into an assertion rather than an inference from a fast test.
    """

    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    def __getattr__(self, name):
        return getattr(httpx, name)

    def AsyncClient(self, **kwargs):  # noqa: N802 - mirrors httpx's own casing
        kwargs.pop("transport", None)
        handle = self._handler
        record = self.requests.append

        def dispatch(request):
            record(str(request.url))
            return handle(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(dispatch), **kwargs)


def respond_with(payload, status: int = 200):
    """A handler that always answers ``payload``, and counts what it was asked for."""
    seen: list = []

    def handler(request):
        seen.append(str(request.url))
        if status == 200:
            return httpx.Response(200, json=payload)
        return httpx.Response(status, text="blocked")

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def exploding_handler(request):  # noqa: ARG001
    raise AssertionError(
        f"a request was issued to {request.url} but the test expected the cache to answer"
    )


@pytest.fixture
def shareholding_payload():
    return load("shareholding_RELIANCE.json")


@pytest.fixture
def deals_payload():
    return load("deals_snapshot_20260925.json")


@pytest.fixture
def pledge_payload():
    return load("pledge_yesbank_EMPTY.json")


@pytest.fixture
def shareholding(shareholding_payload):
    return own.parse_shareholding(
        shareholding_payload, symbol="RELIANCE", url=SHAREHOLDING_URL
    )


@pytest.fixture
def deals(deals_payload):
    return own.parse_deals(deals_payload, url=DEALS_URL)


@pytest.fixture
def client(temp_cache):
    """An :class:`OwnershipClient` on a real on-disk cache, with pacing turned off.

    Pacing is off because the floor exists to be polite to a live edge, and offline it would
    only add seconds to every call.
    """
    return own.OwnershipClient(
        cache=temp_cache, min_request_interval_ms=0, max_retries=2
    )


def offline(module_httpx, handler, monkeypatch):
    monkeypatch.setattr(own, "httpx", module_httpx)
    return module_httpx


# ================================================================ fixture integrity


class TestFixtureIntegrity:
    """The fixtures are the module's evidence, so their recorded facts are checked, not trusted.

    A META.json that says 40,154 bytes while the file is 40,130 would leave every schema claim
    in the module resting on a payload nobody can reproduce. This is the test that makes the
    manifest falsifiable.
    """

    @pytest.mark.parametrize("name", sorted(META["captures"]))
    def test_stored_bytes_and_sha256_match_the_manifest(self, name):
        record = META["captures"][name]
        raw = (FIXTURES / name).read_bytes()

        assert len(raw) == record["original_bytes"] == record["stored_bytes"], (
            f"{name} is {len(raw)} bytes; the manifest says {record['stored_bytes']}"
        )
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == record["stored_sha256"] == record["original_sha256"], (
            f"{name} does not hash to its recorded sha256"
        )
        assert digest[:16] == record["original_sha256_16"]
        assert record["http_status"] == 200

    @pytest.mark.parametrize("name", sorted(META["captures"]))
    def test_line_endings_are_lf(self, name):
        assert b"\r" not in (FIXTURES / name).read_bytes(), f"{name} contains CR"

    def test_every_capture_was_stored_untrimmed(self):
        """No capture is a subset of another capture's story.

        The whole 40 KB and whole 55 KB bodies are kept precisely because trimming is what would
        have hidden the two deals defects. A "no trim" claim in the manifest that is not true
        would make the module's row counts unreproducible.
        """
        for name, record in META["captures"].items():
            assert record["stored_bytes"] == record["original_bytes"], (
                f"{name} claims 'NO TRIM' but stores {record['stored_bytes']} of "
                f"{record['original_bytes']} bytes"
            )
            assert record["trim_rule"].startswith("NO TRIM")

    def test_one_manifest_per_directory_and_no_stale_siblings(self):
        """``META.json`` is the only manifest, and every payload is accounted for by it.

        An earlier state of this directory carried a per-file ``*.META.json`` beside each
        payload as well. Those three described the *trimmed* bodies from the first capture
        (10,627 / 1,452 / 29 bytes) while the files on disk were the whole ones
        (40,154 / 54,901 / 28), so each one asserted a sha256 that no longer matched the file it
        sat next to. Two manifests, one of them wrong, is worse than one: a reader has no way to
        tell which is the record. They were deleted; this test is what stops them coming back.
        """
        # `*.META.json` cannot match `META.json` -- the pattern needs something before the dot --
        # so the two assertions are separate and both necessary.
        assert sorted(p.name for p in FIXTURES.glob("*.META.json")) == [], (
            "a per-file manifest reappeared beside the payloads; keep META.json as the only record"
        )
        assert (FIXTURES / "META.json").is_file()

        payloads = {
            p.name for p in FIXTURES.iterdir()
            if p.suffix == ".json" and p.name != "META.json"
        }
        assert payloads == set(META["captures"]), (
            "a payload has no manifest entry, or an entry has no payload"
        )
        assert META["directory"] == "tests/fixtures/nse/ownership"

    def test_the_manifest_records_the_request_count_and_the_method(self):
        """Three requests, and the method that made them, are on the record.

        A capture whose provenance is unstated cannot be re-checked, and this one cost three
        requests to an edge that blocks irreversibly.
        """
        assert META["request_count"] == 3
        assert len(META["captures"]) == 3
        assert "prime-on-403" in META["capture_method"]
        assert "2.5 s" in META["capture_method"]
        assert META["host"] == "https://www.nseindia.com"
        for record in META["captures"].values():
            assert record["url"].startswith(META["host"])
            assert record["retrieved_utc"].endswith("+00:00")

    def test_the_pledge_fixture_is_the_deliberate_empty_one(self):
        """A negative fixture, and a *negative* one only.

        It must be 28 bytes of empty arrays, and the manifest must say so. If a future capture
        ever brings back real rows, this test fails and forces a decision about what the module
        claims — which is the point of storing the empty one under its own name.
        """
        assert PLEDGE_BYTES == b'{"comNameList":[],"data":[]}'
        assert load("pledge_yesbank_EMPTY.json") == {"comNameList": [], "data": []}
        measured = META["captures"]["pledge_yesbank_EMPTY.json"]["measured"]
        assert measured["rows_returned"] == 0
        assert measured["is_404"] is False
        assert "UNVERIFIED" in measured["VERDICT"]

    def test_fpi_is_recorded_as_not_requested(self):
        """The 403 body is not a payload, so it is not stored as a fixture.

        Storing an error page under this directory is how somebody later writes a parser for it.
        """
        fpi = META["not_captured"]["fpi_foreign_ownership"]
        assert fpi["requested_on_2026_09_28"] is False
        assert fpi["http_status_observed"] == 403
        assert not (FIXTURES / "fpi.json").exists()
        assert not list(FIXTURES.glob("*quote*equity*"))


# ================================================== group 1: the bare-array shareholding


class TestShareholdingShape:
    """The route answers with a **bare JSON array**, and that is the first thing to pin.

    ``resp.json()["data"]`` is the natural call and it does not work here. On a list it raises
    ``TypeError``; ``.get("data")`` raises ``AttributeError``. Neither is a "no data" answer, and
    both are the sort of thing that gets papered over with a ``try``. The shape is asserted from
    the raw bytes here so the trap cannot be reintroduced by a re-serialised fixture.
    """

    def test_the_response_body_is_a_bare_array(self):
        payload = json.loads(SHAREHOLDING_BYTES)

        assert isinstance(payload, list), (
            f"the shareholding body parses to {type(payload).__name__}, not a list"
        )
        assert payload, "the fixture is empty, so it proves nothing about the shape"
        assert all(isinstance(row, dict) for row in payload)

    def test_resp_json_data_does_not_work_and_the_test_says_so_explicitly(self):
        """The exact two dict-shaped calls, each asserted to fail, with the reason recorded.

        Asserted with ``pytest.raises`` rather than a truthiness check so a future upstream
        change that turns the payload into a dict makes this test red instead of quietly making
        the parser's bare-array branch dead code.
        """
        payload = json.loads(SHAREHOLDING_BYTES)

        with pytest.raises(TypeError):
            payload["data"]
        with pytest.raises(AttributeError):
            payload.get("data")
        assert "data" not in payload

    def test_twenty_two_rows_and_thirty_three_keys_one_keyset(self, shareholding_payload):
        assert len(shareholding_payload) == 22
        keysets = {frozenset(row) for row in shareholding_payload}
        assert len(keysets) == 1, "the captured rows do not all carry the same keys"
        assert len(next(iter(keysets))) == 33

    def test_the_parser_keeps_every_row(self, shareholding):
        assert len(shareholding) == 22
        assert shareholding.attrs["bfinance_source_row_count"] == 22
        assert shareholding.attrs["bfinance_shareholding_undated_rows"] == 0
        assert shareholding.attrs["bfinance_shareholding_unparsed_dates"] == []
        assert tuple(shareholding.columns) == own.SHAREHOLDING_COLUMNS
        assert shareholding.index.name == "AsOf"
        assert shareholding.attrs["bfinance_provenance"] == own.PROVENANCE_SHAREHOLDING
        assert shareholding.attrs["bfinance_source_url"] == SHAREHOLDING_URL
        assert shareholding.attrs["bfinance_symbol"] == "RELIANCE"

    def test_no_upstream_key_is_unaccounted_for(self, shareholding):
        """Every key the payload carries is either mapped or explicitly declined.

        ``unexpected_keys`` is the loud one: a key this module has never seen. ``unmapped_keys``
        is the quiet one, and it is a list rather than an absence, so "I saw it and chose not to
        map it" is on the record instead of being indistinguishable from a bug.
        """
        attrs = shareholding.attrs
        assert attrs["bfinance_shareholding_unexpected_keys"] == []
        assert len(attrs["bfinance_shareholding_upstream_keys"]) == 33
        assert attrs["bfinance_shareholding_unmapped_keys"] == [
            "cgTimeStamp", "countString", "desc", "format", "index", "industry", "isin",
            "newColumn", "underlyingDrs",
        ]
        for key in attrs["bfinance_shareholding_unmapped_keys"]:
            assert key in own.SHAREHOLDING_KEY_MAP, f"{key} is unmapped but not declared"
            assert own.SHAREHOLDING_KEY_MAP[key] is None

    def test_a_new_upstream_key_would_be_reported_rather_than_ignored(self):
        """Prove the ``unexpected_keys`` detector is not vacuous.

        One synthetic key added to a real row must appear in ``unexpected_keys``. Without this,
        a parser that silently ignored unknown keys would pass every other test in this class.
        """
        payload = load("shareholding_RELIANCE.json")
        payload[0] = dict(payload[0], someBrandNewNseField="surprise")
        frame = own.parse_shareholding(payload, symbol="RELIANCE", url="fixture")

        assert frame.attrs["bfinance_shareholding_unexpected_keys"] == ["someBrandNewNseField"]
        assert len(frame) == 22, "an unknown key must not cost a row"

    def test_served_descending_and_parsed_ascending(self, shareholding_payload, shareholding):
        """NSE hands the array newest-first; the frame is oldest-first.

        Asserted from the raw payload so the test is about the reversal and not about the
        parser's sort being stable.
        """
        assert shareholding_payload[0]["date"] == "30-JUN-2026"
        assert shareholding_payload[-1]["date"] == "30-SEP-2021"
        assert shareholding.index.is_monotonic_increasing
        assert str(shareholding.index[0])[:10] == "2021-09-30"
        assert str(shareholding.index[-1])[:10] == "2026-06-30"

    def test_history_is_about_five_years_and_ends_at_the_last_filed_quarter(self, shareholding):
        assert shareholding.attrs["bfinance_shareholding_first_date"] == (
            own.SHAREHOLDING_FIRST_OBSERVED_DATE
        )
        assert shareholding.attrs["bfinance_shareholding_last_date"] == (
            own.SHAREHOLDING_LAST_OBSERVED_DATE
        )
        assert str(shareholding.index[-1])[:10] <= "2026-06-30"

    def test_units_are_measured_not_inferred(self, shareholding, deals):
        """The percentage reading is established by arithmetic, and it is the reason it is claimed.

        ``pr_and_prgrp + public_val == 100.00`` on all 22 rows is the whole argument. If that
        stops holding the unit is no longer established and this test should fail rather than
        leave "percent" in ``UNITS`` as an inherited assumption.
        """
        totals = (shareholding["PromoterPct"] + shareholding["PublicPct"]).round(6)
        assert (totals == 100.0).all(), f"percent-of-shares reading broken: {sorted(set(totals))}"
        assert shareholding.attrs["bfinance_units"] == own.UNITS["shareholding"]
        assert deals.attrs["bfinance_units"] == own.UNITS["deals"]

    def test_the_undeclared_unit_is_not_coerced(self, shareholding):
        """``employeeTrusts`` is ``"0"`` on every row: a count or a percent, indistinguishable.

        Coercing it to ``0.0`` would pick one. It stays text, and ``UNITS`` says UNDECLARED.
        Checked as "not a number" rather than as ``dtype == object``, because pandas 3 infers a
        string dtype and an ``object`` assertion would pass for the wrong reason.
        """
        assert (shareholding["EmployeeTrusts"] == "0").all()
        assert not pd.api.types.is_numeric_dtype(shareholding["EmployeeTrusts"])
        assert all(isinstance(value, str) for value in shareholding["EmployeeTrusts"])
        assert own.UNITS["shareholding"]["EmployeeTrusts"].startswith("UNDECLARED")

    def test_the_formatted_xbrl_size_is_not_turned_into_bytes(self, shareholding):
        """``"510.29 KB"`` stays a string because NSE never says whether KB is 1000 or 1024."""
        sizes = shareholding["XBRLFileSize"].dropna()
        assert len(sizes) == 5
        assert all(isinstance(value, str) and value.endswith(" KB") for value in sizes)
        assert "NOT parsed to bytes" in own.UNITS["shareholding"]["XBRLFileSize"]

    def test_an_undated_filing_is_kept_rather_than_dropped(self):
        """A row with no usable date must not vanish.

        It keeps a ``NaT`` index, is counted in ``attrs``, and is therefore excluded from every
        time-series join by construction — invisible to the arithmetic, visible to the reader.
        Dropping it instead would turn a schema problem into a short history.
        """
        payload = [
            {"symbol": "RELIANCE", "date": "31-MAR-2026", "pr_and_prgrp": "50.0",
             "public_val": "50.0", "revisedData": "N", "broadcastDate": "20-APR-2026 19:40:36",
             "submissionDate": "21-APR-2026"},
            {"symbol": "RELIANCE", "date": "-", "pr_and_prgrp": "50.0", "public_val": "50.0",
             "revisedData": "N"},
        ]
        frame = own.parse_shareholding(payload, symbol="RELIANCE", url="fixture")

        assert len(frame) == 2
        assert frame.attrs["bfinance_shareholding_undated_rows"] == 1
        assert frame.attrs["bfinance_shareholding_undated_values"] == ["-"]
        assert frame.index.isna().sum() == 1
        assert pd.Timestamp("2026-03-31") in frame.index

    def test_a_payload_that_is_neither_array_nor_row_list_raises(self):
        with pytest.raises(ParsingError):
            own.parse_shareholding("not json at all", url="fixture")
        with pytest.raises(ParsingError):
            own.parse_shareholding({"meta": {"rows": 3}}, url="fixture")

    def test_the_url_is_the_measured_route_and_strips_a_ns_suffix(self):
        assert own.SHAREHOLDING_API_PATH == "/api/corporate-share-holdings-master"
        assert SHAREHOLDING_URL == (
            "https://www.nseindia.com/api/corporate-share-holdings-master"
            "?index=equities&symbol=RELIANCE"
        )
        assert "symbol=RELIANCE" in own.shareholding_url("RELIANCE.NS")
        assert "symbol=" not in own.shareholding_url(None)
        with pytest.raises(NotAnNseSymbolError):
            own.shareholding_url("500112.BO")
        with pytest.raises(NotAnNseSymbolError):
            own.shareholding_url("^NIFTY50")


# ================================================ the three coexisting date spellings


class TestUpstreamDateFormats:
    """Three spellings, one normaliser, and each of them covered.

    ``date``/``submissionDate`` are ``DD-MMM-YYYY``; ``broadcastDate``/``systemDate`` are
    ``DD-MMM-YYYY HH:MM:SS``; the deals payload uses title-case ``DD-Mmm-YYYY``. They are not
    interchangeable as strings, and a parser that handles only one of them is a parser that drops
    whichever field it does not recognise.
    """

    @pytest.mark.parametrize(
        "raw,with_clock,expected",
        [
            ("30-JUN-2026", False, pd.Timestamp("2026-06-30")),
            ("16-JUL-2026", False, pd.Timestamp("2026-07-16")),
            ("16-JUL-2026 19:24:44", True, pd.Timestamp("2026-07-16 19:24:44")),
            ("07-JAN-2022 02:55:01", True, pd.Timestamp("2022-01-07 02:55:01")),
            ("25-Sep-2026", False, pd.Timestamp("2026-09-25")),
            ("24-Sep-2026", False, pd.Timestamp("2026-09-24")),
            # Case tolerance on the month token is what lets one regex serve both spellings.
            ("30-jun-2026", False, pd.Timestamp("2026-06-30")),
            ("25-SEP-2026", False, pd.Timestamp("2026-09-25")),
            # A space instead of "T" is the same instant.
            ("16-JUL-2026T19:24:44", True, pd.Timestamp("2026-07-16 19:24:44")),
        ],
    )
    def test_every_spelling_normalises(self, raw, with_clock, expected):
        assert own.normalise_upstream_date(raw, with_clock=with_clock) == expected

    def test_the_three_spellings_are_three_distinct_strings(self):
        """The normaliser is not covering one spelling three ways.

        If these compared equal, a single-format parser would already handle all three and the
        claim in the module docstring would be over-stated.
        """
        up, clocked, title = "30-JUN-2026", "30-JUN-2026 00:00:00", "30-Jun-2026"
        assert len({up, clocked, title}) == 3
        assert own.normalise_upstream_date(up) == own.normalise_upstream_date(title)
        assert own.UPSTREAM_DATE == "DD-MMM-YYYY"
        assert own.UPSTREAM_DATETIME == "DD-MMM-YYYY HH:MM:SS"
        assert own.UPSTREAM_DEAL_DATE == "DD-Mmm-YYYY"

    def test_asking_for_a_date_drops_the_clock(self, shareholding):
        """A caller who asked for a date must not silently receive a time of day.

        ``SubmissionDate`` and the index are midnight; ``BroadcastAt`` keeps its clock, because
        that field *is* a timestamp. The same upstream string therefore lands in two different
        columns at two different precisions, on purpose.
        """
        row = shareholding.loc[pd.Timestamp("2026-06-30")]
        assert row["SubmissionDate"] == pd.Timestamp("2026-07-16")
        assert row["BroadcastAt"] == pd.Timestamp("2026-07-16 19:24:44")
        assert shareholding.index.name == "AsOf"
        assert shareholding.loc[pd.Timestamp("2026-06-30")].name == pd.Timestamp("2026-06-30")
        assert own.normalise_upstream_date("16-JUL-2026 19:24:44") == pd.Timestamp("2026-07-16")

    def test_the_deals_payload_spellings_normalise_into_the_same_index(self, deals):
        """The deals frame's index is built from title-case strings, and it is naive."""
        assert deals.index.name == "Date"
        assert deals.index.tz is None
        assert set(deals.index.strftime("%Y-%m-%d")) == {AS_ON, OFF_AS_ON}
        assert deals.attrs["bfinance_deals_as_on_date_raw"] == "25-Sep-2026"
        assert deals.attrs["bfinance_deals_as_on_date"] == AS_ON

    def test_every_upstream_format_is_recorded_on_the_frame(self, shareholding):
        assert shareholding.attrs["bfinance_upstream_date_formats"] == (
            own.UPSTREAM_DATE, own.UPSTREAM_DATETIME, own.UPSTREAM_ELAPSED
        )

    @pytest.mark.parametrize(
        "raw", [None, "", "-", "--", "::", "  -  "]
    )
    def test_sentinels_read_as_absent(self, raw):
        assert own.normalise_upstream_date(raw) is None

    def test_n_is_not_a_sentinel_because_it_means_two_different_things(self):
        """``"N"`` is ``remarksWeb``'s "no notes" **and** ``revisedData``'s "not revised".

        Collapsing both to ``None`` would make a not-revised filing indistinguishable from a row
        that said nothing at all about revision. Measured: ``"N"`` on 14 of 22 ``remarksWeb`` and
        on 22 of 22 ``revisedData``.
        """
        assert "N" not in own.ABSENT_SENTINELS
        assert own._clean_text("N") == "N"
        shareholding = own.parse_shareholding(load("shareholding_RELIANCE.json"), url="fixture")
        assert (shareholding["Remarks"] == "N").sum() == 14
        assert (shareholding["Revised"] == "N").all()
        assert (shareholding["Remarks"] == "-").sum() == 0

    @pytest.mark.parametrize("raw", ["31-Feb-2026", "30-JUN-2026x", "not-a-date", "2026-13-01"])
    def test_impossible_and_unrecognised_values_are_none_not_an_exception(self, raw):
        assert own.normalise_upstream_date(raw) is None

    def test_the_broadcast_lag_is_a_measured_duration(self, shareholding):
        """``timeDifference`` is not a time of day and not a guess.

        It equals ``systemDate - broadcastDate`` to the second on all 15 rows carrying both, and
        ``"::"`` sits on exactly the 7 rows whose ``systemDate`` is null. A lag of zero would read
        as "identical stamps" rather than "not reported", so the sentinel is ``None``.
        """
        lag = shareholding["BroadcastLagSeconds"]
        observed = (shareholding["SystemAt"] - shareholding["BroadcastAt"]).dt.total_seconds()
        both = lag.notna() & shareholding["SystemAt"].notna()

        assert int(both.sum()) == 15
        assert (observed[both] == lag[both]).all()
        assert lag.max() == 27898.0, "07:44:58 -- longer than a time of day could be"
        assert int(lag.isna().sum()) == 7
        assert int(shareholding["SystemAt"].isna().sum()) == 7
        assert (lag.isna() == shareholding["SystemAt"].isna()).all()
        # The "::" sentinel normalises to None in BOTH columns -- see the module docstring on why
        # the raw text survives for real values but not for the sentinel. The payload still has
        # the literal string on exactly those 7 rows.
        assert shareholding.loc[lag.isna(), "BroadcastLag"].isna().all()
        raw = load("shareholding_RELIANCE.json")
        assert sum(1 for row in raw if row["timeDifference"] == "::") == 7
        assert all(
            (row["timeDifference"] == "::") == (row["systemDate"] is None) for row in raw
        )
        assert own.parse_broadcast_lag("::") is None
        assert own.parse_broadcast_lag("00:00:04") == 4.0
        assert own.parse_broadcast_lag("07:44:58") == 27898.0
        # The raw text form is kept beside the seconds for values upstream does send.
        assert (shareholding.loc[lag.notna(), "BroadcastLag"] == "07:44:58").sum() == 1


# ============================================== the off-cycle filing and the composite key


class TestOffCycleFiling:
    """RELIANCE files off-cycle, and ``broadcastDate`` is what separates the two filings.

    ``30-SEP-2021`` (a quarter end) and ``08-DEC-2021`` (not one) were **both broadcast on
    07-JAN-2022** — 02:55:01 and 03:25:01. So a key built from the broadcast *day* collapses two
    distinct filings, and ``(symbol, date, broadcastDate)`` does not.
    """

    def test_the_off_cycle_filing_is_present_and_is_not_a_quarter_end(self, shareholding):
        off_cycle = shareholding.attrs["bfinance_shareholding_off_cycle_dates"]

        assert off_cycle == ["2021-12-08", "2024-10-29"] == list(own.SHAREHOLDING_OFF_CYCLE_DATES)
        for iso in off_cycle:
            stamp = pd.Timestamp(iso)
            assert (stamp.month, stamp.day) not in {
                (3, 31), (6, 30), (9, 30), (12, 31)
            }, f"{iso} is a quarter end, so it is not evidence of an off-cycle filing"
        assert pd.Timestamp("2021-12-08") in shareholding.index

    def test_the_both_share_a_broadcast_day(self, shareholding):
        """The control for the test below: prove the fixture really contains the collision.

        Without this, ``test_the_full_timestamp_separates_them`` would pass on a fixture where
        nothing collides, and would be checking nothing.
        """
        ident = own.shareholding_identity(shareholding)
        ident = ident.assign(day=ident["BroadcastAt"].dt.date)
        colliding = ident[ident.duplicated(subset=["Symbol", "day"], keep=False)]

        assert len(colliding) == 2, (
            "the fixture no longer contains two filings sharing a broadcast day, so the "
            "disambiguation test below is vacuous"
        )
        assert sorted(colliding["AsOf"].dt.strftime("%Y-%m-%d")) == ["2021-09-30", "2021-12-08"]
        assert set(colliding["day"]) == {pd.Timestamp("2022-01-07").date()}

    def test_the_full_broadcast_timestamp_separates_them(self, shareholding):
        """Both rows survive, and neither is collapsed or deduplicated away."""
        ident = own.shareholding_identity(shareholding)
        pair = ident[ident["BroadcastAt"].dt.date == pd.Timestamp("2022-01-07").date()]

        assert len(pair) == 2
        assert not ident.duplicated(
            subset=["Symbol", "AsOf", "BroadcastAt"]
        ).any(), "the composite key is not unique"
        assert pair["BroadcastAt"].nunique() == 2
        assert len(shareholding) == 22, "a row was lost deduplicating the broadcast day"

    def test_the_as_of_key_is_unique_here_and_says_so(self, shareholding):
        """``(symbol, date)`` happens to be distinct in this capture.

        Asserted because it is a real fact about the data, and asserted *as a fact* rather than
        relied on: a feed that files twice for one period would make this go red rather than
        quietly produce a wrong join.
        """
        ident = own.shareholding_identity(shareholding)
        assert not ident.duplicated(subset=["Symbol", "AsOf"]).any()
        assert shareholding.attrs["bfinance_shareholding_asof_key_is_unique"] is True
        assert shareholding.attrs["bfinance_shareholding_identity_key"] == [
            "Symbol", "AsOf", "BroadcastAt"
        ]

    def test_the_off_cycle_filing_was_broadcast_long_after_submission(self, shareholding):
        """Both filings sat unpublished for weeks, which is why their dates differ at all.

        The two lags are different and both are asserted, because they are easy to swap: the
        quarter-end waited **78 days** and the off-cycle filing **20 days**. Getting them the
        wrong way round would still "look" plausible, which is the point of naming each.
        """
        off_cycle = shareholding.loc[pd.Timestamp("2021-12-08")]
        quarter = shareholding.loc[pd.Timestamp("2021-09-30")]

        assert off_cycle["SubmissionDate"] == pd.Timestamp("2021-12-18")
        assert off_cycle["BroadcastAt"] == pd.Timestamp("2022-01-07 03:25:01")
        assert (off_cycle["BroadcastAt"] - off_cycle["SubmissionDate"]).days == 20

        assert quarter["SubmissionDate"] == pd.Timestamp("2021-10-21")
        assert quarter["BroadcastAt"] == pd.Timestamp("2022-01-07 02:55:01")
        assert (quarter["BroadcastAt"] - quarter["SubmissionDate"]).days == 78
        assert off_cycle["BroadcastAt"] > quarter["BroadcastAt"]

    def test_a_quarterly_consumer_would_lose_two_rows(self, shareholding):
        """The practical consequence, counted rather than asserted in prose."""
        quarter_ends = {(3, 31), (6, 30), (9, 30), (12, 31)}
        kept = [
            stamp for stamp in shareholding.index
            if (stamp.month, stamp.day) in quarter_ends
        ]

        assert len(shareholding) == 22
        assert len(kept) == 20
        assert 22 - len(kept) == len(own.SHAREHOLDING_OFF_CYCLE_DATES)


# ===================================================== revision fields: modelled, unproven


class TestRevisionFields:
    """NSE models restatement explicitly. Nothing here has ever exercised it.

    The revision fields are first-class upstream and this module carries every one of them. But
    across all 22 captured rows ``revisedData == 'N'``, ``revisedStatus == '-'`` and
    ``revisedDate``/``revisionDate``/``revisionRemark`` are null — so the honest description is
    *modelled and unproven*, and these tests exist to keep that description and the data from
    drifting apart.
    """

    def test_every_revision_field_is_carried_through(self, shareholding):
        for column in (
            "Revised", "RevisedStatus", "RevisedDate", "RevisionDate",
            "RevisedRemark", "RevisionRemark",
        ):
            assert column in shareholding.columns, f"{column} was dropped"
            assert column in own.SHAREHOLDING_COLUMNS

    def test_the_observed_reviseddata_histogram_is_recorded(self, shareholding,
                                                            shareholding_payload):
        assert shareholding.attrs["bfinance_shareholding_revised_data"] == {"N": 22}
        assert shareholding.attrs["bfinance_shareholding_restatement_observed"] is False
        assert (shareholding["Revised"] == "N").all()

        # "-" is the absent sentinel, so it normalises to None in the frame. The payload keeps it,
        # and the distinction matters: "no revision status" is not "status = -".
        assert {row["revisedStatus"] for row in shareholding_payload} == {"-"}
        assert shareholding["RevisedStatus"].isna().all()
        assert "-" in own.ABSENT_SENTINELS

    def test_no_revision_date_was_ever_populated(self, shareholding):
        assert shareholding["RevisedDate"].isna().all()
        assert shareholding["RevisionDate"].isna().all()
        assert shareholding["RevisedRemark"].isna().all()
        assert shareholding["RevisionRemark"].isna().all()

    def test_the_module_does_not_claim_restatement_handling_is_verified(self):
        """A docstring that drifts from the data is a defect the test suite cannot otherwise see.

        The claim is pinned in two directions: the honest one must be present, and the
        over-claim must be absent.
        """
        doc = ast.get_docstring(ast.parse(SOURCE)) or ""
        assert "modelled and unproven" in doc.lower()
        assert "no restatement has ever been observed" in doc.lower()
        for overclaim in (
            "restatement handling is verified",
            "restatements are handled",
            "revision-aware",
        ):
            assert overclaim not in doc.lower()

    def test_a_restated_row_would_be_surfaced_if_one_ever_arrived(self):
        """The field is *wired*, not merely present — proven on a synthetic row.

        This does not claim a restatement has been seen. It claims that if NSE sent one, the
        parser would report it rather than swallow it, which is the part that can be tested
        without waiting for an observation.
        """
        payload = [{
            "symbol": "RELIANCE", "date": "31-MAR-2026", "pr_and_prgrp": "50.0",
            "public_val": "50.0", "revisedData": "Y", "revisedStatus": "REVISED",
            "revisedDate": "12-MAY-2026", "revisionDate": "11-MAY-2026",
            "revisionRemark": "corrected promoter holding", "broadcastDate": "12-MAY-2026 19:00:00",
        }]
        frame = own.parse_shareholding(payload, symbol="RELIANCE", url="fixture")
        row = frame.iloc[0]

        assert frame.attrs["bfinance_shareholding_revised_data"] == {"Y": 1}
        assert frame.attrs["bfinance_shareholding_restatement_observed"] is True
        assert row["Revised"] == "Y"
        assert row["RevisedStatus"] == "REVISED"
        assert row["RevisedDate"] == pd.Timestamp("2026-05-12")
        assert row["RevisionDate"] == pd.Timestamp("2026-05-11")
        assert row["RevisionRemark"] == "corrected promoter holding"
        assert row["BroadcastAt"] == pd.Timestamp("2026-05-12 19:00:00")

    def test_a_clock_in_revisiondate_is_truncated_and_that_is_documented(self):
        """The one place this module deliberately loses information, asserted rather than
        discovered.

        ``revisionDate`` is read as a *date* because its name says ``Date``; a value carrying a
        clock loses the clock. That is a decision made with zero observations behind it, so it
        is stated in the docstring and pinned here. ``broadcastDate`` is the contrast: a value
        with a clock has been observed, so the clock is kept.
        """
        payload = [{
            "symbol": "RELIANCE", "date": "31-MAR-2026", "revisedData": "Y",
            "revisionDate": "11-MAY-2026 18:30:00", "broadcastDate": "12-MAY-2026 19:00:00",
        }]
        frame = own.parse_shareholding(payload, url="fixture")
        row = frame.iloc[0]

        assert row["RevisionDate"] == pd.Timestamp("2026-05-11")
        assert row["BroadcastAt"] == pd.Timestamp("2026-05-12 19:00:00")
        assert "deliberately lost" in (own.parse_shareholding.__doc__ or "")

    def test_an_unparseable_revision_date_is_counted_not_swallowed(self):
        payload = [{
            "symbol": "RELIANCE", "date": "31-MAR-2026", "revisedData": "Y",
            "revisedDate": "31-Feb-2026", "broadcastDate": "12-MAY-2026 19:00:00",
        }]
        frame = own.parse_shareholding(payload, url="fixture")

        assert frame.attrs["bfinance_shareholding_unparsed_dates"] == ["revisedDate='31-Feb-2026'"]
        assert frame["RevisedDate"].isna().all()
        assert len(frame) == 1, "the filing must survive a bad revision date"


# ============================================ group 2: all three deal kinds, one request


class TestDealsOneRequest:
    """All seven keys arrive together, and the three counters arrive as **strings**.

    ``BULK_DEALS`` is ``"237"``, not ``237``. It reads as a count, compares unequal to every
    count, and is the only thing in the payload that says whether the arrays came back whole.
    """

    def test_one_payload_carries_all_seven_keys(self, deals_payload):
        assert sorted(deals_payload) == [
            "BLOCK_DEALS", "BLOCK_DEALS_DATA", "BULK_DEALS", "BULK_DEALS_DATA",
            "SHORT_DEALS", "SHORT_DEALS_DATA", "as_on_date",
        ]
        assert len(deals_payload) == 7
        assert own.deals_url() == (
            "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
        )
        assert "?" not in own.deals_url(), "the route takes no parameters, by measurement"

    def test_the_counters_are_strings_not_numbers(self, deals_payload):
        for key, want in (("BULK_DEALS", "237"), ("SHORT_DEALS", "75"), ("BLOCK_DEALS", "6")):
            assert isinstance(deals_payload[key], str), f"{key} is not a string"
            assert deals_payload[key] == want
            assert (deals_payload[key] == int(want)) is False, (
                f"{key} compared equal to its int, so the string typing is not what it claims"
            )

    def test_the_counters_are_used_as_counts(self, deals):
        counts = own.deal_counts(deals)

        assert counts["bulk"] == {
            "declared": 237, "declared_raw": "237", "observed": 237, "agrees": True
        }
        assert counts["short"]["declared"] == 75
        assert counts["block"]["declared"] == 6
        for kind in own.DEAL_KINDS:
            entry = counts[kind]
            assert isinstance(entry["declared"], int)
            assert isinstance(entry["declared_raw"], str)
            assert entry["agrees"] is True, f"{kind} array is not as long as its counter claims"
        assert deals.attrs["bfinance_deals_counter_matches_rows"] == {
            "bulk": True, "short": True, "block": True
        }

    def test_all_three_kinds_come_out_of_one_payload(self, deals):
        assert deals.attrs["bfinance_deals_kinds"] == ["bulk", "short", "block"]
        assert deals.attrs["bfinance_deals_counts_observed"] == {
            "bulk": 237, "short": 75, "block": 6
        }
        assert len(deals) == 318
        assert tuple(deals.columns) == own.DEAL_COLUMNS
        assert set(deals["Kind"]) == set(own.DEAL_KINDS)
        assert deals.attrs["bfinance_provenance"] == own.PROVENANCE_DEALS
        assert deals.attrs["bfinance_source_url"] == DEALS_URL

    def test_a_kind_subset_filters_locally_from_the_same_payload(self, deals_payload):
        """``kinds=`` is a local filter; the request would have been identical either way."""
        short_only = own.parse_deals(deals_payload, kinds=["short"], url=DEALS_URL)

        assert set(short_only["Kind"]) == {"short"}
        assert len(short_only) == 75
        assert short_only.attrs["bfinance_deals_counts_observed"] == {"short": 75}
        with pytest.raises(ValueError):
            own.parse_deals(deals_payload, kinds=["options"], url=DEALS_URL)

    def test_the_route_is_snapshot_only_and_says_so(self, deals):
        assert deals.attrs["bfinance_deals_snapshot_only"] is True
        assert deals.attrs["bfinance_deals_accepts_date_parameter"] is False
        assert "snapshot-capital-market-blockdeal" in (
            ast.get_docstring(ast.parse(SOURCE)) or ""
        ), "the 404 sibling route should stay on the record: block deals ride in this payload"

    def test_a_date_argument_is_an_assertion_that_raises_when_it_does_not_match(self, deals_payload):
        """The route takes no ``date``, so a date argument can only be checked, never honoured.

        A sibling route was measured to ignore a ``date=`` argument and answer 200 with the
        current day, so accepting one here without a check would be a silent wrong-day answer.
        """
        with pytest.raises(own.DealsSnapshotMismatch) as excinfo:
            own.parse_deals(deals_payload, expected_date=OFF_AS_ON, url=DEALS_URL)
        assert "snapshot-only" in str(excinfo.value)

        assert len(own.parse_deals(deals_payload, expected_date=AS_ON, url=DEALS_URL)) == 318
        assert len(own.parse_deals(
            deals_payload, expected_date=pd.Timestamp("2026-09-25"), url=DEALS_URL
        )) == 318

    def test_a_non_object_payload_raises(self):
        with pytest.raises(ParsingError):
            own.parse_deals([1, 2, 3], url=DEALS_URL)
        with pytest.raises(ParsingError):
            own.parse_deals({"as_on_date": "25-Sep-2026", "BULK_DEALS_DATA": {}}, url=DEALS_URL)

    def test_the_notional_is_labelled_derived_not_published(self, deals):
        assert "DERIVED" in own.UNITS["deals"]["NotionalCr"]
        assert "not published" in deals.attrs["bfinance_deals_notional_basis"]
        priced = deals.dropna(subset=["WATP"])
        expected = priced["Qty"] * priced["WATP"] / 1e7
        assert (priced["NotionalCr"].round(6) == expected.round(6)).all()
        assert deals["NotionalCr"].isna().sum() == 75, "the short deals have no price"


# ============================================== clientName: the module's reason to exist


class TestClientName:
    """``clientName`` is the counterparty, and no other free source in this package names it.

    It is load-bearing: two ``AXIOMGAS`` ``SELL`` 186,000 @ 54.75 bulk rows on 25-Sep-2026 differ
    in nothing else, so dropping the column collapses 318 distinguishable rows into 317.
    """

    def test_the_column_is_carried_and_filled_where_upstream_filled_it(self, deals, deals_payload):
        assert "ClientName" in deals.columns
        assert int(deals["ClientName"].notna().sum()) == 243
        assert len(deals) == 318

        upstream = sum(
            1
            for key in ("BULK_DEALS_DATA", "SHORT_DEALS_DATA", "BLOCK_DEALS_DATA")
            for row in deals_payload[key]
            if row["clientName"] is not None
        )
        assert upstream == 243, "the frame and the payload disagree about the nulls"

    def test_a_real_counterparty_survives_end_to_end(self, deals):
        row = deals[
            (deals["Symbol"] == "ADANIENT")
            & (deals["Kind"] == "block")
            & (deals["Qty"] == 3450000)
        ].iloc[0]

        assert row["ClientName"] == "ADANI INFRA (INDIA) LIMITED"
        assert row["Name"] == "Adani Enterprises Limited"
        assert row["Side"] == "BUY"
        assert row["WATP"] == 2905.0
        assert row["NotionalCr"] == round(3450000 * 2905 / 1e7, 6)

    def test_dropping_clientname_collapses_a_real_row_pair(self, deals):
        """The negative control, as a live assertion rather than a promise.

        Without ``ClientName`` the frame is not smaller by one column; it is *wrong by one row*,
        because two distinct counterparties become indistinguishable. The comparison is all nine
        columns against the other eight — a hand-picked subset that simply omits ``ClientName``
        would report the same duplicate count either way and prove nothing.
        """
        every = list(deals.columns)
        without_client = [c for c in every if c != "ClientName"]

        assert len(deals) == 318
        assert deals.drop_duplicates(subset=every).shape[0] == 318
        assert deals.drop_duplicates(subset=without_client).shape[0] == 317
        assert int(deals.duplicated(subset=every).sum()) == 0
        assert int(deals.duplicated(subset=without_client).sum()) == 1

        pair = deals[
            (deals["Symbol"] == "AXIOMGAS")
            & (deals["Kind"] == "bulk")
            & (deals["Side"] == "SELL")
            & (deals["Qty"] == 186000)
            & (deals["WATP"] == 54.75)
        ]
        assert len(pair) == 2
        assert pair["ClientName"].nunique() == 2
        assert sorted(pair["ClientName"]) == ["BINDU R BAFNA", "VBCUBE VENTURES FUND"]

    def test_the_collapsed_pair_is_only_a_pair_at_that_price(self, deals):
        """AXIOMGAS sold 186,000 three times; two of them at the same price.

        Worth pinning because it shows the distinction is a *price* collision, not a symbol
        collision: filtering on symbol and quantity alone gives three rows and would overstate
        what ``clientName`` rescues.
        """
        same_qty = deals[
            (deals["Symbol"] == "AXIOMGAS")
            & (deals["Kind"] == "bulk")
            & (deals["Qty"] == 186000)
        ]
        assert len(same_qty) == 3
        assert sorted(same_qty["WATP"].unique()) == [54.20, 54.75]
        assert sorted(same_qty["ClientName"]) == [
            "BINDU R BAFNA", "RISHI R BAFNA", "VBCUBE VENTURES FUND"
        ]
        assert int(same_qty["WATP"].duplicated().sum()) == 1

    def test_the_client_column_is_never_an_empty_string(self, deals):
        """A blank client is a missing one, not a client called "".

        ``_clean_text`` maps the sentinels to ``None``; a string that survives has real content.
        """
        present = deals["ClientName"].dropna()
        assert len(present) == 243
        assert (present.str.len() > 0).all()
        assert (present.str.strip() == present).all()


# ================================================ trap 1: as_on_date misses the short deals


class TestTrap1MixedDates:
    """One payload, two dates, and 100% of the short deals on the wrong one.

    ``as_on_date`` is ``25-Sep-2026``. All 75 short-deal rows are ``24-Sep-2026``. Filtering on
    ``as_on_date`` therefore drops every short deal, with no error and no warning — and returns
    a frame that looks like a quiet market. The supported filter is the per-row ``date``.
    """

    def test_the_payload_really_carries_both_dates(self, deals):
        assert deals.attrs["bfinance_deals_row_dates"] == [OFF_AS_ON, AS_ON]
        assert deals.attrs["bfinance_deals_mixed_dates"] is True
        by_kind = {
            kind: sorted(set(deals.loc[deals["Kind"] == kind].index.strftime("%Y-%m-%d")))
            for kind in own.DEAL_KINDS
        }
        assert by_kind == {
            "bulk": [AS_ON], "short": [OFF_AS_ON], "block": [AS_ON]
        }

    def test_filtering_on_as_on_date_silently_drops_every_short_deal(self, deals):
        """The trap, reproduced in the test so it cannot be rediscovered by a caller.

        243 rows survive and **zero** of them are short deals. Nothing raises; nothing warns; the
        result is a frame that is confidently a subset.
        """
        naive = deals[deals.index == pd.Timestamp(AS_ON)]

        assert len(naive) == 243
        assert len(deals) == 318
        assert int((naive["Kind"] == "short").sum()) == 0
        assert len(naive) - len(deals[deals["Kind"] != "short"]) == 0

    def test_the_per_row_date_filter_keeps_them(self, deals):
        short = own.deals_for_date(deals, OFF_AS_ON)

        assert len(short) == 75
        assert set(short["Kind"]) == {"short"}
        assert set(short.index.strftime("%Y-%m-%d")) == {OFF_AS_ON}
        assert len(own.deals_for_date(deals, AS_ON)) == 243
        assert len(own.deals_for_date(deals, "23-Sep-2026")) == 0
        assert short.attrs["bfinance_deals_off_as_on_date_rows"] == 75

    def test_the_filter_accepts_every_spelling_a_caller_might_write(self, deals):
        for spelling in ("24-Sep-2026", "2026-09-24", pd.Timestamp("2026-09-24")):
            assert len(own.deals_for_date(deals, spelling)) == 75
        with pytest.raises(ValueError):
            own.deals_for_date(deals, "not-a-date")

    def test_the_frame_index_is_the_per_row_date_not_as_on_date(self, deals):
        assert deals.index.name == "Date"
        assert deals.index.max() == pd.Timestamp(AS_ON)
        assert deals.index.min() == pd.Timestamp(OFF_AS_ON)
        assert deals.attrs["bfinance_deals_off_as_on_date_rows"] == 75
        assert deals.attrs["bfinance_deals_off_as_on_date_rows"] == int(
            (deals.index != pd.Timestamp(AS_ON)).sum()
        )

    def test_the_supported_filter_reaches_every_row_and_the_other_one_does_not(self, deals):
        """The property, stated so that losing it is loud.

        Filtering on the per-row date over both days returns the **whole** frame; filtering on
        ``as_on_date`` returns 243 of 318. Stated as coverage of the frame rather than as "returns
        75 rows", because the second phrasing is satisfied by a filter that is merely right about
        one day.
        """
        both = pd.concat(
            [own.deals_for_date(deals, AS_ON), own.deals_for_date(deals, OFF_AS_ON)]
        )
        naive = deals[deals.index == pd.Timestamp(AS_ON)]

        assert len(both) == len(deals) == 318
        assert len(naive) == 243
        assert len(deals) - len(naive) == deals.attrs["bfinance_deals_off_as_on_date_rows"]
        assert set(both["Kind"]) == set(own.DEAL_KINDS)
        assert set(naive["Kind"]) == {"bulk", "block"}, (
            "an as_on_date filter is expected to lose the short deals entirely"
        )

    def test_a_filtered_slice_keeps_its_attrs(self, deals):
        slice_ = own.deals_for_date(deals, OFF_AS_ON)
        assert slice_.attrs["bfinance_provenance"] == own.PROVENANCE_DEALS
        assert slice_.attrs["bfinance_deals_as_on_date"] == AS_ON
        assert slice_.attrs["bfinance_deals_unpopulated_kinds"] == ["short"]


# ============================================ trap 2: the short-deal rows are largely null


class TestTrap2NullDensity:
    """75 short-deal rows, and half of every cell is null.

    A short deal arrives with a symbol, a name, a quantity and a date — and no side, no client and
    no price. Presenting that as data is how "NSE reported 75 short deals" gets written down. So
    the density is counted, the all-null columns are named, and :func:`is_populated` lets a
    caller separate *empty* from *unpopulated* before they make that claim.
    """

    def test_the_measured_density_is_recorded_per_kind(self, deals):
        density = deals.attrs["bfinance_deals_null_density"]

        assert density == {"bulk": 0.0, "short": 0.5, "block": 0.125}
        assert deals.attrs["bfinance_deals_null_cells"] == {
            "bulk": 0, "short": 300, "block": 6
        }
        assert deals.attrs["bfinance_deals_total_cells"] == {
            "bulk": 1896, "short": 600, "block": 48
        }

    def test_the_density_is_recomputable_from_the_payload(self, deals, deals_payload):
        """Not a hard-coded constant: derived from the rows as they arrived, per kind.

        Counted on the **payload**, and the scope is recorded in ``attrs``, because a cell holding
        ``"-"`` is a populated cell upstream. The frame-level figure is separate — see the next
        test for why the two differ.
        """
        keys = {
            "bulk": "BULK_DEALS_DATA", "short": "SHORT_DEALS_DATA", "block": "BLOCK_DEALS_DATA"
        }
        for kind in own.DEAL_KINDS:
            rows = deals_payload[keys[kind]]
            nulls = sum(1 for row in rows for value in row.values() if value is None)
            cells = len(rows) * 8  # 8 upstream row keys

            assert nulls == deals.attrs["bfinance_deals_null_cells"][kind], kind
            assert cells == deals.attrs["bfinance_deals_total_cells"][kind], kind
            assert deals.attrs["bfinance_deals_null_density"][kind] == round(
                nulls / cells, 6
            ), kind
        assert deals.attrs["bfinance_deals_null_density_scope"] == "raw_payload"

    def test_the_frame_level_density_is_reported_separately_and_differs(self, deals):
        """Both numbers, and the reason they differ, are on the record.

        Bulk ``remarks`` is ``"-"`` upstream — a value, so a populated cell — and normalises to
        ``None`` in the frame. So the frame's bulk density is 0.25 where the payload's is 0.000.
        A consumer who recomputed one and compared it with the other would otherwise conclude
        the module had miscounted.
        """
        frame_density = deals.attrs["bfinance_deals_frame_null_density"]

        assert frame_density == {"bulk": 0.25, "short": 1.0, "block": 0.25}
        assert deals.attrs["bfinance_deals_null_density"] == {
            "bulk": 0.0, "short": 0.5, "block": 0.125
        }
        assert all(0.0 <= value <= 1.0 for value in frame_density.values()), (
            "a null share above 1.0 is a divide-by-the-wrong-denominator bug, not data"
        )
        for kind in own.DEAL_KINDS:
            subset = deals.loc[deals["Kind"] == kind, list(own.DEAL_NULLABLE_SOURCE_COLUMNS)]
            cells = len(subset) * 4
            assert round(int(subset.isna().sum().sum()) / cells, 6) == frame_density[kind], kind
        assert set(own.DEAL_NULLABLE_SOURCE_COLUMNS) == {
            "Side", "ClientName", "WATP", "Remarks"
        }
        assert "NotionalCr" not in own.DEAL_NULLABLE_SOURCE_COLUMNS, (
            "NotionalCr is derived and inherits WATP's nulls; counting it double-counts"
        )

    def test_the_all_null_columns_are_named(self, deals):
        assert deals.attrs["bfinance_deals_all_null_columns"] == {
            "bulk": [],
            "short": ["buySell", "clientName", "remarks", "watp"],
            "block": ["remarks"],
        }
        short = deals[deals["Kind"] == "short"]
        for column in ("Side", "ClientName", "WATP", "Remarks"):
            assert short[column].isna().all(), f"short {column} was supposed to be all null"
        assert short["Qty"].notna().all()
        assert short["Symbol"].notna().all()

    def test_short_is_reported_unpopulated_and_bulk_and_block_are_not(self, deals):
        assert deals.attrs["bfinance_deals_unpopulated_kinds"] == ["short"]
        assert own.is_populated(deals, "short") is False
        assert own.is_populated(deals, "bulk") is True
        assert own.is_populated(deals, "block") is True
        with pytest.raises(ValueError):
            own.is_populated(deals, "options")

    def test_parsing_warns_naming_the_unpopulated_kind(self, deals_payload, caplog):
        """A warning, because a frame that is 50% null is not something a caller should have to
        go looking for. Asserted on the message so the kind cannot be dropped from it."""
        with caplog.at_level("WARNING", logger="bfinance"):
            own.parse_deals(deals_payload, url=DEALS_URL)

        messages = [record.getMessage() for record in caplog.records]
        assert any("UNPOPULATED" in message for message in messages)
        assert any("short" in message for message in messages)

    def test_empty_and_unpopulated_are_distinguishable(self, deals):
        """The two cases are different facts, and ``is_populated`` alone cannot tell them apart.

        Both are ``False``. What separates them is the observed row count, so the test asserts on
        that too — this is why the module records counts rather than only a boolean.
        """
        empty_payload = {
            "as_on_date": "25-Sep-2026", "BULK_DEALS": "0", "SHORT_DEALS": "0",
            "BLOCK_DEALS": "0", "BULK_DEALS_DATA": [], "SHORT_DEALS_DATA": [],
            "BLOCK_DEALS_DATA": [],
        }
        empty = own.parse_deals(empty_payload, url="synthetic")

        assert own.is_populated(empty, "short") is False
        assert own.is_populated(deals, "short") is False
        assert empty.attrs["bfinance_deals_counts_observed"]["short"] == 0
        assert deals.attrs["bfinance_deals_counts_observed"]["short"] == 75
        assert len(empty) == 0
        assert len(deals) == 318
        assert empty.attrs["bfinance_deals_null_density"]["short"] == 0.0
        assert deals.attrs["bfinance_deals_null_density"]["short"] == 0.5

    def test_the_threshold_is_a_named_constant_not_a_magic_number(self, deals):
        assert own.DEAL_NULL_DENSITY_UNPOPULATED == 0.25
        assert deals.attrs["bfinance_deals_null_density_threshold"] == 0.25
        # Raising it above block's 0.125 excludes block too; the threshold is the caller's to
        # move, which is why it is a parameter and not a hard-coded comparison.
        assert own.is_populated(deals, "block", threshold=0.2) is True
        assert own.is_populated(deals, "block", threshold=0.1) is False
        assert own.is_populated(deals, "bulk", threshold=0.1) is True


# ================================================== group 3a: the pledge probe, not a parser


class TestPledgeProbe:
    """The pledge route is live, answers 200, and has never returned a row.

    So this package ships a **probe**, not a parser. The tests below are as much about what is
    *absent* as what is present: no pledge field name, no ``parse_pledge``, no ``DataFrame``.
    """

    def test_the_probe_reports_reachable_empty_and_unverified(self, pledge_payload):
        probe = own.probe_pledge_payload(
            pledge_payload, url=PLEDGE_URL, http_status=200
        )

        assert isinstance(probe, own.PledgeProbe)
        assert probe.reachable is True, "a 200 is not a 404"
        assert probe.row_count == 0
        assert probe.row_schema_verified is False
        assert probe.http_status == 200
        assert probe.url == PLEDGE_URL
        assert probe.provenance == own.PROVENANCE_PLEDGE
        assert probe.observed_keys == ("comNameList", "data")

    def test_the_verdict_states_all_three_facts(self, pledge_payload):
        probe = own.probe_pledge_payload(pledge_payload, url=PLEDGE_URL)

        assert "reachable" in probe.verdict
        assert "empty" in probe.verdict
        assert "UNVERIFIED" in probe.verdict
        assert probe.row_schema_verified is False, "an empty payload verifies nothing"

    def test_the_probe_is_not_a_frame(self, pledge_payload):
        """If this ever returns a frame, the honest answer has been replaced by an empty one."""
        probe = own.probe_pledge_payload(pledge_payload, url=PLEDGE_URL)

        assert not isinstance(probe, pd.DataFrame)
        assert not isinstance(probe, (list, dict))
        assert own.probe_pledge_payload.__doc__ is not None
        assert "No row parsing" in own.probe_pledge_payload.__doc__

    def test_the_observed_keys_are_the_ones_that_were_observed(self, pledge_payload):
        """Naming ``comNameList``/``data`` is not invention — they were in the body.

        The line the module will not cross is a *row* field, and the next test is about that.
        """
        assert sorted(pledge_payload) == ["comNameList", "data"]
        assert own.probe_pledge_payload(pledge_payload).observed_keys == tuple(
            sorted(pledge_payload)
        )

    def test_no_pledge_row_field_is_named_anywhere_in_the_module(self):
        """The absence, asserted structurally rather than textually.

        A text scan for ``"pledge"`` would fire on every honest sentence explaining that the
        parser was refused, so this checks two things that *can* be decided:

        1. there is no column-shaped constant for the unverified payload, and
        2. the pure probe's only string literal is ``"data"`` — the one key that was observed in
           the body. A pledge *row* field cannot be named by that function because it is not
           written there, and a future edit that added one would turn this red immediately.
        """
        tree = ast.parse(SOURCE)
        constants = {}
        for node in tree.body:
            # Only literal-bearing assignments are read. `logger = logging.getLogger(...)` is a
            # module-level Assign whose value is a Call, and literal_eval raises on it.
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(
                node.value, (ast.Constant, ast.Tuple, ast.List, ast.Dict, ast.Set)
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = ast.literal_eval(node.value)
        column_shaped = [
            name
            for name, value in constants.items()
            if isinstance(value, (tuple, list, dict))
            and any(token in name.upper() for token in ("PLEDGE", "FPI"))
        ]
        assert column_shaped == [], (
            f"a column-shaped constant for an unverified payload exists: {column_shaped}"
        )

        probe = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "probe_pledge_payload"
        )
        # clean=False, so the compared value is the same raw string the AST holds. The default
        # cleans indentation and the two would never match.
        docstring = ast.get_docstring(probe, clean=False)
        literals = {
            node.value
            for node in ast.walk(probe)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value  # the `url or ""` default is an empty string, not a key
            and node.value != docstring
        }
        assert literals == {"data"}, (
            f"the pledge probe names strings beyond the one observed key: {sorted(literals)}"
        )

        pledge_functions = [
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "pledge" in node.name
        ]
        assert sorted(pledge_functions) == [
            "pledge_url", "probe_pledge", "probe_pledge_async", "probe_pledge_payload"
        ]
        assert not hasattr(own, "parse_pledge")
        assert not hasattr(own, "parse_pledge_payload")

    def test_the_probe_cannot_report_a_field_it_has_never_seen(self, pledge_payload):
        """Structural consequence of the previous test, stated as behaviour.

        The probe's whole vocabulary is one key, so ``hasattr`` on anything field-shaped is False
        and there is no attribute to read off it. There is nothing to guess at.
        """
        probe = own.probe_pledge_payload(pledge_payload, url=PLEDGE_URL)

        assert not hasattr(probe, "columns")
        assert not hasattr(probe, "rows")
        assert not hasattr(probe, "data")
        assert set(probe.__dataclass_fields__) == {
            "reachable", "row_count", "observed_keys", "row_schema_verified", "http_status",
            "url", "cache_status", "provenance", "verdict",
        }

    def test_a_non_envelope_payload_reads_as_not_reachable(self):
        probe = own.probe_pledge_payload("not json", url=PLEDGE_URL)

        assert probe.reachable is False
        assert probe.observed_keys == ()
        assert "NOT reachable" in probe.verdict

    def test_a_payload_with_rows_would_flip_row_schema_verified(self):
        """Shows the flag is computed, not hard-coded to ``False``."""
        probe = own.probe_pledge_payload({"comNameList": [], "data": [{"anything": 1}]})

        assert probe.row_count == 1
        assert probe.row_schema_verified is True
        assert "1 rows" in probe.verdict
        assert "verified" in probe.verdict

    def test_the_probe_url_is_the_measured_route_in_its_own_date_spelling(self):
        assert own.PLEDGE_API_PATH == "/api/corporate-pledgedata"
        assert PLEDGE_URL == (
            "https://www.nseindia.com/api/corporate-pledgedata?index=equities&symbol=YESBANK"
            "&from_date=01-01-2018&to_date=27-09-2026"
        )
        assert "from_date=01-01-2018" in PLEDGE_URL, "the route takes DD-MM-YYYY, not DD-MMM-YYYY"
        with pytest.raises(NotAnNseSymbolError):
            own.pledge_url("RELIANCE.BO")


# ==================================================== group 3b: FPI, blocked and typed


class TestFpiBlocked:
    """403 from ``/api/quote-equity``, so this module asks for nothing and parses nothing.

    A parser for a payload nobody has seen is fiction with a docstring. The correct answer is a
    typed refusal that names the route, the status and the edge — and a status object that a
    caller can read without spending a request.
    """

    def test_the_status_is_typed_and_unavailable(self):
        status = own.fpi_ownership_status()

        assert status is own.FPI_BLOCKED_STATUS
        assert isinstance(status, own.FpiOwnershipStatus)
        assert status.available is False
        assert status.capability == "fpi_ownership_limit_and_utilisation"
        assert status.route == "/api/quote-equity"
        assert status.http_status_observed == 403
        assert "Akamai" in status.edge
        assert status.parser_implemented is False

    def test_calling_it_raises_a_typed_error_naming_the_route(self):
        with pytest.raises(own.FpiOwnershipUnavailableError) as excinfo:
            own.fpi_ownership("RELIANCE")

        message = str(excinfo.value)
        assert "RELIANCE" in message
        assert "/api/quote-equity" in message
        assert "403" in message
        assert issubclass(own.FpiOwnershipUnavailableError, UpstreamServiceError)

    def test_no_fpi_parser_exists(self):
        names = [name for name in dir(own) if "fpi" in name.lower()]

        assert not any("parse" in name for name in names), names
        assert not any(
            "fpi" in name.lower() for name in dir(own.OwnershipClient)
        ), "the client must not offer an FPI method that cannot work"
        assert not hasattr(own, "parse_fpi")
        assert not hasattr(own, "fpi_frame")

    def test_neither_entry_point_can_reach_a_socket(self, monkeypatch):
        """The refusal is structural, not a policy that could be reordered away.

        ``httpx.AsyncClient`` is replaced with something that raises on construction. If either
        call reached the transport, this test fails.
        """
        def forbidden(*args, **kwargs):  # noqa: ARG001
            raise AssertionError("FPI must never construct a client or request anything")

        monkeypatch.setattr(own.httpx, "AsyncClient", forbidden)
        assert own.fpi_ownership_status().available is False
        with pytest.raises(own.FpiOwnershipUnavailableError):
            own.fpi_ownership("RELIANCE")

    def test_the_module_docstring_records_the_block_and_that_it_was_not_attempted(self):
        doc = ast.get_docstring(ast.parse(SOURCE)) or ""

        assert "/api/quote-equity" in doc
        assert "403 Access Denied" in doc
        assert "not** requested" in doc or "was **not** requested" in doc


# ================================================================ caching: hit and stale


class TestCache:
    """Cached means no request; stale means exactly one. Both asserted on the URL list.

    The check that matters is not "the test was fast" but "``handler`` was never called", so every
    test here drives a real :class:`OwnershipClient` with a recording transport and reads
    ``attrs['bfinance_cache_status']`` off the result.
    """

    async def test_a_cached_shareholding_call_makes_no_request(self, monkeypatch, temp_cache,
                                                               shareholding_payload):
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )
        payload = shareholding_payload

        first = _StubHttpx(respond_with(payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.shareholding_patterns_async("RELIANCE")
        assert len(first.requests) == 1
        assert first.requests[0] == SHAREHOLDING_URL

        # Second call: a handler that fails the test if it is reached at all.
        second = _StubHttpx(exploding_handler)
        monkeypatch.setattr(own, "httpx", second)
        frame = await client.shareholding_patterns_async("RELIANCE")

        assert second.requests == [], "a request was issued despite a fresh cache entry"
        assert frame.attrs["bfinance_cache_status"] == "hit"
        assert len(frame) == 22
        assert frame.attrs["bfinance_provenance"] == own.PROVENANCE_SHAREHOLDING

    async def test_a_stale_shareholding_call_requests_again(self, monkeypatch, temp_cache,
                                                            shareholding_payload):
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )

        first = _StubHttpx(respond_with(shareholding_payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.shareholding_patterns_async("RELIANCE")
        assert len(first.requests) == 1

        # Expire the entry the way the passage of time would, rather than by mutating the
        # client's memory: the point is that the *cache* goes stale, not the client.
        temp_cache.set(
            "ownership_shareholding_equities_RELIANCE", shareholding_payload,
            category="shareholding", ttl_hours=-1.0,
        )
        assert temp_cache.get_result(
            "ownership_shareholding_equities_RELIANCE"
        ).status == "expired"

        second = _StubHttpx(respond_with(shareholding_payload))
        monkeypatch.setattr(own, "httpx", second)
        frame = await client.shareholding_patterns_async("RELIANCE")

        assert len(second.requests) == 1, "a stale entry must be refetched exactly once"
        assert second.requests[0] == SHAREHOLDING_URL
        assert frame.attrs["bfinance_cache_status"] == "miss"
        assert len(frame) == 22

    async def test_a_cached_deals_call_makes_no_request(self, monkeypatch, temp_cache,
                                                        deals_payload):
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )
        first = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.deals_async()
        assert len(first.requests) == 1

        second = _StubHttpx(exploding_handler)
        monkeypatch.setattr(own, "httpx", second)
        frame = await client.deals_async()

        assert second.requests == []
        assert frame.attrs["bfinance_cache_status"] == "hit"
        assert len(frame) == 318

    async def test_a_stale_deals_call_requests_again(self, monkeypatch, temp_cache, deals_payload):
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )
        first = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.deals_async()
        temp_cache.set(
            "ownership_deals_snapshot", deals_payload, category="deals", ttl_hours=-1.0
        )

        second = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", second)
        await client.deals_async()

        assert len(second.requests) == 1
        assert second.requests[0] == DEALS_URL

    async def test_a_cached_pledge_probe_makes_no_request(self, monkeypatch, temp_cache,
                                                          pledge_payload):
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )
        first = _StubHttpx(respond_with(pledge_payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.probe_pledge_async("YESBANK", "2018-01-01", "2026-09-27")
        assert len(first.requests) == 1

        second = _StubHttpx(exploding_handler)
        monkeypatch.setattr(own, "httpx", second)
        probe = await client.probe_pledge_async("YESBANK", "2018-01-01", "2026-09-27")

        assert second.requests == []
        assert probe.cache_status == "hit"
        assert probe.row_count == 0
        assert probe.row_schema_verified is False

    async def test_force_refresh_requests_despite_a_fresh_entry(self, monkeypatch, temp_cache,
                                                               deals_payload):
        """The escape hatch for a snapshot that NSE revises in place."""
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=2
        )
        first = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", first)
        await client.deals_async()

        second = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", second)
        frame = await client.deals_async(force_refresh=True)

        assert len(second.requests) == 1
        assert frame.attrs["bfinance_cache_status"] == "miss"

    async def test_cache_none_means_every_call_requests(self, monkeypatch, deals_payload):
        client = own.OwnershipClient(cache=None, min_request_interval_ms=0, max_retries=2)
        assert client.cache is None
        stub = _StubHttpx(respond_with(deals_payload))
        monkeypatch.setattr(own, "httpx", stub)

        await client.deals_async()
        await client.deals_async()

        assert len(stub.requests) == 2, "an explicit cache=None must not silently cache anyway"
        assert own.OwnershipClient(
            cache=None
        ).cache is None

    def test_the_ttls_are_declared_and_ordered_by_how_fast_the_data_moves(self):
        ttl = own.OwnershipClient.CATEGORY_TTL_HOURS

        assert ttl == {"shareholding": 24.0, "deals": 1.0, "pledge": 24.0}
        assert ttl["deals"] < ttl["shareholding"], (
            "a snapshot NSE revises in place must not outlive a quarterly filing"
        )
        client = own.OwnershipClient(cache=None)
        assert client.ttl_hours("deals") == 1.0
        with pytest.raises(KeyError):
            client.ttl_hours("options")

    async def test_the_client_survives_a_403_by_priming_once(self, monkeypatch, temp_cache,
                                                            shareholding_payload):
        """The corporate-actions retry pattern, exercised: 403 -> prime -> retry once.

        A second block must raise rather than loop, so a persistently blocked edge costs one
        bounded pair of requests instead of a retry storm against it.
        """
        client = own.OwnershipClient(
            cache=temp_cache, min_request_interval_ms=0, max_retries=3
        )
        seen: list = []

        def handler(request):
            seen.append(str(request.url))
            if len(seen) == 1:
                return httpx.Response(403, text="Access Denied")
            return httpx.Response(200, json=shareholding_payload)

        stub = _StubHttpx(handler)
        monkeypatch.setattr(own, "httpx", stub)
        frame = await client.shareholding_patterns_async("RELIANCE")

        assert client.primed_cookie is True
        assert seen[0] == SHAREHOLDING_URL
        assert own.LANDING_URL in seen[1]
        assert len(frame) == 22

        always_blocked = _StubHttpx(lambda request: httpx.Response(403, text="nope"))
        monkeypatch.setattr(own, "httpx", always_blocked)
        with pytest.raises(UpstreamServiceError):
            await own.OwnershipClient(
                cache=temp_cache, min_request_interval_ms=0, max_retries=3
            ).shareholding_patterns_async("TCS", force_refresh=True)

    def test_the_sync_entry_points_work_offline(self, monkeypatch, shareholding_payload,
                                                deals_payload, pledge_payload):
        """The wrappers callers actually import, through the same offline path."""
        stub = _StubHttpx(
            lambda request: httpx.Response(
                200,
                json=(
                    shareholding_payload
                    if "share-holdings" in str(request.url)
                    else deals_payload
                    if "largedeal" in str(request.url)
                    else pledge_payload
                ),
            )
        )
        monkeypatch.setattr(own, "httpx", stub)
        client = own.OwnershipClient(cache=None, min_request_interval_ms=0, max_retries=2)

        assert len(client.shareholding_patterns("RELIANCE")) == 22
        assert len(client.deals()) == 318
        assert client.probe_pledge("YESBANK").row_count == 0
        assert len(stub.requests) == 3


# ================================================================ negative controls


class TestNegativeControls:
    """The controls that prove the tests above are not vacuous.

    Each of these computes the *wrong* behaviour from the same fixture and asserts the defect is
    really there. A test that asserts "the filter keeps the short deals" is only worth anything
    if something in the suite proves the fixture would have made that filter fail.
    """

    def test_control_the_as_on_date_filter_really_does_drop_the_short_deals(self, deals):
        """If this ever passes with 75 short deals surviving, Trap 1 is no longer in the data."""
        as_on = deals.attrs["bfinance_deals_as_on_date"]
        naive = deals[deals.index == pd.Timestamp(as_on)]
        lost = deals[deals.index != pd.Timestamp(as_on)]

        assert len(lost) == 75
        assert set(lost["Kind"]) == {"short"}
        assert int((naive["Kind"] == "short").sum()) == 0
        assert int(deals.attrs["bfinance_deals_off_as_on_date_rows"]) == len(lost)

    def test_control_the_fixture_really_is_half_null_for_short_deals(self, deals, deals_payload):
        """Trap 2's density, recomputed, so neither the 0.5 nor the 1.0 can go stale.

        Two different denominators on purpose, and confusing them is the mistake this pins:
        upstream the short rows carry 8 keys and 4 of them are null — 300/600, **0.5**. The frame
        exposes only the 4 nullable output columns and all 4 are null — 300/300, **1.0**.
        """
        columns = list(own.DEAL_NULLABLE_SOURCE_COLUMNS)
        short = deals.loc[deals["Kind"] == "short", columns]
        frame_nulls = int(short.isna().sum().sum())

        raw = deals_payload["SHORT_DEALS_DATA"]
        payload_nulls = sum(1 for row in raw for value in row.values() if value is None)

        assert (frame_nulls, payload_nulls) == (300, 300)
        assert payload_nulls / (len(raw) * 8) == 0.5
        assert deals.attrs["bfinance_deals_null_density"]["short"] == 0.5
        assert deals.attrs["bfinance_deals_frame_null_density"]["short"] == 1.0
        assert deals.attrs["bfinance_deals_null_density_scope"] == "raw_payload"

    def test_control_the_clientname_column_is_load_bearing(self, deals):
        """Recomputed, so a future edit that quietly drops the column turns this red."""
        every = list(deals.columns)
        without_client = [c for c in every if c != "ClientName"]

        assert int(deals.duplicated(subset=every).sum()) == 0
        assert int(deals.duplicated(subset=without_client).sum()) == 1, (
            "dropping clientName no longer merges any rows, so the column stopped mattering"
        )
        assert "ClientName" in every and "ClientName" not in without_client

    def test_control_the_broadcast_day_collision_is_really_two_filings(self, shareholding):
        """The off-cycle test's premise, asserted separately from its conclusion."""
        ident = own.shareholding_identity(shareholding)
        days = ident["BroadcastAt"].dt.date
        counts = days.value_counts()

        assert int(counts.max()) == 2
        assert days.nunique() == 21, "22 filings on 21 broadcast days is the collision"
        assert len(ident) == 22
        assert int(ident["BroadcastAt"].nunique()) == 22, "no two filings share a timestamp"

    def test_control_the_bare_array_would_really_break_a_dict_shaped_call(self):
        """The shape test's premise, in each of the three forms a caller actually writes it."""
        payload = json.loads(SHAREHOLDING_BYTES)

        with pytest.raises(TypeError):
            payload["data"]
        with pytest.raises(AttributeError):
            payload.get("data")
        with pytest.raises(KeyError):
            [row["data"] for row in payload]
        assert len(own.shareholding_rows(payload)) == 22, (
            "the parser must not need the dict-shaped access to work"
        )

    def test_control_the_normaliser_would_fail_on_a_naive_string_compare(self, deals_payload,
                                                                        shareholding_payload):
        """The two payloads spell the same month differently, so text is not identity.

        ``30-JUN-2026`` and ``30-Jun-2026`` are the same day and two different strings. Anything
        that grouped, joined or de-duplicated on the raw text across these two payloads would
        treat one day's filings as unrelated to that day's deals.
        """
        upper, title = "30-JUN-2026", "30-Jun-2026"

        assert upper != title
        assert upper.split("-")[1] == "JUN"
        assert title.split("-")[1] == "Jun"
        assert own.normalise_upstream_date(upper) == own.normalise_upstream_date(title)
        assert own.normalise_upstream_date(upper) == pd.Timestamp("2026-06-30")

        # And the live case across payloads: the deals payload's spelling is title-case.
        assert deals_payload["BULK_DEALS_DATA"][0]["date"] == "25-Sep-2026"
        assert shareholding_payload[0]["date"] == "30-JUN-2026"
        assert (
            deals_payload["BULK_DEALS_DATA"][0]["date"].split("-")[1]
            != shareholding_payload[0]["date"].split("-")[1]
        )


# ============================================== no network at import, and no eager import


class TestNoNetworkAtImportOrParse:
    """The stronger version of "this file is offline": sockets are made fatal, in a subprocess.

    In-process monkeypatching cannot catch a request made on a thread the monkeypatch did not
    reach, and an import-time client is exactly the kind of thing that would do it. So this runs
    a separate interpreter with ``socket.connect`` and friends replaced, imports the package,
    runs every parser over every fixture, and calls both FPI entry points.
    """

    PROGRAM = textwrap.dedent(
        """
        import socket

        def forbidden(*args, **kwargs):
            raise AssertionError("network I/O in the ownership test surface")

        socket.socket.connect = forbidden
        socket.socket.connect_ex = forbidden
        socket.create_connection = forbidden
        socket.getaddrinfo = forbidden

        import bfinance
        import bfinance.ownership as own

        import json
        from pathlib import Path

        fixtures = Path("tests/fixtures/nse/ownership")
        shareholding = json.loads(
            (fixtures / "shareholding_RELIANCE.json").read_text(encoding="utf-8")
        )
        deals = json.loads(
            (fixtures / "deals_snapshot_20260925.json").read_text(encoding="utf-8")
        )
        pledge = json.loads(
            (fixtures / "pledge_yesbank_EMPTY.json").read_text(encoding="utf-8")
        )

        assert len(own.parse_shareholding(shareholding, url="x")) == 22
        assert len(own.parse_deals(deals, url="x")) == 318
        assert own.probe_pledge_payload(pledge, url="x").row_count == 0

        # The FPI surface must not reach a transport either.
        assert own.fpi_ownership_status().available is False
        try:
            own.fpi_ownership("RELIANCE")
        except own.FpiOwnershipUnavailableError:
            pass
        else:
            raise AssertionError("fpi_ownership did not raise")

        # Constructing a client must not fetch anything.
        client = own.OwnershipClient(cache=None)
        assert client.primed_cookie is False
        print("OK")
        """
    )

    def test_importing_and_parsing_touches_no_socket(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", self.PROGRAM],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr[-2000:]!r}"
        )
        assert "OK" in result.stdout

    def test_importing_bfinance_does_not_pull_in_ownership(self):
        """Not imported eagerly, so an unrelated caller pays nothing for it.

        Run in a subprocess because ``bfinance.ownership`` is very much in ``sys.modules`` in
        this process — it was imported by the test module above, so an in-process check could
        only ever report the wrong answer.
        """
        program = (
            "import sys, bfinance; "
            "assert 'bfinance.ownership' not in sys.modules, sorted(sys.modules); "
            "print('OK')"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout

    def test_the_module_does_not_import_trendlyne(self):
        """``bfinance`` is a leaf: importing it must not drag in a sibling data package.

        The same rule the existing ``TestIsolation`` enforces elsewhere, asserted here for this
        module because it is new and imports two other bfinance submodules.
        """
        offenders = [
            node.module
            for node in ast.walk(ast.parse(SOURCE))
            if isinstance(node, ast.ImportFrom) and node.module
            and "trendlyne" in node.module
        ]
        offenders += [
            alias.name
            for node in ast.walk(ast.parse(SOURCE))
            if isinstance(node, ast.Import)
            for alias in node.names
            if "trendlyne" in alias.name
        ]
        assert offenders == []


# ================================================= module shortcuts and surface hygiene


class TestModuleSurface:
    """Three capability groups, one module, and no eager global client."""

    def test_the_three_groups_have_their_own_provenance(self, shareholding, deals, pledge_payload):
        assert shareholding.attrs["bfinance_provenance"] == own.PROVENANCE_SHAREHOLDING
        assert deals.attrs["bfinance_provenance"] == own.PROVENANCE_DEALS
        assert own.probe_pledge_payload(
            pledge_payload
        ).provenance == own.PROVENANCE_PLEDGE
        assert len({
            own.PROVENANCE_SHAREHOLDING, own.PROVENANCE_DEALS, own.PROVENANCE_PLEDGE
        }) == 3
        assert shareholding.attrs["bfinance_capability"] == "shareholding_patterns"
        assert deals.attrs["bfinance_capability"] == "bulk_short_block_deals"

    def test_every_result_records_its_source_url(self, shareholding, deals, pledge_payload):
        assert shareholding.attrs["bfinance_source_url"] == SHAREHOLDING_URL
        assert deals.attrs["bfinance_source_url"] == DEALS_URL
        assert own.probe_pledge_payload(pledge_payload, url=PLEDGE_URL).url == PLEDGE_URL
        for frame in (shareholding, deals):
            assert frame.attrs["bfinance_source_url"].startswith("https://www.nseindia.com/api/")

    def test_no_client_is_built_at_import(self):
        """A module-level client would make the first *call* the first I/O, at import time."""
        assert own._DEFAULT_CLIENT is None
        assert "OwnershipClient()" not in (own.__doc__ or "")

    def test_the_shortcuts_delegate_to_the_client(self):
        for name in (
            "shareholding_patterns", "shareholding_patterns_async", "deals", "deals_async",
            "probe_pledge", "probe_pledge_async",
        ):
            assert callable(getattr(own, name)), name

    def test_validate_index_normalises_without_a_whitelist(self):
        assert own.validate_index(" Equities ") == "equities"
        assert own.validate_index("mf") == "mf", (
            "an unmeasured segment must not be refused as if it were known to fail"
        )
        with pytest.raises(ValueError):
            own.validate_index("  ")
        with pytest.raises(ValueError):
            own.validate_index(None)
