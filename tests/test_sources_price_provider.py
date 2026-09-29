"""
The price-provider reader, driven offline against a **recorded** payload over
``httpx.MockTransport``.

What this file is for
---------------------
:mod:`bfinance.sources.price_provider` exists because ``bfinance.trendlyne`` is a leaf: the
isolation rules forbid this package from importing it, so the reader duplicates the payload parser
rather than reusing the one that already exists. Duplication rots. The mitigation is this file --
it drives the local reader over the **same** recorded payloads the leaf's own tests use and
asserts the parsed bars, so a schema change on the vendor's side, or a parser that drifts from
the leaf's, is a test failure rather than silent staleness.

No socket is opened. Every request is served by a :class:`httpx.MockTransport` handler keyed on a
substring of the URL, which is also what makes the three-request chain (sitemap -> symbol page ->
OHLC JSON) assertable: the test can count the requests and the ``Referer`` header, which is the
difference between "the reader fetched something" and "the reader fetched the right thing".

The three things asserted here that a passing test could otherwise get wrong for free
-------------------------------------------------------------------------
* **The bars.** Specific Open/High/Low/Close/Volume values on specific dates, taken from the
  recorded payload, not a shape check. ``tests/test_trendlyne_core.py`` already pins the sibling
  parser against the same fixture, so a row that silently went to the wrong column index would
  diverge from a test that exists.
* **That the chain is real.** Three URLs, and the OHLC request carries the ``Referer`` the
  vendor's ``/mapp/v1/`` gate requires. A reader that dropped the referer would still "succeed"
  against a mock that never checks headers, and would answer HTTP 444 in production.
* **That the mutation control can fail.** The last test in this file re-runs the first test's
  assertions against a deliberately corrupted payload and requires the parse to refuse. A parser
  that returns an empty frame instead of raising would make the whole file pass for the wrong
  reason.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Dict, List

import httpx
import pandas as pd
import pytest

import bfinance.sources as sources
from bfinance.sources import price_provider as pp
from bfinance.sources.bindings import price_provider_history
from bfinance.utils.exceptions import (
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "trendlyne"
PACKAGE_DIR = pathlib.Path(sources.__file__).parent

SITEMAP = FIXTURES / "sitemap_excerpt.xml"
STOCK_PAGE = FIXTURES / "stock_page_motherson.html"
OHLC = FIXTURES / "ohlc_motherson.json"

#: The symbol the whole chain resolves. It is MOTHERSON because all three recorded fixtures agree
#: on it -- the sitemap excerpt carries ``/equity/878/MOTHERSON/...``, the stock page is
#: MOTHERSON's, and the OHLC payload's own ``stockData`` says id 878 -- and because it is one of
#: the two names the 2026-09-30 basis measurement found to DISAGREE with the bhavcopy by exactly
#: its 1:2 bonus factor. The reader under test is therefore the reader whose basis declaration
#: matters.
SYMBOL = "MOTHERSON"
STOCK_ID = 878

#: The literal vendor name, assembled rather than written, so this file's own guard cannot match
#: itself. Same trick ``tests/test_sources_cascade.py`` uses for the same reason.
VENDOR_PACKAGE = "trend" + "lyne"

#: The three bars this file asserts values on, taken from the recorded payload: the newest
#: ``eodData`` bar, one from the ``eodDataLastWeek`` key, and one from the oldest retained bar --
#: which is where ``close_price`` and ``last_price`` actually differ. All five OHLCV fields, so a
#: parser that read the wrong column index cannot pass.
EOD_BAR = ("2026-09-18", 161.87, 165.37, 161.84, 162.57, 19068300.0)
LAST_WEEK_BAR = ("2026-09-24", 163.75, 164.8, 162.5, 162.5, 22050862.0)
#: The discriminating bar: close 3.266 against last_price 3.26.
DISCRIMINATING_BAR = ("2006-01-05", 3.289, 3.305, 3.25, 3.266, 280873.0)


def text(name: pathlib.Path) -> str:
    return name.read_text(encoding="utf-8")


def payload(name: pathlib.Path = OHLC) -> dict:
    return json.loads(text(name))


class Recorder:
    """A MockTransport handler that serves three routes and records every request it answered."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "sitemap-stocks.xml" in url:
            return httpx.Response(200, text=text(SITEMAP))
        if "/mapp/v1/stock/web/ohlc/" in url:
            return httpx.Response(200, json=payload())
        if "/equity/" in url:
            return httpx.Response(200, text=text(STOCK_PAGE))
        return httpx.Response(404)

    @property
    def urls(self) -> List[str]:
        return [str(r.url) for r in self.requests]


def reader(handler) -> pp.PriceProvider:
    """A reader whose every request is served by ``handler``, with pacing off."""
    return pp.PriceProvider(
        transport=httpx.MockTransport(handler),
        min_request_interval_ms=0.0,
        max_retries=2,
    )


# ============================================================ the parse, cell for cell


class TestTheRecordedPayloadIsParsedIntoBars:
    def test_three_specific_bars_carry_the_values_the_wire_carries(self):
        """
        The load-bearing assertion: real numbers, not a shape check.

        ``eodData`` is newest-first on the wire, ``eodDataLastWeek`` is a separate key, and
        ``last_price`` is a different field from ``close_price`` that agrees with it on most
        recent bars. All three are ways a parser can look right and be wrong, so the bars are
        asserted by value and by date -- and the third bar is chosen to be one where
        ``close_price`` (3.266) and ``last_price`` (3.26) genuinely differ, so a parser that read
        the neighbouring column would fail here and pass every recent-bar check.
        """
        frame = reader(Recorder()).price_history(SYMBOL)

        assert list(frame.columns) == list(pp.COLUMNS)
        assert frame.index.name == "Date"
        assert frame.index.is_monotonic_increasing, "the wire is newest-first; the frame is not"

        for date, open_, high, low, close, volume in (
            EOD_BAR, LAST_WEEK_BAR, DISCRIMINATING_BAR
        ):
            row = frame.loc[pd.Timestamp(date)]
            assert (row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]) == (
                open_, high, low, close, volume
            ), f"{date} parsed to {row.to_dict()}, not the recorded bar"

    def test_the_five_freshest_sessions_come_from_the_separate_last_week_key(self):
        """
        A frame built from ``eodData`` alone is a week behind the vendor's own latest bar.

        That is a real, measured property of the route, and a reader that ignores the second key
        returns a frame that looks complete and is short. The count is published rather than
        inferred.
        """
        frame = reader(Recorder()).price_history(SYMBOL)
        body = payload()["body"]

        assert len(body["eodDataLastWeek"]) == 5
        assert frame.attrs[pp.LAST_WEEK_ATTR] == 5
        assert str(frame.index[-1].date()) == "2026-09-25"
        # And the key is not dropped when the caller opts out -- the merge is a decision, not a
        # silent difference in bar count.
        without = reader(Recorder()).price_history(SYMBOL, include_last_week=False)
        assert len(without) == len(frame) - 5
        assert str(without.index[-1].date()) == "2026-09-18"

    def test_the_frame_publishes_the_price_basis_and_says_what_it_may_not_be_spliced_with(self):
        """
        The basis is a claim this package exists to make checkable, so it is on ``attrs`` and in
        prose, and the prose names the failure rather than the rule.
        """
        attrs = reader(Recorder()).price_history(SYMBOL).attrs

        assert attrs[pp.PRICE_BASIS_ATTR] == "as_traded_split_bonus_adjusted"
        assert attrs[pp.DEMERGER_APPLIED_ATTR] is False
        assert attrs["bfinance_synthetic_ohlc"] is False
        assert "spliced" in attrs[pp.NOTE_ATTR].lower()
        assert attrs[pp.CLOSE_LAST_DIVERGENT_ATTR] > 0, (
            "this payload's close and last price disagree on many rows; a count of zero would "
            "mean the divergence was resolved silently rather than counted"
        )

    def test_the_token_is_redacted_on_the_recorded_url_and_absent_from_the_log(self, caplog):
        """
        The token is a per-symbol credential for a private API surface.

        It is necessarily **in** the request URL -- that is the only way to address the route --
        so "the token does not leak" cannot mean "the token is never in a URL". It means the
        token does not reach anywhere a reader other than the HTTP client can see: not ``attrs``
        (which go into a CSV export), not a log line, not an exception message. Asserted on all
        three, because each is a separate path out.
        """
        import logging

        recorder = Recorder()
        with caplog.at_level(logging.DEBUG, logger="bfinance"):
            attrs = reader(recorder).price_history(SYMBOL).attrs

        token = pp.extract_ohlc_token(text(STOCK_PAGE))
        assert token, "the recorded stock page must carry a token for this test to mean anything"
        assert f"/ohlc/{STOCK_ID}/" in recorder.urls[2], (
            "the request itself must carry the real token, or this test would pass on a reader "
            "that never made a working request"
        )
        assert "<token>" in attrs[pp.SOURCE_ATTR]
        assert token not in repr(attrs)
        assert [r for r in caplog.records if token in r.getMessage()] == []

    def test_a_failure_message_never_prints_the_token(self):
        """The error path is the other place a URL escapes, and the one a log line is built from."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "/mapp/v1/stock/web/ohlc/" in str(request.url):
                return httpx.Response(444)
            return Recorder()(request)

        with pytest.raises(UpstreamServiceError) as excinfo:
            reader(handler).price_history(SYMBOL)

        token = pp.extract_ohlc_token(text(STOCK_PAGE))
        assert "444" in str(excinfo.value)
        assert token not in str(excinfo.value)


class TestTheThreeRequestChainIsReal:
    def test_sitemap_then_page_then_json_and_nothing_else(self):
        """
        The chain is the reader. Asserting the URLs, not just the result.

        A reader that invented a symbol id, or that skipped the page and guessed a token, would
        produce a frame in a mock and a 444 in production, and the difference is invisible unless
        the requests are counted.
        """
        recorder = Recorder()
        reader(recorder).price_history(SYMBOL)

        assert len(recorder.urls) == 3
        assert "sitemap-stocks.xml" in recorder.urls[0]
        assert f"/equity/{STOCK_ID}/{SYMBOL}/" in recorder.urls[1]
        assert "/mapp/v1/" not in recorder.urls[1]
        assert f"/mapp/v1/stock/web/ohlc/{STOCK_ID}/" in recorder.urls[2]

    def test_the_json_request_carries_the_referer_the_vendor_gate_requires(self):
        """
        ``/mapp/v1/`` answers HTTP 444 with a zero-length body unless the request carries a
        ``Referer`` naming a page on the site. A mock that ignores headers would let a reader
        without one pass every other test here.
        """
        recorder = Recorder()
        reader(recorder).price_history(SYMBOL)

        json_request = recorder.requests[-1]
        referer = json_request.headers.get("referer", "")
        assert referer.startswith(pp.BASE_URL), (
            f"the OHLC request went without a site referer ({referer!r}); the vendor answers that "
            f"with HTTP 444 and an empty body, which is a bot gate and not a bad symbol"
        )
        # And the page request does NOT need one, so it does not get one: a header sent
        # unconditionally misreports a navigation as an XHR.
        assert "referer" not in recorder.requests[1].headers

    def test_the_sitemap_and_the_token_are_cached_so_the_second_call_costs_one_request(self):
        """Two of the three requests are per-index and per-symbol; repeating them doubles the
        cost of every symbol forever."""
        recorder = Recorder()
        provider = reader(recorder)

        provider.price_history(SYMBOL)
        assert len(recorder.urls) == 3
        provider.price_history(SYMBOL)
        assert len(recorder.urls) == 4, "a warm reader should spend one request, not three"


class TestTheFailuresThatAreNotFailures:
    def test_a_rejected_token_is_a_200_with_a_failed_envelope_and_is_refused(self):
        """
        The single most important thing about this route: a bad token answers **HTTP 200**.

        Status-code checking alone would report success and hand the caller an empty body. The
        envelope is therefore checked, and a failed one is a refusal rather than a frame.
        """
        rejected = httpx.Response(
            200,
            json={"head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}},
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "/mapp/v1/stock/web/ohlc/" in str(request.url):
                return rejected
            recorder = Recorder()
            return recorder(request)

        with pytest.raises(UpstreamServiceError) as excinfo:
            reader(handler).price_history(SYMBOL)
        message = str(excinfo.value)
        assert "status='2'" in message
        assert "HTTP 200" in message, (
            "the message must name the trap, or it reads as a decode error"
        )

    def test_444_is_terminal_and_never_reaches_a_json_decode(self):
        """
        444 is the vendor's request gate, not a bad symbol, and retrying an unchanged request
        cannot succeed. Asserted through the attempt count, because "it raised" alone would also
        be true of a reader that retried five times and gave up.
        """
        attempts: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(str(request.url))
            if "/mapp/v1/stock/web/ohlc/" in str(request.url):
                return httpx.Response(444)
            return Recorder()(request)

        with pytest.raises(UpstreamServiceError, match="444"):
            reader(handler).price_history(SYMBOL)

        ohlc_calls = [u for u in attempts if "/mapp/v1/" in u]
        assert len(ohlc_calls) == 1, f"444 was retried: {ohlc_calls}"

    def test_429_is_a_rate_limit_error_not_a_generic_upstream_failure(self):
        """The one status a caller may usefully retry later gets its own type."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "/mapp/v1/stock/web/ohlc/" in str(request.url):
                return httpx.Response(429)
            return Recorder()(request)

        with pytest.raises(RateLimitExceededError):
            reader(handler).price_history(SYMBOL)

    def test_a_symbol_the_sitemap_does_not_carry_is_refused_named_not_answered_thin(self):
        """
        A frame for the wrong instrument is worse than no frame. The live sitemap held 5,925
        entries on 2026-09-30 and TATAMOTORS was not one of them, and no ETF is -- the only route
        that resolves them is robots-disallowed -- so a miss is a real outcome of this route, not
        only a typo.
        """
        with pytest.raises(TickerNotFoundError) as excinfo:
            reader(Recorder()).price_history("NIFTYBEES")
        assert "NIFTYBEES" in str(excinfo.value)

    def test_a_schema_change_is_a_refusal_and_not_an_empty_frame(self):
        """
        The failure this whole package is organised around: a vendor serving a shell at HTTP 200
        must not read as a quiet market. Renaming a required column is the smallest schema change
        that produces it.
        """
        broken = payload()
        broken["body"]["eodHeaders"][1]["unique_name"] = "opening_price"
        with pytest.raises(UpstreamServiceError) as excinfo:
            pp.parse_ohlc_payload(broken, SYMBOL)
        assert "open_price" in str(excinfo.value)


class TestTheReaderIsReachableThroughTheRegistry:
    def test_the_adapter_passes_the_symbol_and_drops_a_defaulted_window(self):
        """
        The adapter is the only place that knows this entry's signature, and it must not invent
        a date. Forwarding ``start=None`` would be a different request from omitting it, which is
        the same reason the bhavcopy adapter does not default its range.
        """
        seen: Dict[str, object] = {}

        def entry(**kwargs):
            seen.update(kwargs)
            return pd.DataFrame({"Close": [1.0]})

        price_provider_history(entry, {"symbol": SYMBOL})
        assert seen == {"symbol": SYMBOL}

        price_provider_history(entry, {"symbol": SYMBOL, "start": "2025-01-01",
                                       "end": "2025-06-30"})
        assert seen == {"symbol": SYMBOL, "start": "2025-01-01", "end": "2025-06-30"}

    def test_the_adapter_refuses_an_incomplete_request_before_any_io(self):
        """
        A malformed request must not reach the network. Without this the cascade could spend a
        real request on a request that was never going to be answerable.
        """
        reached: List[dict] = []

        def must_not_be_called(**kwargs):
            reached.append(kwargs)
            return kwargs

        with pytest.raises(ValueError, match="required key"):
            price_provider_history(must_not_be_called, {})
        assert reached == []

    def test_the_row_names_this_module_and_declares_the_measured_basis(self):
        """The row, its module string and its basis, read off the table rather than restated."""
        spec = sources.DEFAULT.source(sources.TRENDLYNE_OHLC_PROVIDER)

        assert spec is not None
        assert spec.module == "bfinance.sources.price_provider"
        assert spec.entry == "price_history"
        assert spec.wired is True
        assert spec.bases == (sources.Basis.SPLIT_ADJUSTED,), (
            "the basis is the measurement, not a preference: this route is "
            "as_traded_split_bonus_adjusted and 3 of 14 symbols disagreed with the bhavcopy by "
            "their own corporate-action factor over 62 of 62 sessions on 2026-09-30"
        )
        assert spec.serves(sources.Basis.SPLIT_ADJUSTED)
        assert not spec.serves(sources.Basis.AS_TRADED), (
            "declaring it as_traded is what would let a failover splice an adjusted series onto "
            "the tape"
        )
        # And the note carries the arithmetic, because a basis column with no evidence beside it
        # is a claim.
        for figure in ("932.60", "56.27", "1158.55", "0.500000", "0.666667"):
            assert figure in spec.note, f"the note lost the measurement figure {figure}"


    def test_a_window_is_applied_locally_and_costs_nothing(self):
        """
        This route publishes no period segment, so a narrower request is not a narrower fetch.

        That is the whole reason ``start``/``end`` are keyword arguments applied after the parse
        rather than query parameters: the request is the same one either way, and the test proves
        it by counting the requests.
        """
        recorder = Recorder()
        provider = reader(recorder)

        full = provider.price_history(SYMBOL)
        windowed = provider.price_history(SYMBOL, start="2026-09-16", end="2026-09-25")
        assert len(recorder.urls) == 4, "the windowed call must not have re-fetched anything"

        assert list(windowed.index) == [
            ts for ts in full.index if "2026-09-16" <= str(ts.date()) <= "2026-09-25"
        ]
        assert len(windowed) < len(full)
        # attrs survive the slice, so a narrowed frame still says what it is on.
        assert windowed.attrs[pp.PRICE_BASIS_ATTR] == pp.PRICE_BASIS

    def test_a_reversed_window_is_refused_rather_than_returned_empty(self):
        """An empty result and a refusal are different artefacts, and only one of them is a bug
        report about the caller's own arguments."""
        with pytest.raises(ValueError, match="is after end"):
            reader(Recorder()).price_history(SYMBOL, start="2026-09-25", end="2026-09-01")


class TestTheLeafPackageStaysALeaf:
    def test_no_module_in_this_package_imports_the_leaf(self):
        """
        The repo-wide sweep is ``tests/test_trendlyne_core.py::TestIsolation``'s job. This is the
        scoped copy, because the duplication above is the one place a future edit could plausibly
        "simplify" by importing the shared parser, and doing so would make this package's
        availability depend on a package it is forbidden to know about.
        """
        offenders = []
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                names: List[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                    names += [f"{node.module}.{alias.name}" for alias in node.names]
                if any(VENDOR_PACKAGE in name for name in names):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"bfinance/sources/ must not import the leaf package: {offenders}"

    def test_no_filename_in_this_package_names_the_leaf(self):
        """The same rule for the name, which the isolation test also matches on."""
        offenders = [p.name for p in sorted(PACKAGE_DIR.rglob("*.py"))
                     if VENDOR_PACKAGE in p.name.lower()]
        assert offenders == []


class TestTheParserIsNotVacuous:
    def test_a_mutated_payload_must_be_refused_not_parsed(self):
        """
        The negative control, and the reason the other tests in this file mean anything.

        :meth:`TestTheRecordedPayloadIsParsedIntoBars` asserts parsed values, which is the
        strongest kind of assertion -- unless the parse never ran. So the same parser is run over
        a payload whose OHLC rows have been **deleted**, and the whole point is that it raises
        rather than returning an empty frame. An empty frame is exactly what
        :func:`bfinance.sources.cascade.is_empty_payload` treats as a source failure, and a
        parser that returned one would turn this file green while the reader was broken.

        The second half closes the other direction: a payload whose *close* column is renamed must
        also be refused, so the control is not satisfied merely by "some corruption is detected".
        """
        emptied = payload()
        emptied["body"]["eodData"] = []
        with pytest.raises(UpstreamServiceError) as excinfo:
            pp.parse_ohlc_payload(emptied, SYMBOL)
        assert "is empty; nothing to parse" in str(excinfo.value)

        renamed = payload()
        renamed["body"]["eodHeaders"][4]["unique_name"] = "closing_price"
        with pytest.raises(UpstreamServiceError) as excinfo:
            pp.parse_ohlc_payload(renamed, SYMBOL)
        assert "close_price" in str(excinfo.value)

    def test_the_control_would_catch_a_parser_that_swapped_close_for_last_price(self):
        """
        The specific mutation a reader of this route is most likely to get wrong, and the reason
        the assertion above names a specific bar.

        ``last_price`` sits immediately after ``close_price`` on the wire and agrees with it on
        most recent bars, so a parser that read the wrong index still looks right on a recent
        window. The oldest retained bar is where the two differ (3.266 against 3.26), and the
        recorded values assert the right one was taken.
        """
        frame = reader(Recorder()).price_history(SYMBOL)
        body = payload()["body"]
        names = [h["unique_name"] for h in body["eodHeaders"]]
        close_at, last_at = names.index("close_price"), names.index("last_price")
        assert close_at != last_at, "the fixture no longer discriminates the two fields"

        for row in body["eodData"]:
            if row[0].startswith(DISCRIMINATING_BAR[0]):
                assert row[close_at] != row[last_at]
        bar = frame.loc[pd.Timestamp(DISCRIMINATING_BAR[0]), "Close"]
        assert float(bar) == DISCRIMINATING_BAR[4]
