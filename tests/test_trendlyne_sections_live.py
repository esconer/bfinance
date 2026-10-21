"""
Live smoke tests for the trendlyne section-page layer — **one symbol**, every route.

Run with::

    uv run pytest tests/test_trendlyne_sections_live.py -m live -p no:cacheprovider -q

The whole file is ``live``-marked and is deselected by ``-m "not live"``, which is the job the
CI gate runs. Nothing here is a second opinion on the offline suite: the offline suite in
``tests/test_trendlyne_sections.py`` pins every shape off the 2026-10-10 recordings, and these
tests answer the one question recordings cannot — **are the routes still the routes**.

Why one symbol and why TCS
--------------------------
TCS, because that is what every new-route recording was made against on 2026-10-10, so a failure
here is comparable with the evidence in ``tests/fixtures/trendlyne/META.json``. One symbol because
these tests spend the vendor's per-session WAF budget — the message on
:class:`~bfinance.trendlyne.errors.TrendlyneWafChallengeError` records a third-party measurement
of roughly 130-150 requests per session, and a file that fetched a page per assertion per symbol
would spend it on the first run and fail on the second.

One client, one session, and the module's own 1000 ms pacing between calls. The pacing is the
client's, not this file's: nothing here adds a sleep of its own, so this file cannot outrun the
site's published ``Crawl-delay: 1`` even if edited.

ROBOTS
------
Every route below was verified robots-clean against the live ``robots.txt`` on 2026-10-10 and
again on 2026-10-10 for the JSON siblings; the per-route verdict is recorded as a comment in
``trendlyne/client.py`` beside the endpoint itself. No test here requests a member, a write or an
alerts-mutation path, and the ``insider-trading-sast/all/`` route is the read-only list rather
than the per-holder drill-down.
"""
from __future__ import annotations

import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne.errors import (
    TrendlyneServerError,
    TrendlyneWafChallengeError,
)

pytestmark = pytest.mark.live

#: The one symbol every test in this file resolves.
SYMBOL = "TCS"


@pytest.fixture(scope="module")
def client() -> tl.TrendlyneClient:
    """One client for the whole module, so the sitemap index is fetched once and the session's
    request budget is spent on section pages rather than on the resolver."""
    return tl.TrendlyneClient()


class TestEverySectionRouteAnswersLive:
    """One call per method, asserting only what a live run can establish.

    The shapes are asserted in the offline suite; repeating all of them here would turn every vendor
    wording tweak into a red build for no new information. What is asserted is that the route
    answers, that it answers with the kind of thing the contract says it does, and that the guards
    behave on real bytes.
    """

    def test_a_sitemap_index_resolves_the_symbol(self, client):
        record = client.resolve(SYMBOL)
        assert record.symbol == SYMBOL
        assert record.stock_id and record.slug

    def test_corporate_actions(self, client):
        result = client.corporate_actions(SYMBOL)
        assert list(result) == ["bonus", "dividend", "other"]
        assert all("bfinance_trendlyne_table" in frame.attrs for frame in result.values())

    def test_bulk_block_deals(self, client):
        frame = client.bulk_block_deals(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_table"] == "deals"

    def test_share_holding_is_snapshot_only_on_live_bytes_too(self, client):
        """The 500 is a property of the route, and the refusal has to happen before the request."""
        frame = client.share_holding(SYMBOL)
        assert frame.attrs["bfinance_trendlyne_snapshot_only"] is True
        with pytest.raises(TrendlyneServerError) as excinfo:
            client.share_holding(SYMBOL, period="Jun-2026")
        assert "latest" in str(excinfo.value)

    def test_share_holding_holders(self, client):
        holders = client.share_holding_holders(SYMBOL)
        assert holders, "the page carries at least one holder table"
        assert all(h.attrs["bfinance_trendlyne_holder_category"] is not None for h in holders)

    def test_board_meetings(self, client):
        frame = client.board_meetings(SYMBOL)
        assert list(frame.columns) == ["Ex-date", "Purpose", "Notes"]

    def test_research_reports(self, client):
        frame = client.research_reports(SYMBOL)
        assert "Target" in frame.columns

    def test_latest_news(self, client):
        frame = client.latest_news(SYMBOL)
        assert list(frame.columns) == ["date", "kind", "title", "url", "source"]

    def test_latest_news_page_two_still_answers(self, client):
        """``?page=2`` was measured 200 on 2026-10-10; a page that stopped paginating would return
        page 1 again, and an empty frame is how that shows up."""
        frame = client.latest_news(SYMBOL, page=2)
        assert list(frame.columns) == ["date", "kind", "title", "url", "source"]

    def test_stock_alerts(self, client):
        frame = client.stock_alerts(SYMBOL)
        assert list(frame.columns) == ["date", "kind", "headline", "detail", "values"]

    def test_insider_trading_reports_its_row_count(self, client):
        """The cap guard has to work on live bytes, and the row count is the evidence either way."""
        frame = client.insider_trading(SYMBOL)
        rows = frame.attrs["bfinance_trendlyne_insider_rows"]
        assert rows == len(frame)
        assert isinstance(frame.attrs["bfinance_trendlyne_insider_rows_capped"], bool)
        if rows >= tl.INSIDER_ROW_CAP:
            assert "TRUNCATED" in frame.attrs["bfinance_trendlyne_note"]
        else:
            assert "TRUNCATED" not in frame.attrs["bfinance_trendlyne_note"]

    def test_the_mutual_fund_route_still_filters_by_period(self, client):
        """The measured counter-example to the share-holding guard. If this starts refusing, the
        guard has been copied onto the wrong route."""
        frame = client.monthly_mf_holdings(SYMBOL, period="Jun-2026")
        assert frame.attrs["bfinance_trendlyne_period"] == "Jun-2026"

    def test_buy_sell_zone(self, client):
        result = client.buy_sell_zone(SYMBOL)
        assert set(result) == {"standalone", "consolidated"}
        assert result["standalone"].attrs["bfinance_trendlyne_current_pe"] is not None

    def test_swot(self, client):
        result = client.swot(SYMBOL)
        assert result, "the SWOT widget publishes at least one fact table"
        assert all(frame.attrs["bfinance_trendlyne_swot_heading"] for frame in result.values())

    def test_the_three_json_routes(self, client):
        parameters = client.adv_technical_analysis(SYMBOL)
        assert isinstance(parameters, dict) and parameters

        body = client.overview_second_part(SYMBOL)
        assert "eventsData" in body and "companyProfileData" in body

        performance = client.price_performance(SYMBOL)
        assert "returnsComparison" in performance

    def test_the_tidy_views_of_those_json_routes(self, client):
        assert not client.technical_signals(SYMBOL).empty
        assert set(client.events(SYMBOL)) >= {"board_meeting", "dividend", "bonus"}
        assert not client.company_overview(SYMBOL).empty
        assert not client.returns_comparison(SYMBOL).empty


class TestTheWafGuardOnLiveBytes:
    """The guard is mostly offline. What only a live run can show is that the WAF's real response
    is one the guard recognises — so this asserts the *shape* of the exception, tolerating the case
    where the session was never challenged."""

    def test_a_challenge_is_a_typed_terminal_refusal_not_a_decode_error(self, client):
        try:
            frame = client.bulk_block_deals(SYMBOL)
        except TrendlyneWafChallengeError as exc:
            # Terminal, typed, and the message says what the caller can do about it.
            assert exc.status_code in (403, 405, 200)
            assert "WAF" in str(exc)
            assert "130-150" in str(exc)
            return
        assert not frame.empty, (
            "a WAF challenge must never reach the parser as an empty frame: if the site served a "
            "captcha page and this returned an empty deals frame, the guard has a hole"
        )
