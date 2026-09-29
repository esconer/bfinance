"""
The richer search route exists to say *what* an instrument is, and its categories are the reason.

Measured live on 2026-09-30 against ``/member/api/ac_snames/all/?term={q}&all-results=true``, which
returns **22 fields** where the plain route at ``/member/api/ac_snames/`` returns 4:

    {"id": "NIFTYBEES", "label": "Nippon India ETF Nifty 50 BeES(NIFTYBEES)",
     "value": "NIFTYBEES", "k": 1588, "slugname": "nippon-india-etf-nifty-50-bees",
     "country": "IND", "defaultExchange": "NSE", "stock_code": "NIFTYBEES",
     "UIcode": "NIFTYBEES", "category": "ETF", "pageType": 10, "NSEcode": "NIFTYBEES",
     "BSEcode": "590103", "isCfd": false, "currentPrice": 259.47, "day_change": -1.0,
     "day_changeP": -0.38, "day_high": 260.49, "day_low": 257.9, "sectorName": "Others",
     "industry": {}, "nexturl": "https://trendlyne.com/equity/1588/NIFTYBEES/...",
     "urls": [["Overview", "https://trendlyne.com/equity/1588/NIFTYBEES/..."]]}

Across **136 distinct (category, symbol) pairs from 8 queries**, the category vocabulary is six
strings and two of them are traps:

    category        n     pageType   can this package fetch OHLC for it?
    Equity          73    2          yes
    Index           26    24         yes
    ETF             12    10         yes  (and absent from the equity sitemap entirely)
    Mutual Funds    11    12         NO   -- separate namespace, NAV not OHLC
    Not Traded       9    2          NO   -- delisted or suspended
    Superstars       5    (absent)   unknown

A ``Mutual Funds`` hit has no OHLC route on this package at all -- the package's own not-found message
already says mutual funds are a separate namespace at ``/mutual-fund/nav/{id}/`` -- and a ``Not
Traded`` hit is delisted. Resolving to either and returning a price history would be a **wrong**
answer, not a thin one, so the category is carried on the result and never used to filter silently.

The two-flag gate is the other thing under test. This route is a second path into the same
robots-disallowed ``/member/`` namespace, so it needs ``allow_member_search=True`` **and**
``allow_full_search=True``, it must refuse before a socket opens, and it must name the flag that is
actually missing rather than listing both as if either would do.

No test here opens a socket: the real client is driven through ``httpx.MockTransport``, with the
recorded payloads verbatim.
"""
from __future__ import annotations

import httpx
import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne.errors import TrendlyneRobotsDisallowedError, TrendlyneSchemaError

#: One real record, verbatim, with the fields trimmed to none -- the 22 keys are the measurement and
#: the test carries all of them so a schema change is a visible diff rather than a silent pass.
NIFTYBEES_RECORD = {
    "id": "NIFTYBEES",
    "label": "Nippon India ETF Nifty 50 BeES(NIFTYBEES)",
    "value": "NIFTYBEES",
    "k": 1588,
    "slugname": "nippon-india-etf-nifty-50-bees",
    "country": "IND",
    "defaultExchange": "NSE",
    "stock_code": "NIFTYBEES",
    "UIcode": "NIFTYBEES",
    "category": "ETF",
    "pageType": 10,
    "NSEcode": "NIFTYBEES",
    "BSEcode": "590103",
    "isCfd": False,
    "currentPrice": 259.47,
    "day_change": -1.0,
    "day_changeP": -0.38,
    "day_high": 260.49,
    "day_low": 257.9,
    "sectorName": "Others",
    "industry": {},
    "nexturl": "https://trendlyne.com/equity/1588/NIFTYBEES/nippon-india-etf-nifty-50-bees/",
    "urls": [["Overview", "https://trendlyne.com/equity/1588/NIFTYBEES/nippon-india-etf-nifty-50-bees/"]],
}

#: One record per measured category, with the pageType the vendor sent alongside it. The
#: (category, pageType) pair is the evidence, so it is recorded rather than paraphrased.
CATEGORY_RECORDS = {
    "Equity": (2, {"id": "RELIANCE", "k": 1127, "slugname": "reliance", "category": "Equity",
                   "label": "Reliance Industries(RALIANCE)"}),
    "Index": (24, {"id": "NIFTYNXT50", "k": 1888, "slugname": "nifty-next-50",
                   "category": "Index", "label": "NIFTY NEXT 50(NIFTYNXT50)"}),
    "ETF": (10, dict(NIFTYBEES_RECORD)),
    "Mutual Funds": (12, {"id": "F000010N6E", "k": 900001,
                          "slugname": "uti-nifty-next-50-index-fund-direct-growth",
                          "category": "Mutual Funds",
                          "label": "UTI Nifty Next 50 Index Fund Direct Growth(F000010N6E)"}),
    "Not Traded": (2, {"id": "RELCAPITAL", "k": 600001, "slugname": "reliance-capital-limited",
                       "category": "Not Traded",
                       "label": "Reliance Capital Limited(RELCAPITAL)"}),
    "Superstars": (None, {"id": "reliance-capital-limited", "k": 600001,
                          "slugname": "reliance-capital-limited", "category": "Superstars",
                          "label": "reliance capital limited(reliance-capital-limited)"}),
}


def full_handler(payload=None, seen=None):
    """A transport that answers the ``/all/`` route with a recorded payload and records the URLs."""
    body = NIFTYBEES_RECORD if payload is None else payload
    # A single record is given as a bare dict for readability; anything else -- a list, or a
    # dict-shaped envelope -- is passed through exactly as given, so the wrapper cannot be what
    # makes a shape test pass.
    if isinstance(body, dict) and "k" in body:
        body = [body]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        if "ac_snames" in str(request.url):
            return httpx.Response(200, json=body)
        return httpx.Response(404, text="")

    return handler


def client(seen=None, payload=None, **flags) -> tl.TrendlyneClient:
    flags.setdefault("allow_member_search", True)
    flags.setdefault("allow_full_search", True)
    return tl.TrendlyneClient(
        transport=httpx.MockTransport(full_handler(payload, seen)),
        min_request_interval_ms=0.0,
        max_retries=1,
        **flags,
    )


def search(query="NIFTYBEES", *, payload=None, **kwargs) -> list:
    """Drive the real client through the mock transport, with a recorded payload."""
    return tl._run_async(client(payload=payload).search_full_async(query, **kwargs))


# ------------------------------------------------------------------ the two-flag gate


class TestTheGateNeedsBothFlags:
    def test_the_new_flag_is_off_by_default(self):
        """Off is the only shipped configuration, exactly like :data:`MEMBER_SEARCH_FLAG`."""
        assert tl.TrendlyneClient().allow_full_search is False
        assert tl.TrendlyneClient(allow_full_search=False).allow_full_search is False
        assert tl.TrendlyneClient(allow_full_search=True).allow_full_search is True

    def test_the_flag_name_is_the_constant(self):
        """A caller blocked by policy needs to read the switch off the error, so it is one name."""
        assert tl.FULL_SEARCH_FLAG == "allow_full_search"

    def test_both_flags_off_refuses_and_names_allow_member_search(self):
        with pytest.raises(TrendlyneRobotsDisallowedError) as excinfo:
            client(allow_member_search=False, allow_full_search=False).search_full("NIFTYBEES")
        message = str(excinfo.value)
        assert tl.MEMBER_SEARCH_FLAG in message
        assert "/member/api/ac_snames/" in message
        assert "Disallow: */member/" in message

    def test_only_allow_member_search_still_refuses(self):
        """
        The two-flag rule itself. One flag is not a weaker version of two; it is a closed gate, and
        this is the case a caller hits by opening the old switch and assuming the new one follows.
        """
        c = client(allow_member_search=True, allow_full_search=False)
        with pytest.raises(TrendlyneRobotsDisallowedError) as excinfo:
            c.search_full("NIFTYBEES")
        message = str(excinfo.value)
        assert tl.FULL_SEARCH_FLAG in message, "the missing flag must be named by its constant"
        assert "the one that is closed" in message, (
            "and it must be identified as the missing one, not listed alongside an open flag"
        )

    def test_only_allow_full_search_still_refuses(self):
        """The rule is symmetric: the member gate is not implied by the new one."""
        c = client(allow_member_search=False, allow_full_search=True)
        with pytest.raises(TrendlyneRobotsDisallowedError) as excinfo:
            c.search_full("NIFTYBEES")
        assert tl.MEMBER_SEARCH_FLAG in str(excinfo.value)
        assert "not made" in str(excinfo.value)

    def test_the_refusal_costs_zero_requests(self):
        """A refusal that still made the call would be a partial fix, not a fix."""
        for flags in (
            {"allow_member_search": False, "allow_full_search": False},
            {"allow_member_search": True, "allow_full_search": False},
            {"allow_member_search": False, "allow_full_search": True},
        ):
            seen: list = []
            c = client(seen=seen, **flags)
            with pytest.raises(TrendlyneRobotsDisallowedError):
                c.search_full("NIFTYBEES")
            assert seen == [], f"{flags} still requested a disallowed path: {seen}"

    def test_the_constructor_flag_opens_the_route(self):
        c = client(allow_member_search=True, allow_full_search=True)
        assert [r.symbol for r in c.search_full("NIFTYBEES")] == ["NIFTYBEES"]

    def test_the_per_call_flags_override_a_closed_constructor(self):
        """
        Both overrides, since a per-call switch that works for one flag and not the other would be
        a surprise in exactly the direction this method is gated.
        """
        seen: list = []
        c = client(seen=seen, allow_member_search=False, allow_full_search=False)
        with pytest.raises(TrendlyneRobotsDisallowedError):
            c.search_full("NIFTYBEES")
        assert seen == [], "the closed gate spent nothing before the overrides were passed"

        found = c.search_full("NIFTYBEES", allow_member_search=True, allow_full_search=True)
        assert [r.symbol for r in found] == ["NIFTYBEES"]
        assert len(seen) == 1, "the opt-in costs exactly the one request it authorises"

    def test_a_per_call_full_flag_does_not_borrow_the_constructor_member_flag(self):
        """Each override governs its own flag, and neither is inferred from the other."""
        c = client(allow_member_search=False, allow_full_search=True)
        with pytest.raises(TrendlyneRobotsDisallowedError) as excinfo:
            c.search_full("NIFTYBEES", allow_full_search=True)
        assert tl.MEMBER_SEARCH_FLAG in str(excinfo.value), (
            "the member flag is still shut, and the message must say so"
        )

    def test_the_async_twin_is_gated_identically(self):
        with pytest.raises(TrendlyneRobotsDisallowedError):
            tl._run_async(client(allow_full_search=False).search_full_async("NIFTYBEES"))

    def test_an_empty_query_is_a_value_error_not_a_request(self):
        seen: list = []
        with pytest.raises(ValueError):
            client(seen=seen).search_full("   ")
        assert seen == []


# ------------------------------------------------------------------ the name


class TestLabelBecomesAName:
    def test_the_trailing_ticker_is_removed(self):
        record = search()[0]
        assert record.name == "Nippon India ETF Nifty 50 BeES"
        assert record.name != NIFTYBEES_RECORD["label"], (
            "the ticker is already in .symbol; keeping it twice is not a name"
        )

    def test_a_name_with_its_own_parentheses_survives(self):
        """
        The case a blind ``rsplit("(", 1)`` gets wrong. "Tata Elxsi (India) Ltd" is a name, not a
        wrapper, and stripping the last group regardless of content would yield "Tata Elxsi" -- a
        different company's plausible-looking name.
        """
        payload = dict(NIFTYBEES_RECORD, id="ELXSI", k=1234, slugname="tata-elxsi",
                       label="Tata Elxsi (India) Ltd(ELXSI)")
        record = search(payload=payload)[0]
        assert record.name == "Tata Elxsi (India) Ltd"
        assert "(India)" in record.name, "the name's own parentheses were removed"

    def test_a_parenthetical_that_is_not_the_ticker_is_left_alone(self):
        """
        Only a trailing group *equal to the ticker* is stripped. Here the last group is a scheme
        plan, so stripping it would delete part of the name.
        """
        payload = dict(NIFTYBEES_RECORD, id="F000010N6E", k=900001,
                       label="UTI Nifty Next 50 Index Fund(Direct Growth)(F000010N6E)")
        record = search(payload=payload)[0]
        assert record.name == "UTI Nifty Next 50 Index Fund(Direct Growth)"

    def test_a_label_with_no_parentheses_is_returned_whole(self):
        payload = dict(NIFTYBEES_RECORD, label="Nippon India ETF Nifty 50 BeES")
        assert search(payload=payload)[0].name == "Nippon India ETF Nifty 50 BeES"

    def test_a_missing_label_falls_back_to_the_slug(self):
        """Degrades the field rather than the call; nothing is invented to fill it."""
        payload = dict(NIFTYBEES_RECORD)
        payload.pop("label")
        record = search(payload=payload)[0]
        assert record.name == "nippon-india-etf-nifty-50-bees"
        assert record.symbol == "NIFTYBEES", "and the record itself is unaffected"

    def test_an_empty_label_falls_back_rather_than_returning_nothing(self):
        record = search(payload=dict(NIFTYBEES_RECORD, label="   "))[0]
        assert record.name == "nippon-india-etf-nifty-50-bees"

    def test_the_fallback_order_is_slug_then_symbol(self):
        """With neither label nor slug, the symbol is still better than an empty string."""
        assert tl._display_name("", "NIFTYBEES", "some-slug") == "some-slug"
        assert tl._display_name("", "NIFTYBEES", "") == "NIFTYBEES"
        assert tl._display_name("", "", "") == ""


# ------------------------------------------------------------------ the category


class TestTheCategoryIsCarried:
    @pytest.mark.parametrize("vendor, expected, page_type", [
        ("Equity", tl.SymbolCategory.EQUITY, 2),
        ("ETF", tl.SymbolCategory.ETF, 10),
        ("Index", tl.SymbolCategory.INDEX, 24),
        ("Mutual Funds", tl.SymbolCategory.MUTUAL_FUND, 12),
        ("Not Traded", tl.SymbolCategory.NOT_TRADED, 2),
        ("Superstars", tl.SymbolCategory.OTHER, None),
    ])
    def test_each_measured_category_maps_as_measured(self, vendor, expected, page_type):
        """
        All six of the strings the 136 measured pairs produced, with the pageType each arrived with
        recorded in the table above. ``Superstars`` is the one that is *not* mapped: it arrived with
        no pageType and describes a vendor-side grouping, so it is unknown rather than an equity.
        """
        assert tl._VENDOR_CATEGORIES.get(vendor, tl.SymbolCategory.OTHER) is expected
        assert CATEGORY_RECORDS[vendor][0] == page_type
        payload = CATEGORY_RECORDS[vendor][1]
        record = search(payload=payload)[0]
        assert record.category is expected
        assert record.vendor_category == vendor, "the raw string is kept so OTHER is auditable"

    def test_an_unrecognised_vendor_string_is_unknown_not_guessed(self):
        """
        The failure direction matters. Guessing an unmeasured string into ``equity`` would assert
        that a price history can be fetched for an instrument nobody has checked, which is the
        wrong-instrument defect in a new coat.
        """
        record = search(payload=dict(NIFTYBEES_RECORD, category="SME"))[0]
        assert record.category is tl.SymbolCategory.OTHER
        assert record.vendor_category == "SME"
        assert record.category is not tl.SymbolCategory.EQUITY

    @pytest.mark.parametrize("value", [None, "", "   ", 42, "EQUITY", "mutual funds"])
    def test_anything_outside_the_measured_vocabulary_is_other(self, value):
        """
        Case-sensitive on purpose. A case-insensitive lookup would be a guess about a vocabulary
        nobody has measured, and the failure it risks is a wrong category rather than a missing one.
        """
        assert tl.parse_symbol_category(value) is tl.SymbolCategory.OTHER

    def test_surrounding_whitespace_is_stripped_but_case_is_not(self):
        """
        Padding is noise and is removed; capitalisation is the vendor's own and is not second-guessed.
        One of the two and not the other is a deliberate line, so it is pinned.
        """
        assert tl.parse_symbol_category(" Equity ") is tl.SymbolCategory.EQUITY
        assert tl.parse_symbol_category("equity") is tl.SymbolCategory.OTHER

    def test_a_category_is_a_str_so_it_serialises_as_its_own_value(self):
        import json

        assert tl.SymbolCategory.ETF == "etf"
        assert json.loads(json.dumps({"c": tl.SymbolCategory.ETF})) == {"c": "etf"}

    def test_the_vocabulary_is_closed(self):
        """Six measured values, no aliases and no inferred extras."""
        assert {m.value for m in tl.SymbolCategory} == {
            "equity", "etf", "index", "mutual_fund", "not_traded", "other"
        }

    def test_a_sitemap_record_is_not_claimed_to_be_an_equity(self):
        """
        The default. A sitemap entry carries no category field, and defaulting it to ``EQUITY``
        would be a claim about 5,922 records that was never measured.
        """
        assert tl.TrendlyneSymbol("MOTHERSON", 878, "samvardhana-motherson-international-ltd"
                                  ).category is tl.SymbolCategory.OTHER


class TestHasOhlc:
    @pytest.mark.parametrize("category", ["equity", "etf", "index"])
    def test_measured_as_serving_ohlc(self, category):
        assert tl.SymbolCategory(category).has_ohlc() is True

    @pytest.mark.parametrize("category", ["mutual_fund", "not_traded"])
    def test_measured_as_having_no_ohlc_route(self, category):
        """
        A mutual fund has no OHLC route on this package and a delisted line has no price. Either
        one handed back with bars is a **wrong** answer rather than an empty one, which is the whole
        reason the category is carried.
        """
        assert tl.SymbolCategory(category).has_ohlc() is False

    def test_unknown_is_not_claimed_to_be_fetchable(self):
        """
        ``other`` answers False rather than raising or returning None: the method answers "can you
        fetch it", and an unmeasured category has not earned a True. Read it as unknown, not as a
        claim that the instrument has no history.
        """
        assert tl.SymbolCategory.OTHER.has_ohlc() is False

    def test_it_is_reachable_from_the_result_without_importing_anything(self):
        record = search(payload=CATEGORY_RECORDS["Mutual Funds"][1])[0]
        assert record.category.has_ohlc() is False, (
            "the whole point of carrying it: a caller can branch before asking for bars"
        )


# ------------------------------------------------------------------ shapes and limits


class TestResponseShapes:
    def test_a_bare_json_list_is_the_measured_shape(self):
        """The top level of this route is a list, not an object with a ``data`` key."""
        found = search()
        assert len(found) == 1
        assert (found[0].symbol, found[0].stock_id, found[0].slug) == (
            "NIFTYBEES", 1588, "nippon-india-etf-nifty-50-bees"
        )

    def test_the_dict_with_data_shape_is_parsed_too(self):
        found = search(payload={"data": [NIFTYBEES_RECORD]})
        assert [r.symbol for r in found] == ["NIFTYBEES"]
        found = search(payload={"results": [NIFTYBEES_RECORD]})
        assert [r.symbol for r in found] == ["NIFTYBEES"]

    @pytest.mark.parametrize("payload", ["a string", 42, 3.5, True])
    def test_a_non_list_non_envelope_is_reported_not_returned_as_a_miss(self, payload):
        """
        Reported, not coerced. Returning ``[]`` for a shape nobody has seen would be a silent miss
        reading exactly like "trendlyne has no such symbol", which is the false negative this
        package was corrected for elsewhere.
        """
        with pytest.raises(TrendlyneSchemaError) as excinfo:
            search(payload=payload)
        assert "expected a list" in str(excinfo.value)

    def test_an_envelope_with_neither_data_nor_results_reads_as_no_match(self):
        """
        The one shape that resolves to an empty list, and it does so through the same
        ``get("data") or get("results") or []`` the plain route uses. Stated here so the two
        neighbouring cases are not confused: an envelope is tolerated, a scalar is not.
        """
        assert search(payload={"unexpected": "shape"}) == []

    def test_the_vendor_no_match_sentinel_is_skipped_not_invented(self):
        assert search(payload=[{"headline": "No matches found"}]) == []

    def test_a_record_without_an_id_is_skipped_rather_than_filled(self):
        payload = [{"k": 1, "id": "", "slugname": "x"}, NIFTYBEES_RECORD]
        assert [r.symbol for r in search(payload=payload)] == ["NIFTYBEES"]

    def test_the_request_carries_the_all_results_query_string(self):
        seen: list = []
        client(seen=seen).search_full("NIFTYBEES")
        assert "ac_snames/all/" in seen[0]
        assert "all-results=true" in seen[0]
        assert "term=NIFTYBEES" in seen[0]

    def test_the_xhr_header_is_sent(self):
        """
        Measured: this route answers HTTP 200 with a non-JSON body when the header is missing, so
        omitting it is a silent failure rather than an error status.
        """
        seen_headers: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(request.headers.get("X-Requested-With"))
            return httpx.Response(200, json=[NIFTYBEES_RECORD])

        c = tl.TrendlyneClient(transport=httpx.MockTransport(handler),
                               min_request_interval_ms=0.0, max_retries=1,
                               allow_member_search=True, allow_full_search=True)
        c.search_full("NIFTYBEES")
        assert seen_headers == ["XMLHttpRequest"]


class TestLimit:
    @pytest.fixture
    def many(self):
        return [
            dict(NIFTYBEES_RECORD, id=f"SYM{i}", k=1000 + i, slugname=f"sym-{i}",
                 label=f"Some Instrument {i}(SYM{i})", category="Equity")
            for i in range(20)
        ]

    def test_the_limit_is_honoured(self, many):
        assert len(search(payload=many, limit=3)) == 3
        assert len(search(payload=many, limit=1)) == 1
        assert len(search(payload=many, limit=10)) == 10

    def test_the_vendor_ranking_is_preserved(self, many):
        """
        The order is the vendor's only claim about which candidate was meant. Re-sorting it here
        would assert a preference this package has not measured.
        """
        assert [r.symbol for r in search(payload=many, limit=5)] == [
            "SYM0", "SYM1", "SYM2", "SYM3", "SYM4"
        ]

    def test_a_limit_below_one_still_returns_one(self, many):
        """Matches :meth:`search_remote_async` rather than quietly returning nothing."""
        assert len(search(payload=many, limit=0)) == 1


# ------------------------------------------------------------------ compatibility


class TestNothingExistingBroke:
    def test_the_symbol_still_constructs_with_three_positional_arguments(self):
        """
        :func:`parse_sitemap`, :meth:`search_remote_async` and the existing tests all build
        ``TrendlyneSymbol(symbol, stock_id, slug)`` positionally. The three new fields are appended
        and defaulted, so none of those call sites changes.
        """
        record = tl.TrendlyneSymbol("MOTHERSON", 878, "samvardhana-motherson-international-ltd")
        assert (record.symbol, record.stock_id, record.slug) == (
            "MOTHERSON", 878, "samvardhana-motherson-international-ltd"
        )
        assert record.name == ""
        assert record.category is tl.SymbolCategory.OTHER
        assert record.vendor_category == ""

    def test_sitemap_records_parse_exactly_as_before(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://trendlyne.com/equity/878/MOTHERSON/samvardhana/</loc></url>"
            "</urlset>"
        )
        assert tl.parse_sitemap(xml)["MOTHERSON"] == tl.TrendlyneSymbol(
            "MOTHERSON", 878, "samvardhana"
        )

    def test_the_plain_route_is_untouched_by_the_new_gate(self):
        """
        Opening ``allow_full_search`` must not have opened, closed or altered the route that
        :meth:`resolve_any_async` depends on. It still needs only one flag, or nothing works.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"k": 1588, "id": "NIFTYBEES", "value": "NIFTYBEES", "slugname": "niftybees"}
            ])

        c = tl.TrendlyneClient(transport=httpx.MockTransport(handler),
                               min_request_interval_ms=0.0, max_retries=1,
                               allow_member_search=True)
        assert [r.symbol for r in c.search_remote("NIFTYBEES")] == ["NIFTYBEES"]

    def test_it_is_not_wired_into_resolution(self):
        """
        The reason this method exists separately. ``"nifty bees"`` is fuzzy and ranked, and taking
        its first hit is what made ``ohlc("nifty bees")`` return BANKBEES' bars (commit
        ``06f6c7c``). Feeding this route into resolution would recreate that defect.
        """
        from pathlib import Path

        source = Path(tl.__file__).read_text(encoding="utf-8")
        body = source.split("async def resolve_any_async", 1)[1].split(
            "\n    def resolve_any", 1
        )[0]
        assert "search_full" not in body, (
            "resolve_any_async must not consult the fuzzy, ranked route"
        )

    def test_the_new_names_are_exported(self):
        import bfinance.trendlyne as pkg

        for name in ("FULL_SEARCH_FLAG", "SymbolCategory", "parse_symbol_category"):
            assert name in tl.__all__
            assert name in pkg.__all__
            assert getattr(pkg, name) is getattr(tl, name)

    def test_the_robots_map_covers_the_new_path(self):
        """
        The table exists so a path is absent only when this package cannot build it. The
        ``/all/`` sub-path is covered by the same broad rule -- ``*`` is a leading wildcard with no
        trailing ``$`` -- but the map is keyed by path, so the path has to be in it.
        """
        path = tl._SYMBOL_SEARCH_FULL_PATH.split("?")[0]
        assert path == "/member/api/ac_snames/all/"
        assert tl.ROBOTS_DISALLOWED_ROUTES[path] == "Disallow: */member/"


# ------------------------------------------------------------------ non-vacuity


class TestTheseTestsCanFail:
    def test_the_two_flag_rule_is_not_a_single_flag_rule(self):
        """
        The gate is the security-relevant half of this change, so it is proved to discriminate: the
        same call, the same transport, the same query, differing only in the second flag.
        """
        refused = client(allow_member_search=True, allow_full_search=False)
        allowed = client(allow_member_search=True, allow_full_search=True)
        with pytest.raises(TrendlyneRobotsDisallowedError):
            refused.search_full("NIFTYBEES")
        assert [r.symbol for r in allowed.search_full("NIFTYBEES")] == ["NIFTYBEES"]

    def test_the_category_is_read_from_the_payload_not_invented(self):
        """Five different categories off five different payloads, through the real client."""
        seen = {c: search(payload=CATEGORY_RECORDS[c][1])[0].category for c in
                ("Equity", "Index", "ETF", "Mutual Funds", "Not Traded")}
        assert len(set(seen.values())) == 5, "the mapping collapsed categories together"

    def test_a_mangled_name_would_be_caught(self):
        """
        The negative control for the parentheses rule, on the case a blind rsplit actually breaks:
        a label that ends in a name's own parenthetical and carries no ticker to remove. rsplit
        would cut the name in half and the half it discards is the part that identifies the
        company, so this asserts the two differ rather than restating the rule.
        """
        label = "Tata Elxsi (India) Ltd"
        blind = label.rsplit("(", 1)[0]
        assert blind == "Tata Elxsi ", "the naive implementation truncates the name"
        assert tl._display_name(label, "ELXSI", "tata-elxsi") == "Tata Elxsi (India) Ltd"
