"""
trendlyne section-page tests — fully offline, driven by the 2026-10-10 recordings.

Every fixture here was fetched from trendlyne.com on 2026-10-10 against TCS (id 1372) and NTPC
(id 959); ``META.json`` in the fixture directory records each URL, byte count, SHA-256 and trim
rule. The HTTP layer runs through ``httpx.MockTransport``, so **no test in this file touches the
network** and none is ``live``-marked.

Which symbol the offline tests resolve, and why that is not a mistake
--------------------------------------------------------------------
``sitemap_excerpt_tcs.xml`` is a byte-identical copy of the MOTHERSON-only excerpt: a fresh TCS
window of ``/equity-sitemap-stocks.xml`` could not be cut without a network request, and inventing
``<url>`` blocks for TCS would be inventing vendor data. So these tests resolve **MOTHERSON** (id
878) through the sitemap and the mock transport keys on the **route substring**, serving the TCS
bodies from the fixtures above under the MOTHERSON id. Every TCS fixture's path differs from the
MOTHERSON one by the stock id alone, which is exactly the substitution the mock makes. A test that
cares about the id therefore asserts the template that ``build_path`` produces, not a hardcoded
TCS URL — which is the stronger claim anyway.

One ordering trap, and it is a real one rather than a test artefact
------------------------------------------------------------------
``/equity/monthly-mutual-fund-share-holding/...`` **contains** the substring ``share-holding``.
A handler that checks ``share-holding`` first serves the share-holding page to every mutual-fund
request, and the mutual-fund extractor then fails on a page it has never been designed to read.
:data:`ROUTES` is ordered longest-needle-first for exactly that reason, and
``TestTheMockItselfCannotBeTheThingThatPasses`` pins it.

What these tests are actually about
-----------------------------------
- **one request per call.** Fourteen new routes — eleven server-rendered pages and three
  ``?format=json`` siblings — makes request count a contract, not an implementation detail: it is
  what keeps a caller who wants three sections from spending three times the site's per-session
  WAF budget. Every method's count is asserted against the path ``build_path`` produces.
- **the guards.** WAF (405 and the header form, terminal and unretried), the share-holding
  snapshot-only refusal, the mutual-fund route's *absence* of that guard, and the envelope's
  ``head.status != "0"``.
- **shapes that were verified off the fixtures**, not shapes someone assumed: news is a card list
  with no table at all, the SWOT tables are single-column with the fact as their header cell, and
  the two buy-sell-zone grids are byte-identical and can only be told apart by their tab pane.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne import tables as tbt
from bfinance.trendlyne.errors import (
    TrendlyneAccessDeniedError,
    TrendlyneNotAuthorizedError,
    TrendlyneNotFoundError,
    TrendlyneServerError,
    TrendlyneWafChallengeError,
)

# The mock-transport seam, the handler factory and the fixture readers are shared with the core
# module rather than copied: two copies of a request stub can drift into testing different things.
# `tests/` is not a package (no `__init__.py`) and pytest prepends *its own* directory to
# `sys.path`, so the module is imported by bare name here, exactly as pytest imports it — the same
# arrangement tests/test_screener_market_offline.py uses for the same reason.
from test_trendlyne_core import mock_client, payload, text

#: The symbol these tests resolve. MOTHERSON, because that is what the recorded sitemap excerpt
#: carries; see the module docstring for the substitution the mock makes.
SYMBOL = "MOTHERSON"
RECORD = tl.parse_sitemap(text("sitemap_excerpt.xml"))[SYMBOL]

#: Route substring -> fixture, **ordered longest needle first**. The mutual-fund route's own path
#: contains ``share-holding``, so a naive ordering silently serves the wrong page.
ROUTES: Tuple[Tuple[str, str], ...] = (
    ("/equity/monthly-mutual-fund-share-holding/", "monthly_mf_holding_tcs.html"),
    ("/equity/insider-trading-sast/all/", "insider_trading_tcs.html"),
    ("/equity/corporate-actions/", "corporate_actions_motherson.html"),
    ("/equity/bulk-block-deals/", "bulk_deals_motherson.html"),
    ("/equity/share-holding/", "shareholding_tcs.html"),
    ("/equity/board-meeting/", "board_meeting_tcs.html"),
    ("/equity/swot-buy-or-sell/", "swot_tcs.html"),
    ("/equity/api/stock/adv-technical-analysis/", "adv_technical_analysis_tcs.json"),
    ("/equity/overview-second-part/", "overview_second_part_tcs.json"),
    ("/share-price/price-performance-analysis/", "price_performance_tcs.json"),
    ("/research-reports/stock/", "research_reports_tcs.html"),
    ("/latest-news/", "latest_news_tcs.html"),
    ("/alerts/stock-alerts/", "stock_alerts_tcs.html"),
    ("/tools/buy-sell-zone/", "buy_sell_zone_tcs.html"),
    ("/equity-sitemap-stocks.xml", "sitemap_excerpt.xml"),
)

#: One method per JSON route, so the envelope refusal is exercised on the route it belongs to
#: rather than three methods sharing a single injected needle.
ENVELOPE_ROUTE_METHODS: Dict[str, str] = {
    "/equity/api/stock/adv-technical-analysis/": "adv_technical_analysis",
    "/equity/overview-second-part/": "events",
    "/share-price/price-performance-analysis/": "returns_comparison",
}

#: The three shapes a WAF challenge arrives in. The third matters: WAF attaches the header
#: independently of the status code, and a challenge served as a 200 is a captcha page, not data.
WAF_FORMS: Dict[str, Any] = {
    "405 without the header": lambda: httpx.Response(405, text="Method Not Allowed"),
    "the header on a 403": lambda: httpx.Response(
        403, text="Forbidden", headers={"x-amzn-waf-action": "captcha"}
    ),
    "the header on a 200": lambda: httpx.Response(
        200, text="<html>captcha</html>", headers={"x-amzn-waf-action": "challenge"}
    ),
}

#: Every section method, the endpoint template it must build, and the kwargs ``build_path`` needs.
#: This is the request-count contract in one table: one entry per method, one URL each.
SECTION_METHODS: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "corporate_actions": ("corporate_actions_page", {}),
    "bulk_block_deals": ("bulk_block_deals_page", {}),
    "share_holding": ("share_holding_page", {"period": tl.SNAPSHOT_PERIOD}),
    "share_holding_holders": ("share_holding_page", {"period": tl.SNAPSHOT_PERIOD}),
    "board_meetings": ("board_meeting_page", {}),
    "research_reports": ("research_reports_page", {}),
    "latest_news": ("latest_news_page", {}),
    "stock_alerts": ("stock_alerts_page", {}),
    "insider_trading": ("insider_trading_page", {}),
    "monthly_mf_holdings": ("monthly_mf_holding_page", {"period": tl.SNAPSHOT_PERIOD}),
    "buy_sell_zone": ("buy_sell_zone_page", {}),
    "swot": ("swot_page", {}),
    "adv_technical_analysis": ("adv_technical_analysis", {}),
    "technical_signals": ("adv_technical_analysis", {}),
    "overview_second_part": ("overview_second_part", {}),
    "company_overview": ("overview_second_part", {}),
    "events": ("overview_second_part", {}),
    "price_performance": ("price_performance_analysis", {}),
    "returns_comparison": ("price_performance_analysis", {}),
}


class Recorder:
    """A ``MockTransport`` handler that answers by route substring and logs every URL.

    ``overrides`` is consulted first and may hold a response **or** a callable taking the request,
    which is how the WAF and envelope tests inject a failure for one route without touching the
    rest. An unrecognised route answers **404**, never an empty 200: a method that quietly requests
    a route nobody registered fails loudly here instead of "succeeding" on a body it was not meant
    to parse.
    """

    def __init__(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[str] = []
        self.overrides = dict(overrides or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for needle, response in self.overrides.items():
            if needle in url:
                return response(request) if callable(response) else response
        for needle, fixture in ROUTES:
            if needle in url:
                if fixture.endswith(".json"):
                    return httpx.Response(200, json=payload(fixture))
                return httpx.Response(200, text=text(fixture))
        return httpx.Response(404, text=f"no fixture is registered for {url}")

    @property
    def paths(self) -> List[str]:
        """Every requested URL with the host stripped, for comparison against ``build_path``."""
        return [url.replace(tl.BASE_URL, "") for url in self.calls]


def client_for(recorder: Recorder, **kwargs) -> tl.TrendlyneClient:
    """A mock-backed client with the sitemap index already warm.

    Warm, because the index is cached in memory and a cold one would add a request that has nothing
    to do with the section route under test. :func:`TestRequestCounts` measures the cold case
    separately.
    """
    client = mock_client(recorder, **kwargs)
    client.load_symbol_index(text("sitemap_excerpt.xml"))
    return client


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client(recorder: Recorder) -> tl.TrendlyneClient:
    return client_for(recorder)


def path_of(method: str) -> str:
    """The path the client must build for one method, straight from the template.

    Built from :data:`SECTION_METHODS` rather than written out, so a template change shows up as a
    failure here instead of as a URL both the code and the test agree on by accident.
    """
    route, kwargs = SECTION_METHODS[method]
    return tl.build_path(route, RECORD, **kwargs)


# =========================================================== request counts


class TestRequestCounts:
    """One request per section call, and it is the URL the template says it is.

    This is the whole reason the section methods resolve through the sitemap and never fetch the
    stock page: the record already carries the id, the symbol and the slug, and the site's WAF
    budget is per session.
    """

    @pytest.mark.parametrize("method", sorted(SECTION_METHODS))
    def test_one_call_is_exactly_one_request_for_the_template(self, method, recorder, client):
        getattr(client, method)(SYMBOL)
        assert recorder.paths == [path_of(method)], (
            f"{method}() made {len(recorder.calls)} request(s): {recorder.paths}; the contract is "
            f"exactly one, and it is {path_of(method)}"
        )

    def test_a_cold_index_costs_one_more_request_and_no_stock_page(self, recorder):
        """The honest cold case: the sitemap, then the section. Never a stock page or a token."""
        client = mock_client(recorder)          # deliberately NOT index-warm
        client.bulk_block_deals(SYMBOL)
        assert recorder.paths == [
            tl.SITEMAP_PATH,
            path_of("bulk_block_deals"),
        ]
        assert not any("/equity/878/" in p for p in recorder.paths), (
            "a section route must not spend a request on the stock page: it needs nothing from it "
            "that the sitemap index does not already hold"
        )

    def test_the_three_helpers_share_one_route_and_still_cost_one_request(self, recorder, client):
        """The shaped views are not extra fetches: one request each, on their parent route."""
        for method in ("technical_signals", "company_overview", "events"):
            recorder.calls.clear()
            getattr(client, method)(SYMBOL)
            assert len(recorder.calls) == 1, method


#: A symbol the recorded sitemap excerpt does **not** carry. NIFTYBEES is the measured example —
#: ``tests/test_trendlyne_core.py`` and ``test_trendlyne_fuzzy_search.py`` both pin it as resolving
#: only through the member route, and it is the ETF case this whole class is about.
SITEMAP_ABSENT = "NIFTYBEES"

#: What ``/member/api/ac_snames/`` answers for it, in the four-field shape
#: :meth:`TrendlyneClient.search_remote_async` reads. ``k`` is the stock id, ``slugname`` the URL
#: segment; the resolver then insists the hit IS the symbol asked for, so ``id`` matches exactly.
MEMBER_HIT = [
    {
        "k": 1588,
        "id": "NIFTYBEES",
        "value": "NIFTYBEES",
        "slugname": "nippon-india-etf-nifty-50-bees",
    }
]


class TestTheShapedViewsResolveOnceNotTwice:
    """A shaped view resolves its symbol **once**, even when resolving is the expensive case.

    ``technical_signals`` / ``events`` / ``company_overview`` / ``returns_comparison`` each resolved
    the symbol and then called their parent, which resolved it again. For a symbol the sitemap
    carries, the second resolve is a cache hit and costs nothing -- which is exactly why
    :class:`TestRequestCounts` above, running on MOTHERSON, never saw it. For a symbol the sitemap
    **omits** -- every ETF -- the second resolve re-hits ``/member/api/ac_snames/``, the one route
    this package's robots gate exists for.

    So these tests are **cold for the member path**: the symbol is one the sitemap fixture does not
    carry, which is the only way to see the second request at all.
    """

    @staticmethod
    def _member_capable(recorder: Recorder):
        """``recorder``'s handler plus the one route it does not register: ``/member/``.

        Wrapping rather than mutating: the Recorder is the fixture registry the rest of this file
        depends on, and a member route added to :data:`ROUTES` would change what every other test
        here can reach.
        """
        original = recorder.__call__

        def handler(request: httpx.Request) -> httpx.Response:
            if "/member/api/ac_snames/" in str(request.url):
                recorder.calls.append(str(request.url))
                return httpx.Response(200, json=MEMBER_HIT)
            return original(request)

        return handler

    @staticmethod
    def _client(recorder: Recorder) -> tl.TrendlyneClient:
        """A client with the sitemap warm (MOTHERSON's) and the member route answerable."""
        client = mock_client(TestTheShapedViewsResolveOnceNotTwice._member_capable(recorder))
        client.load_symbol_index(text("sitemap_excerpt.xml"))
        return client

    @pytest.mark.parametrize(
        "shaped", ["technical_signals", "events", "company_overview", "returns_comparison"]
    )
    def test_a_shaped_view_costs_one_member_request_not_two(self, recorder, shaped):
        """The measurement the fix is about, on a symbol only the member route can resolve.

        Two requests total: the member search and the section route. Before the fix this was three,
        with two on ``/member/`` -- the assertion that matters is ``member == 1``, because the total
        alone would still have been "one section plus some resolution".
        """
        client = self._client(recorder)

        getattr(client, shaped)(SITEMAP_ABSENT)

        member = [p for p in recorder.paths if "/member/api/ac_snames/" in p]
        assert len(member) == 1, (
            f"{shaped}() re-resolved a sitemap-absent symbol: {len(member)} member request(s) in "
            f"{recorder.paths}. /member/ is the robots-gated route; a shaped view must not spend "
            f"it twice."
        )
        assert len(recorder.paths) == 2, recorder.paths
        assert client.symbol_resolution == "member_search"

    def test_the_parent_route_alone_still_resolves_and_costs_one_member_request(self, recorder):
        """The control: the parent really does resolve, and really does reach ``/member/`` once.

        Without it, the test above could be green for the wrong reason -- a client that never
        resolved anything would also make zero member requests.
        """
        client = self._client(recorder)

        client.adv_technical_analysis(SITEMAP_ABSENT)

        member = [p for p in recorder.paths if "/member/api/ac_snames/" in p]
        assert len(member) == 1, recorder.paths
        assert len(recorder.paths) == 2, recorder.paths

    def test_a_sitemap_symbol_is_unaffected_because_the_second_resolve_was_always_free(self, recorder):
        """MOTHERSON *is* in the recorded sitemap, so its second resolve was a cache hit.

        This is the reason the defect survived the suite: the existing request-count assertions are
        all warm-index, sitemap-symbol calls. Asserted so a future "optimisation" that removes the
        second resolve by breaking the sitemap path would fail here rather than pass quietly.
        """
        client = self._client(recorder)

        client.technical_signals(SYMBOL)

        assert recorder.paths == [path_of("technical_signals")], recorder.paths
        assert client.symbol_resolution == "sitemap"

    def test_two_shaped_calls_cost_two_member_requests_so_the_counter_is_real(self, recorder):
        """The other control: the assertion above is not measuring a client that cannot be measured.

        Two separate calls resolve twice -- once each -- so a ``member == 1`` claim on the first call
        is a claim about *that call*, not an artefact of the recorder.
        """
        client = self._client(recorder)

        client.technical_signals(SITEMAP_ABSENT)
        client.technical_signals(SITEMAP_ABSENT)

        member = [p for p in recorder.paths if "/member/api/ac_snames/" in p]
        assert len(member) == 2, recorder.paths

    @pytest.mark.parametrize(
        "method",
        [
            "technical_signals", "events", "company_overview", "returns_comparison",
            "adv_technical_analysis", "overview_second_part", "price_performance",
        ],
    )
    def test_the_public_call_shape_is_unchanged(self, method):
        """The fix added a **keyword-only, private, default-``None``** parameter to the three
        parents the shaped views call. A caller holding one of these methods must not be able to
        tell: the sync twins take exactly one argument, and the async parents take one positional
        plus an internal they never have to pass.

        Asserted rather than left to review, because "we only added a private keyword" is exactly
        the kind of change that later grows a second one.
        """
        public = inspect.signature(getattr(tl.TrendlyneClient, method))
        kinds = [p.kind for p in public.parameters.values()][1:]
        assert all(k is inspect.Parameter.POSITIONAL_OR_KEYWORD for k in kinds), (
            f"{method}{public} grew a keyword parameter; the public call shape is the contract"
        )


class TestTheRequestCountIsPublished:
    """A section frame says how many requests it cost, rather than claiming it cost one.

    ``_section_note`` hard-coded "fetched in one request". That is true for a warm index and false
    three times over for a cold index on a symbol the sitemap omits, and two of those three go to
    the robots-gated member route -- so it was a claim a caller counting their WAF budget could not
    check.
    """

    def test_a_warm_index_call_says_one_request(self, client):
        frame = client.bulk_block_deals(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_requests"] == 1
        assert "fetched in one request" in frame.attrs["bfinance_trendlyne_note"]

    def test_a_cold_index_on_a_sitemap_absent_symbol_says_three(self, recorder):
        """The claim that was false. Cold index + member fallback + the section page is three."""
        client = mock_client(
            TestTheShapedViewsResolveOnceNotTwice._member_capable(recorder)
        )   # deliberately NOT index-warm

        frame = client.bulk_block_deals(SITEMAP_ABSENT)

        assert frame.attrs["bfinance_trendlyne_requests"] == 3
        assert "3 requests (2 of them resolving the symbol)" in frame.attrs[
            "bfinance_trendlyne_note"
        ], frame.attrs["bfinance_trendlyne_note"]
        assert "fetched in one request" not in frame.attrs["bfinance_trendlyne_note"]
        assert len([p for p in recorder.paths if "/member/api/ac_snames/" in p]) == 1

    def test_a_cold_index_on_a_sitemap_symbol_says_two(self, recorder):
        """The middle case, so "three" above is a measurement and not the only other answer: the
        sitemap fetch plus the section, with no member request at all."""
        client = mock_client(recorder)          # deliberately NOT index-warm

        frame = client.bulk_block_deals(SYMBOL)

        assert frame.attrs["bfinance_trendlyne_requests"] == 2
        assert "2 requests (1 of them resolving the symbol)" in frame.attrs[
            "bfinance_trendlyne_note"
        ], frame.attrs["bfinance_trendlyne_note"]

    def test_the_client_counts_every_attempt_it_made(self, recorder):
        """The counter behind the attr, on its own: a retry is a second request the vendor was
        asked for, so it counts as one."""
        client = client_for(recorder)
        assert client.request_count == 0

        client.bulk_block_deals(SYMBOL)
        assert client.request_count == 1

        recorder.overrides["/equity/bulk-block-deals/"] = httpx.Response(500, text="down")
        with pytest.raises(TrendlyneServerError):
            client.bulk_block_deals(SYMBOL)
        assert client.request_count == 3, "one more call, two attempts on a 5xx"


# =========================================================== corporate actions


#: A section table that renders a header and **no rows** — the shape TCS's own corporate-events
#: JSON carries for a split and a rights issue it has never had (``splitTableHeader`` with
#: ``splitTableData: []``, recorded 2026-10-10). A header row with nothing under it still
#: classifies, so it still has a section name; the name has to come from the signature.
HEADER_ONLY_BONUS = (
    "<table><thead><tr>"
    + "".join(f"<th>{cell}</th>" for cell in ("Ex-Date", "Bonus Ratio", "Record Date"))
    + "</tr></thead><tbody></tbody></table>"
)


def _section_page(*tables: str) -> str:
    """A corporate-actions page holding exactly the tables given, in order."""
    return f"<html><body>{''.join(tables)}</body></html>"


class TestCorporateActions:
    def test_three_keys_in_page_order_and_nothing_dropped(self, client):
        result = client.corporate_actions(SYMBOL)
        assert list(result) == ["bonus", "dividend", "other"]
        # MOTHERSON's page carries FIVE tables; the three-key contract must not lose the other two.
        assert len(tbt.page_signatures(text("corporate_actions_motherson.html"))) == 5
        assert set(result["other"]["bfinance_trendlyne_section"]) == {
            "split", "board_meeting", "rights",
        }
        assert result["other"].attrs["bfinance_trendlyne_rows"] == len(result["other"])

    def test_each_frame_declares_itself(self, client):
        for key, frame in client.corporate_actions(SYMBOL).items():
            assert frame.attrs["bfinance_trendlyne_table"] == key
            assert frame.attrs["bfinance_provenance"] == "trendlyne"
            assert frame.attrs["bfinance_symbol"] == SYMBOL

    def test_the_bonus_and_dividend_frames_are_the_vendor_tables(self, client):
        result = client.corporate_actions(SYMBOL)
        assert {"Ex-Date", "Bonus Ratio", "Record Date"} <= set(result["bonus"].columns)
        assert {"Dividend Amount", "Dividend Type"} <= set(result["dividend"].columns)

    def test_a_page_with_no_bonus_table_still_has_all_three_keys(self, recorder):
        """An absent section is an empty frame, not a missing key: a caller iterating the three
        names must be able to tell "this symbol had no bonus" from "the parser lost the table"."""
        stripped = "<table><tr><th>Ex-Date</th><th>Dividend Amount</th>" \
                   "<th>Dividend Type</th></tr><tr><td>2026-10-14</td><td>12</td>" \
                   "<td>INTERIM</td></tr></table>"
        recorder.overrides["/equity/corporate-actions/"] = httpx.Response(200, text=stripped)
        result = client_for(recorder).corporate_actions(SYMBOL)
        assert list(result) == ["bonus", "dividend", "other"]
        assert result["bonus"].empty
        assert result["dividend"].shape[0] == 1
        assert result["bonus"].attrs["bfinance_trendlyne_sections"] == []

    # --------------------------------------------------- the header-only table (MAJOR 1)

    def test_a_section_table_with_a_header_and_no_rows_is_an_empty_frame_with_its_name(
        self, recorder
    ):
        """The case the test above could not reach: the table is **present**, with its header
        rendered and no rows under it.

        ``find_tables`` returns a zero-row frame with columns for such a table, so reading the
        section name back off a row raised ``IndexError: single positional indexer is
        out-of-bounds`` -- a bare Python error, not a ``BFinanceError``, so
        ``Ticker(..., trendlyne_sections=True).trendlyne_*`` callers got it unhandled instead of
        an empty frame and a ``data_warnings`` entry.

        Driven through the **public** method on purpose: a parser-only test would pass even if the
        crash lived one layer up, which is where this was actually reachable.
        """
        dividend = (
            "<table><thead><tr>"
            + "".join(
                f"<th>{cell}</th>"
                for cell in ("Ex-Date", "Dividend Amount", "Dividend Type", "Record Date")
            )
            + "</tr></thead><tbody><tr><td>2026-10-14</td><td>12</td><td>INTERIM</td>"
              "<td>2026-10-13</td></tr></tbody></table>"
        )
        recorder.overrides["/equity/corporate-actions/"] = httpx.Response(
            200, text=_section_page(HEADER_ONLY_BONUS, dividend)
        )

        result = client_for(recorder).corporate_actions(SYMBOL)

        assert list(result) == ["bonus", "dividend", "other"]
        # The empty section keeps its vendor columns, and names itself from the signature.
        assert result["bonus"].empty
        assert list(result["bonus"].columns)[:3] == ["Ex-Date", "Bonus Ratio", "Record Date"]
        assert result["bonus"].attrs["bfinance_trendlyne_sections"] == ["bonus"]
        assert result["bonus"].attrs["bfinance_trendlyne_rows"] == 0
        # And the populated sibling is untouched by the empty one next to it.
        assert result["dividend"].shape[0] == 1
        assert result["dividend"].attrs["bfinance_trendlyne_sections"] == ["dividend"]
        assert result["other"].attrs["bfinance_trendlyne_sections"] == []

    def test_a_header_only_table_with_no_sibling_is_still_all_three_keys(self, recorder):
        """The single-table form, which is the one a symbol with no actions at all produces.

        Separate from the test above because a concat of two parts and a concat of one can fail
        differently, and because "every key present and every frame empty" is the honest answer
        this page shape demands.
        """
        recorder.overrides["/equity/corporate-actions/"] = httpx.Response(
            200, text=_section_page(HEADER_ONLY_BONUS)
        )
        result = client_for(recorder).corporate_actions(SYMBOL)

        assert list(result) == ["bonus", "dividend", "other"]
        assert result["bonus"].empty
        assert result["dividend"].empty
        assert result["other"].empty
        assert result["bonus"].attrs["bfinance_trendlyne_sections"] == ["bonus"]

    def test_control_a_header_only_table_would_raise_before_the_fix(self):
        """The control on the two tests above, and it is what makes them mean anything.

        Reproduces the exact expression that was there — the section name read off a row of the
        tagged frame — and asserts that it raises on a zero-row frame while the same expression on
        a populated one does not. Without this, a parser that silently published the wrong section
        name would still pass everything above.
        """
        page = _section_page(HEADER_ONLY_BONUS)
        frames = tbt.find_tables(page)
        assert frames, "the fixture page must really contain a table"
        assert frames[0].empty, "and it must really be header-only"

        tagged = frames[0].copy()
        tagged[tl._SECTION_COLUMN] = "bonus"
        with pytest.raises(IndexError):
            str(tagged[tl._SECTION_COLUMN].iloc[0])

        # The same read on a frame that has rows is what the old code was written for.
        populated = tbt.find_tables(_section_page(
            "<table><thead><tr><th>Ex-Date</th><th>Bonus Ratio</th><th>Record Date</th></tr>"
            "</thead><tbody><tr><td>2026-10-14</td><td>1:1</td><td>2026-10-13</td></tr>"
            "</tbody></table>"
        ))[0].copy()
        populated[tl._SECTION_COLUMN] = "bonus"
        assert str(populated[tl._SECTION_COLUMN].iloc[0]) == "bonus"


# =========================================================== deals, meetings, reports


class TestBulkBlockDeals:
    def test_the_deals_frame_and_its_provenance(self, client):
        frame = client.bulk_block_deals(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_table"] == "deals"
        assert frame.attrs["bfinance_trendlyne_rows"] == len(frame)
        assert {"Client Name", "Deal Type", "Quantity"} <= set(frame.columns)

    def test_the_insider_header_must_not_satisfy_the_deals_signature(self, client):
        """The trap the registry comment names: no ``Deal Type`` cell on the insider page.

        If the two ever matched each other, ``insider_trading`` would return a bulk-deal frame with
        no error — a different table's numbers under a confident name.
        """
        insider = client.insider_trading(SYMBOL)
        assert insider.attrs["bfinance_trendlyne_table"] == "insider_trade"
        assert "Deal Type" not in insider.columns
        assert tbt.classify_table(list(insider.columns)) != "deals"


class TestBoardMeetings:
    def test_the_ex_date_purpose_notes_frame(self, client):
        frame = client.board_meetings(SYMBOL)
        assert list(frame.columns) == ["Ex-date", "Purpose", "Notes"]
        assert frame.attrs["bfinance_trendlyne_table"] == "board_meeting"
        assert len(frame) == 95, (
            "95 data rows on the recorded TCS capture (96 <tr> including the header)"
        )

    def test_rows_are_not_re_sorted_behind_the_callers_back(self, client):
        """The page leads with a forward-dated meeting; sorting it here would be a preference."""
        frame = client.board_meetings(SYMBOL)
        assert str(frame.iloc[0]["Ex-date"]).startswith("08 Oct 2026")


class TestResearchReports:
    def test_the_report_frame_carries_the_aggregate_row(self, client):
        frame = client.research_reports(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_table"] == "research_report"
        assert "Target" in frame.columns and "Upside(%)" in frame.columns
        assert len(frame) == 21
        first = frame.iloc[0]
        assert first["Author"] == "Consensus Share Price Target", (
            "the vendor's aggregate consensus row is kept, not filtered; a consumer averaging the "
            "Target column has to exclude it deliberately"
        )
        assert float(first["Target"]) == 2445.54 and float(first["Upside(%)"]) == 13.43
        # Its own 'Summary' cell is hidden on the page, so the row's identity lives in 'Author'.
        assert str(first["Summary"]).strip() == ""

    def test_page_two_builds_the_query_string(self, recorder, client):
        client.research_reports(SYMBOL, page=2)
        assert recorder.paths == [f"{path_of('research_reports')}?page=2"]

    @pytest.mark.parametrize("page", [0, -1, "two", None])
    def test_a_bad_page_costs_no_request(self, page, recorder, client):
        with pytest.raises(ValueError, match="page"):
            client.research_reports(SYMBOL, page=page)
        assert recorder.calls == []


# =========================================================== news and alerts (no tables)


class TestLatestNews:
    def test_the_five_columns_verified_off_the_fixture(self, client):
        frame = client.latest_news(SYMBOL)
        assert list(frame.columns) == ["date", "kind", "title", "url", "source"]
        assert len(frame) == 10, "the recorded page carries 10 post cards"

    def test_the_first_card_is_the_one_the_page_leads_with(self, client):
        frame = client.latest_news(SYMBOL)
        row = frame.iloc[0]
        assert row["source"] == "Economic Times"
        assert row["title"] == "IT stocks surge despite US immigration curbs"
        assert row["url"].startswith("https://economictimes.indiatimes.com/")

    def test_the_date_is_the_vendor_text_not_a_guess(self, client):
        """Three date formats live on this page; normalising one of them would be a guess."""
        frame = client.latest_news(SYMBOL)
        assert str(frame.iloc[0]["date"]) == "10 Oct, 2026 at 09:49 AM"
        assert str(frame.iloc[2]["date"]) == "9 Oct, 2026 at 06:35 PM"

    def test_a_paywalled_url_is_reported_not_rewritten(self, client):
        """Research-report cards link a login modal for anonymous readers: the real answer."""
        frame = client.latest_news(SYMBOL)
        assert any("loginmodal" in str(url) for url in frame["url"]), (
            "the paywall href is the vendor's own; rewriting it to the page behind the wall would "
            "invent an address the response never carried"
        )

    def test_a_trendlyne_post_is_linked_by_its_own_posts_url(self, client):
        """The chrome ``/posts/alert-modal/…`` link must not win over the item's own page."""
        frame = client.latest_news(SYMBOL)
        earnings = frame[frame["title"].str.contains("Earnings Call", na=False)]
        assert len(earnings) == 1
        assert "/posts/5888200/" in earnings.iloc[0]["url"]

    def test_page_two_builds_the_query_string(self, recorder, client):
        client.latest_news(SYMBOL, page=2)
        assert recorder.paths == [f"{path_of('latest_news')}?page=2"]

    def test_page_one_is_the_bare_page(self, recorder, client):
        client.latest_news(SYMBOL)
        assert "?page=" not in recorder.paths[0], (
            "the first page is the page's own canonical URL; sending ?page=1 would be shaping a "
            "request for no reason"
        )

    def test_a_page_with_no_cards_is_an_empty_frame_not_an_error(self, recorder):
        recorder.overrides["/latest-news/"] = httpx.Response(200, text="<html><body></body></html>")
        frame = client_for(recorder).latest_news(SYMBOL)
        assert frame.empty
        assert list(frame.columns) == ["date", "kind", "title", "url", "source"]
        assert frame.attrs["bfinance_trendlyne_rows"] == 0


class TestStockAlerts:
    def test_the_five_columns_verified_off_the_fixture(self, client):
        frame = client.stock_alerts(SYMBOL)
        assert list(frame.columns) == ["date", "kind", "headline", "detail", "values"]
        assert len(frame) == 10

    def test_the_values_dict_is_the_cards_own_labelled_tiles(self, client):
        frame = client.stock_alerts(SYMBOL)
        assert frame.iloc[0]["values"] == {"Recommendation": "Buy", "Target Price": "2400.0"}
        assert frame.iloc[1]["values"] == {
            "Day Price": "2399.3", "SMA 30": "2343.86", "Crossover": "ABOVE",
        }

    def test_the_kind_is_the_cards_own_pill_row(self, client):
        frame = client.stock_alerts(SYMBOL)
        assert frame.iloc[0]["kind"] == "RESEARCH REPORT"
        assert frame.iloc[1]["kind"] == "SMA / SMA 30 / Crossover"

    def test_the_detail_is_the_alert_wording_not_the_card_title(self, client):
        frame = client.stock_alerts(SYMBOL)
        # The card title renders the page's own symbol, which is TCS in these bytes -- see the
        # module docstring on the MOTHERSON-id / TCS-body substitution.
        assert frame["headline"].eq("TCS").all()
        assert "crossed above 30 Day SMA" in str(frame.iloc[1]["detail"])

    def test_a_repeated_label_keeps_every_value(self):
        """The dict must not let the last value silently win a repeated label."""
        html = (
            "<div class='tr-alert-body'>"
            "<ul class='nav-alert-full'><li>TEST</li></ul>"
            "<div class='gray555 fs09rem'>1 Jan 2026</div><h5 class='card-title'>X</h5>"
            "<ul class='list-group'>"
            "<li class='list-group-item'>the detail</li>"
            "<li class='list-group-item'><h3>1</h3><p>Day Price</p></li>"
            "<li class='list-group-item'><h3>2</h3><p>Day Price</p></li>"
            "</ul></div>"
        )
        frame = tl.parse_stock_alerts(f"<div>{html}</div>")
        assert frame.iloc[0]["values"] == {"Day Price": ["1", "2"]}


# =========================================================== insider row cap


def insider_page(rows: int) -> str:
    """An insider-trading page with exactly ``rows`` data rows, header byte-identical to the
    recorded one so the registry signature classifies it."""
    header = (
        "Client Name", "Client Category", "Action*", "Reported To/By Exchange", "Quantity",
        "Post Transaction Holding", "Traded %", "Avg. Price", "Value", "Period",
        "Regulation (Insider/SAST)", "Security Type", "Mode",
    )
    body = "".join(
        "<tr>" + "".join(f"<td>c{i}</td>" for _ in header) + "</tr>" for i in range(rows)
    )
    return (
        "<table><thead><tr>"
        + "".join(f"<th>{cell}</th>" for cell in header)
        + "</tr></thead><tbody>" + body + "</tbody></table>"
    )


class TestInsiderRowCap:
    def test_the_recorded_page_is_below_the_cap_and_says_so(self, client):
        frame = client.insider_trading(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_insider_rows"] == len(frame) == 174
        assert frame.attrs["bfinance_trendlyne_insider_rows_capped"] is False
        assert "TRUNCATED" not in frame.attrs["bfinance_trendlyne_note"]

    def test_below_the_cap_never_claims_to_be_complete(self, client):
        """The failure this guard exists to prevent, in the direction that is easy to get wrong."""
        note = client.insider_trading(SYMBOL).attrs["bfinance_trendlyne_note"]
        assert "does NOT mean it is complete" in note

    def test_a_frame_at_the_cap_is_labelled_truncated(self, recorder):
        recorder.overrides["/equity/insider-trading-sast/all/"] = httpx.Response(
            200, text=insider_page(tl.INSIDER_ROW_CAP)
        )
        frame = client_for(recorder).insider_trading(SYMBOL)
        assert len(frame) == tl.INSIDER_ROW_CAP
        assert frame.attrs["bfinance_trendlyne_insider_rows_capped"] is True
        note = frame.attrs["bfinance_trendlyne_note"]
        assert "TRUNCATED" in note and "NOT the full history" in note

    def test_one_row_short_of_the_cap_is_not_called_capped(self, recorder):
        """The off-by-one control: a cap guard that fires early is as wrong as one that never
        fires, and 499 rows is a real page, not a truncated one."""
        recorder.overrides["/equity/insider-trading-sast/all/"] = httpx.Response(
            200, text=insider_page(tl.INSIDER_ROW_CAP - 1)
        )
        frame = client_for(recorder).insider_trading(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_insider_rows_capped"] is False
        assert "TRUNCATED" not in frame.attrs["bfinance_trendlyne_note"]


# =========================================================== holdings, and the one guard


class TestShareHoldingIsSnapshotOnly:
    def test_the_summary_grid_carries_every_quarter_the_page_holds(self, client):
        frame = client.share_holding(SYMBOL)
        assert list(frame.columns)[0] == "Row Type", (
            "the marker column, then a label column plus twelve quarters"
        )
        assert len(frame.columns) == 13
        assert frame.attrs["bfinance_trendlyne_snapshot_only"] is True

    def test_the_client_frame_carries_the_rows_the_page_hides(self, client):
        """The extractor reads this one table with ``displayed_only=False``, so the collapsed
        sub-rows reach the caller through the client too. ``Pledged`` is the one that matters: it
        is a distinct holding figure, not a restatement of the promoter's own."""
        frame = client.share_holding(SYMBOL)
        assert "Pledged" in frame.index
        assert "Locked" in frame.index
        assert "Mutual Funds" in frame.index
        assert frame.loc["Pledged", "Row Type"] == "subgroup"
        assert frame.loc["Promoter", "Row Type"] == "category"

    def test_a_non_latest_period_is_refused_before_any_request(self, recorder, client):
        for period in ("2026", "Jun-2026", "2024"):
            with pytest.raises(TrendlyneServerError) as excinfo:
                client.share_holding(SYMBOL, period=period)
            assert "latest" in str(excinfo.value)
            assert "HTTP 500" in str(excinfo.value)
        assert recorder.calls == [], (
            "the 500 is deterministic and measured; spending a request (or three, with the retry) "
            "to be told it is the thing this package exists not to do"
        )

    def test_the_holders_method_carries_the_same_guard(self, recorder, client):
        # The holders method takes no period at all: build_path would accept one, the method
        # refuses to offer it. Asserted so a future period= argument has to be a decision, and so
        # the guard above cannot quietly be given a way in.
        with pytest.raises(TypeError):
            client.share_holding_holders(SYMBOL, period="2026")     # type: ignore[call-arg]
        assert recorder.calls == []
        # And the method itself costs one request, like every other section call.
        client.share_holding_holders(SYMBOL)
        assert recorder.paths == [path_of("share_holding_holders")]

    def test_a_malformed_period_is_a_value_error_not_a_server_call(self, recorder, client):
        with pytest.raises(ValueError, match="period"):
            client.share_holding(SYMBOL, period="jun-2026")
        assert recorder.calls == []

    def test_the_three_holder_tables_carry_their_own_category(self, client):
        holders = client.share_holding_holders(SYMBOL)
        assert [h.attrs["bfinance_trendlyne_holder_category"] for h in holders] == [
            "Promoter Holding", "Institutional Holding", "Public (Non-Institutional) Holding",
        ]
        for holder in holders:
            assert holder.attrs["bfinance_trendlyne_snapshot_only"] is True


class TestTheMutualFundRouteIsNotSnapshotOnly:
    """The measured counter-example, and the reason the guard above is scoped to one route.

    ``Jun-2026`` and ``2026`` were both measured answering **200 with real data** on this route
    (2026-10-10, TCS) while the share-holding route answered 500 for the same period forms. A
    blanket snapshot guard would have broken the one route where the segment means something.
    """

    @pytest.mark.parametrize("period", ["Jun-2026", "2026", "2024"])
    def test_a_non_latest_period_is_forwarded_and_answered(self, period, recorder, client):
        frame = client.monthly_mf_holdings(SYMBOL, period=period)
        assert recorder.paths == [
            f"/equity/monthly-mutual-fund-share-holding/{RECORD.stock_id}/{SYMBOL}/{period}/"
            f"{RECORD.slug}/"
        ]
        assert not frame.empty
        assert frame.attrs["bfinance_trendlyne_period"] == period

    def test_the_ragged_month_columns_are_not_coerced(self, client):
        """``-`` means the fund held nothing that month; turning it into NaN makes a real value
        indistinguishable from a missing measurement."""
        frame = client.monthly_mf_holdings(SYMBOL)
        assert len(frame.columns) == 10
        assert any((frame[column].astype(str) == "-").any() for column in frame.columns[1:])

    def test_a_malformed_period_still_costs_nothing(self, recorder, client):
        with pytest.raises(ValueError, match="period"):
            client.monthly_mf_holdings(SYMBOL, period="Jun-26")
        assert recorder.calls == []

    def test_the_same_period_that_share_holding_refuses_is_forwarded_here(self, recorder):
        """Both halves in one test, so neither guard can be moved without the other moving too."""
        recorder.overrides["/equity/monthly-mutual-fund-share-holding/"] = httpx.Response(
            200, text=text("monthly_mf_holding_tcs.html")
        )
        client = client_for(recorder)
        assert not client.monthly_mf_holdings(SYMBOL, period="Jun-2026").empty
        with pytest.raises(TrendlyneServerError):
            client.share_holding(SYMBOL, period="Jun-2026")
        assert len(recorder.calls) == 1, "one MF request and no share-holding request"


# =========================================================== buy-sell-zone and swot


class TestBuySellZone:
    def test_both_bases_and_they_are_not_interchangeable(self, client):
        result = client.buy_sell_zone(SYMBOL)
        assert set(result) == {"standalone", "consolidated"}
        # Byte-identical headers, different numbers: this is the only thing that catches a swap.
        assert list(result["standalone"].columns) == list(result["consolidated"].columns)
        assert result["standalone"].attrs["bfinance_trendlyne_current_pe"] == 14.9
        assert result["consolidated"].attrs["bfinance_trendlyne_current_pe"] == 15.1
        assert result["standalone"].attrs["bfinance_trendlyne_strong_buy_zone_pct"] == 1.0
        assert result["consolidated"].attrs["bfinance_trendlyne_strong_buy_zone_pct"] == 1.8

    def test_the_raw_tile_text_is_published_too(self, client):
        """The numeric attrs are derived; the vendor's own rendering must survive them."""
        tiles = client.buy_sell_zone(SYMBOL)["standalone"].attrs[
            "bfinance_trendlyne_buy_sell_tiles"
        ]
        assert tiles["strong buy zone"] == "1.0%"
        assert tiles["forward eps"] == "154.4"


class TestSwot:
    def test_five_tables_keyed_by_their_tab_pane(self, client):
        result = client.swot(SYMBOL)
        assert list(result) == [
            "tag_strengths", "tag_weakness", "tag_opportunity", "tag_threats", "tag_others",
        ]
        assert [len(frame) for frame in result.values()] == [19, 6, 3, 3, 3]

    def test_each_frame_publishes_the_free_text_heading_it_was_not_keyed_on(self, client):
        result = client.swot(SYMBOL)
        assert (
            result["tag_strengths"].attrs["bfinance_trendlyne_swot_heading"]
            == "Companies with no Debt"
        )

    def test_a_page_with_no_fact_tables_raises_rather_than_returning_nothing(self, recorder):
        recorder.overrides["/equity/swot-buy-or-sell/"] = httpx.Response(
            200, text="<html><body><div>nothing here</div></body></html>"
        )
        with pytest.raises(tbt.TrendlyneTableNotFoundError):
            client_for(recorder).swot(SYMBOL)


# =========================================================== the JSON siblings


class TestJsonRoutes:
    def test_every_json_template_carries_format_json(self):
        """The query is in the template, so no call site can forget it."""
        for name in ("adv_technical_analysis", "overview_second_part",
                     "price_performance_analysis"):
            assert tl.ENDPOINTS[name].endswith("?format=json"), name

    def test_the_request_really_carries_the_query(self, recorder, client):
        client.overview_second_part(SYMBOL)
        assert recorder.paths[0].endswith("?format=json")

    def test_the_technical_parameters_come_back_as_the_vendor_sent_them(self, client):
        parameters = client.adv_technical_analysis(SYMBOL)
        assert isinstance(parameters, dict)
        assert parameters["rsi"]["name"] == "RSI(14)"          # measured on the TCS capture
        assert "sma_parameters" in parameters
        assert isinstance(parameters["sma_parameters"], list)

    def test_the_tidy_signal_frame_flattens_three_shapes_and_names_the_source_key(self, client):
        frame = client.technical_signals(SYMBOL)
        assert list(frame.columns) == ["name", "value", "insight", "color", "parameter"]
        # One record, a list of records (sma_parameters) and a dict of records (pivot_level) all
        # reach the same frame, and two records both called "5 Day" stay distinguishable.
        assert {"sma_parameters", "ema_parameters", "pivot_level", "rsi"} <= set(frame["parameter"])
        assert (frame["parameter"] == "sma_parameters").sum() == 8
        assert frame.iloc[0]["name"] == "5 Day"
        assert frame.iloc[0]["value"] == 2105.34

    def test_the_keys_this_frame_does_not_speak_for_are_published(self, client):
        frame = client.technical_signals(SYMBOL)
        unread = frame.attrs["bfinance_trendlyne_unread_parameters"]
        assert "current_price" in unread and "day_low" in unread
        assert len(unread) == frame.attrs["bfinance_trendlyne_parameter_keys"] - (
            frame["parameter"].nunique()
        )

    def test_a_string_insight_is_carried_rather_than_turned_into_none(self, recorder, client):
        """``insight`` is read as a ``{shorttext, longtext}`` object; a **plain string** was being
        discarded into ``None`` with nothing recorded.

        The choice made here is to **carry** the string rather than to list the parameter in
        ``unread_parameters``: that list names keys which produced no row at all, and a key whose
        row is present with a real insight in it is not one of them. Filing ``rsi`` there would
        also break the arithmetic the test above pins -- ``parameter_keys - nunique`` -- because the
        key *is* spoken for. So the value is kept, and this asserts it through the public method.
        """
        recorded = payload("adv_technical_analysis_tcs.json")
        parameters = recorded["body"]["parameters"]
        assert isinstance(parameters["rsi"]["insight"], dict), (
            "the fixture must really carry the dict form, or this test is not changing anything"
        )
        parameters["rsi"]["insight"] = "RSI is overbought"

        recorder.overrides["/equity/api/stock/adv-technical-analysis/"] = httpx.Response(
            200, json=recorded
        )
        frame = client_for(recorder).technical_signals(SYMBOL)

        row = frame[frame["parameter"] == "rsi"].iloc[0]
        assert row["insight"] == "RSI is overbought"
        assert row["name"] == parameters["rsi"]["name"]
        # The key is still spoken for, so it must not have been filed as unread.
        assert "rsi" not in frame.attrs["bfinance_trendlyne_unread_parameters"]

    def test_control_a_dict_insight_still_reads_its_shorttext(self, client):
        """The other half: the dict form is what the recorded capture carries, so the string
        branch above must not have displaced it."""
        frame = client.technical_signals(SYMBOL)
        row = frame[frame["parameter"] == "rsi"].iloc[0]

        assert row["insight"] == payload("adv_technical_analysis_tcs.json")[
            "body"
        ]["parameters"]["rsi"]["insight"]["shorttext"]

    def test_control_an_absent_insight_is_still_none(self):
        """The negative control. A record with no ``insight`` key at all must not gain a value from
        the string branch."""
        frame = tl.parse_technical_signals(
            {"rsi": {"name": "RSI(14)", "value": 50.6, "color": "grey"}}, symbol=SYMBOL
        )

        assert frame.iloc[0]["insight"] is None

    def test_overview_second_part_returns_the_whole_body(self, client):
        body = client.overview_second_part(SYMBOL)
        assert "head" not in body, "the envelope is unwrapped; head.status == '0' was the gate"
        assert set(body) == {
            "eventsData", "researchReports", "corporateFiling", "earningCallsDocuments",
            "annualReportsDocuments", "creditRatingDocuments",
            "investorPresentationsDocuments", "companyProfileData", "faq",
        }

    def test_the_five_event_frames_and_their_own_columns(self, client):
        events = client.events(SYMBOL)
        assert list(events) == ["board_meeting", "dividend", "bonus", "split", "rights"]
        assert list(events["dividend"].columns) == [
            "exDate", "dividendAmount", "dividendType", "recordDate", "instrumentType",
        ]
        assert list(events["board_meeting"].columns) == ["boardMeetDate", "purpose", "remarks"]

    def test_an_event_the_symbol_never_had_is_an_empty_frame_with_its_columns(self, client):
        """TCS has neither a split nor a rights issue on the recorded capture."""
        events = client.events(SYMBOL)
        assert events["split"].empty and list(events["split"].columns) == [
            "exDate", "oldFV", "newFV", "recordDate",
        ]
        assert events["rights"].empty and "rightsPremium" in events["rights"].columns

    def test_every_event_frame_publishes_the_same_absent_list(self, client):
        """A shared list, not a per-frame prefix: two frames must not disagree about what is
        missing."""
        frames = client.events(SYMBOL).values()
        absent = [list(f.attrs["bfinance_trendlyne_events_absent"]) for f in frames]
        assert all(item == absent[0] for item in absent)

    def test_the_company_frame_is_people_and_the_prose_stays_in_attrs(self, client):
        frame = client.company_overview(SYMBOL)
        assert frame.columns[0] == "role"
        assert set(frame["role"]) == {"director", "executive"}
        assert frame.attrs["bfinance_trendlyne_directors"] == 9
        assert frame.attrs["bfinance_trendlyne_top_executives"] == 35
        assert frame.attrs["bfinance_trendlyne_website"] == "http://www.tcs.com"
        assert "global leader in IT services" in frame.attrs[
            "bfinance_trendlyne_company_description"
        ]
        # The two blocks publish different keys, and the gap must stay visible: 'date_str' is the
        # directors block's own key and no executive row carries it. Filling it from the directors
        # block would be this package's inference, not the vendor's record.
        assert "date_str" in frame.columns and "start_date" in frame.columns
        assert frame[frame["role"] == "executive"]["date_str"].isna().all()
        assert frame[frame["role"] == "director"]["date_str"].notna().all()

    def test_the_returns_frame_keeps_the_vendor_values_uncoerced(self, client):
        frame = client.returns_comparison(SYMBOL)
        assert list(frame.columns) == ["Period", "Stock", "Nifty50", "Sensex", "Industry", "Sector"]
        assert len(frame) == 9
        assert frame.iloc[0]["Period"] == "1 Day"
        # The block mixes floats and strings in one column; a coercion rule would have to pick.
        assert isinstance(frame.iloc[0]["Nifty50"], str)

    def test_price_performance_returns_the_whole_body(self, client):
        body = client.price_performance(SYMBOL)
        assert set(body) == {
            "lastModified", "returnsComparison", "returnsDeepDive",
            "returnsPatternData", "dailyData",
        }


#: A failed trendlyne envelope, verbatim: HTTP 200, ``head.status == "2"``, empty body. The shape
#: this package already handles for a rejected OHLC token, reproduced for the new JSON routes.
FAILED_ENVELOPE = {"head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}}


class TestTheEnvelopeIsTheGate:
    """``head.status`` decides success. The HTTP status alone does not, on any of these routes."""

    @pytest.mark.parametrize("needle", sorted(ENVELOPE_ROUTE_METHODS))
    def test_a_200_with_a_failed_envelope_is_a_refusal(self, needle, recorder):
        recorder.overrides[needle] = httpx.Response(200, json=FAILED_ENVELOPE)
        with pytest.raises(TrendlyneNotAuthorizedError):
            getattr(client_for(recorder), ENVELOPE_ROUTE_METHODS[needle])(SYMBOL)

    def test_the_refusal_names_the_route_and_the_status(self, recorder):
        recorder.overrides["/equity/overview-second-part/"] = httpx.Response(
            200, json=FAILED_ENVELOPE
        )
        with pytest.raises(TrendlyneNotAuthorizedError) as excinfo:
            client_for(recorder).overview_second_part(SYMBOL)
        message = str(excinfo.value)
        assert "status='2'" in message and SYMBOL in message

    def test_a_200_with_a_good_envelope_is_data_not_a_refusal(self, recorder, client):
        """The control: an envelope check that always failed would pass every test above."""
        body = client.overview_second_part(SYMBOL)
        assert "eventsData" in body


# =========================================================== the WAF


class TestTheWafChallengeIsTerminal:
    """HTTP 405, or the header, on any status — and never retried.

    The observed failure is per **session**, not per request: the budget is spent rather than
    momentarily exceeded, so a retry does not wait out a rate limit, it spends more of a budget
    that is already gone. ``max_retries=2`` here, so a retry would show up as a second call.
    """

    @pytest.mark.parametrize("label", sorted(WAF_FORMS))
    def test_the_refusal_is_raised_and_not_retried(self, label, recorder):
        recorder.overrides["/equity/bulk-block-deals/"] = WAF_FORMS[label]()
        with pytest.raises(TrendlyneWafChallengeError):
            client_for(recorder).bulk_block_deals(SYMBOL)
        assert len(recorder.calls) == 1, (
            f"{label}: the WAF refusal was retried, which spends more of a spent budget"
        )

    def test_it_is_catchable_as_an_upstream_service_error(self, recorder):
        """The mixin is the point: existing ``except UpstreamServiceError`` handlers work."""
        import bfinance

        assert issubclass(TrendlyneWafChallengeError, bfinance.UpstreamServiceError)
        assert issubclass(TrendlyneWafChallengeError, tl.TrendlyneError)
        recorder.overrides["/equity/bulk-block-deals/"] = httpx.Response(405)
        with pytest.raises(TrendlyneWafChallengeError) as excinfo:
            client_for(recorder).bulk_block_deals(SYMBOL)
        assert excinfo.value.status_code == 405
        assert excinfo.value.url.endswith("/equity/bulk-block-deals/MOTHERSON/878/"
                                          "samvardhana-motherson-international-ltd/")

    def test_the_message_cites_the_budget_as_a_third_party_measurement(self, recorder):
        recorder.overrides["/equity/bulk-block-deals/"] = httpx.Response(405)
        with pytest.raises(TrendlyneWafChallengeError) as excinfo:
            client_for(recorder).bulk_block_deals(SYMBOL)
        message = str(excinfo.value)
        assert "130-150" in message
        assert "third-party measurement" in message, (
            "the ~130-150 figure is an outside measurement of anonymous access, not a limit "
            "bfinance publishes; presenting it as the latter would be a promise this package "
            "cannot keep"
        )
        assert "2026-10-10" in message
        assert "x-amzn-waf-action" in message

    def test_a_plain_403_without_the_header_is_still_an_access_denial(self, recorder):
        """The negative control: it is the header, not merely a 4xx, that means WAF."""
        recorder.overrides["/equity/bulk-block-deals/"] = httpx.Response(403, text="Forbidden")
        with pytest.raises(TrendlyneAccessDeniedError) as excinfo:
            client_for(recorder).bulk_block_deals(SYMBOL)
        assert not isinstance(excinfo.value, TrendlyneWafChallengeError)

    def test_a_200_without_the_header_is_parsed_as_data(self, recorder, client):
        """The other half of the control: the guard cannot be a blanket refusal of every 200."""
        assert not client_for(recorder).bulk_block_deals(SYMBOL).empty

    def test_the_guard_covers_the_json_routes_too(self, recorder):
        recorder.overrides["/equity/overview-second-part/"] = httpx.Response(
            405, headers={"x-amzn-waf-action": "captcha"}
        )
        with pytest.raises(TrendlyneWafChallengeError):
            client_for(recorder).events(SYMBOL)


# =========================================================== sync twins


class TestTheSyncTwins:
    @pytest.mark.parametrize(
        "method", ["bulk_block_deals", "share_holding", "board_meetings", "latest_news",
                   "stock_alerts", "insider_trading", "monthly_mf_holdings", "events",
                   "technical_signals", "returns_comparison"]
    )
    def test_the_sync_twin_produces_the_same_shape_for_one_request(self, method, recorder):
        sync = getattr(client_for(recorder), method)(SYMBOL)
        assert len(recorder.calls) == 1
        assert sync is not None and recorder.paths == [path_of(method)]


# =========================================================== the mock is not the pass


class TestTheMockItselfCannotBeTheThingThatPasses:
    """Controls on the harness. A test that cannot fail is not a test."""

    def test_an_unregistered_route_answers_404_not_an_empty_200(self, client):
        """Otherwise a method pointing at the wrong route would "succeed" on the wrong body.

        The Recorder falls through to 404 for any URL no fixture claims, so a client method that
        builds a path outside :data:`ROUTES` fails here with :class:`TrendlyneNotFoundError` rather
        than parsing whatever it happened to be served.
        """
        with pytest.raises(TrendlyneNotFoundError) as excinfo:
            client.fetch_text("/no/fixture/is/registered/for/this/")
        assert "404" in str(excinfo.value)

    def test_the_mutual_fund_needle_wins_over_the_share_holding_one(self, client):
        """``monthly-mutual-fund-share-holding`` contains ``share-holding``; if the order were
        wrong the MF method would be reading a share-holding page and fail confusingly."""
        frame = client.monthly_mf_holdings(SYMBOL)
        assert "MF | MF" in frame.columns
        assert len(frame.columns) == 10

    def test_each_fixture_was_parsed_as_the_table_it_is(self):
        """One line per fixture, so a swapped fixture shows up as a failure here."""
        assert tbt.classify_table(list(tbt.find_table(
            text("bulk_deals_motherson.html"), "deals").columns)) == "deals"
        assert tbt.classify_table(list(tbt.find_table(
            text("board_meeting_tcs.html"), "board_meeting").columns)) == "board_meeting"
        assert tbt.classify_table(list(tbt.find_table(
            text("research_reports_tcs.html"), "research_report").columns)) == "research_report"
        assert tbt.classify_table(list(tbt.find_table(
            text("insider_trading_tcs.html"), "insider_trade").columns)) == "insider_trade"
        assert len(tbt.page_signatures(text("shareholding_tcs.html"))) == 7
        assert len(tbt.page_signatures(text("swot_tcs.html"))) == 5

    def test_the_news_and_alert_routes_really_have_no_table(self, client):
        """The reason those two parsers exist at all. If a future page grows a table, this fails
        and the parser has to be revisited rather than quietly returning nothing."""
        assert tbt.page_signatures(text("latest_news_tcs.html")) == {}
        assert tbt.page_signatures(text("stock_alerts_tcs.html")) == {}
        assert len(client.latest_news(SYMBOL)) == 10
        assert len(client.stock_alerts(SYMBOL)) == 10

    def test_the_json_fixtures_all_report_status_zero(self):
        for name in ("adv_technical_analysis_tcs.json", "overview_second_part_tcs.json",
                     "price_performance_tcs.json"):
            assert payload(name)["head"]["status"] == "0", name

    def test_the_shareholding_parse_is_not_vacuous(self, client):
        """The summary grid is 12 labelled rows x 13 columns on this fixture -- five top-level
        categories, seven collapsed sub-rows and the marker column -- and a parser that returned
        an empty frame would satisfy several of the shape assertions above."""
        frame = client.share_holding(SYMBOL)
        assert frame.shape == (12, 13)
        assert "Promoter" in frame.index
        assert frame.index.duplicated().sum() == 1, "'Others' is both a DII sub-row and a category"
