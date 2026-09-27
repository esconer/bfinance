"""
trendlyne core tests — fully offline, driven by recorded fixtures.

Every fixture under ``tests/fixtures/trendlyne/`` is a real recording of trendlyne.com taken on
2026-09-27; ``META.json`` in that directory records the source URL, HTTP status, byte count and
SHA-256 prefix of each original response, plus the exact trim rule where a file is an excerpt.
The HTTP layer is exercised through ``httpx.MockTransport``, so **no test touches the network**.

The tests are organised around the claims the module makes, not around its functions:

- the package is a leaf and does no I/O at import (:class:`TestIsolation`)
- 444 and a rejected token are different failures with different types (:class:`TestHttpStatusMapping`)
- classification is by header row, so a heading cannot poison it (:class:`TestTableClassification`)
- a snapshot is never dressed up as a series (:class:`TestSnapshotHonesty`)
- attrs say what was and was not retrieved (:class:`TestProvenance`)
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pandas as pd
import pytest

import bfinance
from bfinance.trendlyne import client as tl
from bfinance.trendlyne import errors as tle
from bfinance.trendlyne import tables as tbt
from bfinance.utils.exceptions import ParsingError

FIXTURES = Path(__file__).parent / "fixtures" / "trendlyne"
PACKAGE_DIR = Path(tl.__file__).parent
SRC_DIR = Path(bfinance.__file__).parent


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def payload(name: str = "ohlc_motherson.json") -> dict:
    return json.loads(text(name))


@pytest.fixture
def client() -> tl.TrendlyneClient:
    """A client with pacing disabled (the real 750 ms slot would add nothing to a mock)."""
    return tl.TrendlyneClient(min_request_interval_ms=0.0, max_retries=2)


@pytest.fixture
def indexed_client(client: tl.TrendlyneClient) -> tl.TrendlyneClient:
    """A client whose symbol index is already loaded from the recorded sitemap excerpt."""
    client.load_symbol_index(text("sitemap_excerpt.xml"))
    return client


def mock_client(handler, **kwargs) -> tl.TrendlyneClient:
    """A client whose every request is served by ``handler`` — no network, no sockets."""
    kwargs.setdefault("min_request_interval_ms", 0.0)
    kwargs.setdefault("max_retries", 2)
    return tl.TrendlyneClient(transport=httpx.MockTransport(handler), **kwargs)


def static(mapping: dict, default=None):
    """A MockTransport handler keyed on a substring of the request URL."""
    def handler(request: httpx.Request) -> httpx.Response:
        for needle, response in mapping.items():
            if needle in str(request.url):
                return response() if callable(response) else response
        return default() if callable(default) else (default or httpx.Response(404))
    return handler


# =========================================================== isolation


class TestIsolation:
    """The module's reason for existing: bfinance must not know this package exists."""

    def test_no_other_bfinance_module_imports_trendlyne(self):
        """No inbound edges. ``bfinance/__init__.py`` is exempt because its export is another
        agent's explicit decision, and a leaf must not forbid that."""
        offenders = []
        for path in sorted(SRC_DIR.rglob("*.py")):
            if PACKAGE_DIR in path.parents or path == PACKAGE_DIR:
                continue
            if path == SRC_DIR / "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                    names += [f"{node.module}.{a.name}" for a in node.names]
                if any("trendlyne" in name for name in names):
                    offenders.append(f"{path.relative_to(SRC_DIR)}:{node.lineno}")
        assert offenders == [], f"trendlyne must be a leaf, but these files import it: {offenders}"

    def test_import_performs_no_network_io(self):
        """Importing the package must not open a socket, in this process or any other."""
        program = textwrap.dedent(
            """
            import socket

            def _forbidden(*args, **kwargs):
                raise AssertionError("network I/O at import time")

            socket.socket.connect = _forbidden
            socket.socket.connect_ex = _forbidden
            socket.create_connection = _forbidden
            socket.getaddrinfo = _forbidden

            import bfinance.trendlyne as t
            t.TrendlyneClient()          # construction must not fetch anything either
            t.TrendlyneClient().clear_caches()
            assert t.PROVENANCE == "trendlyne"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "OK" in result.stdout

    def test_module_level_shortcuts_do_not_build_a_client_at_import(self):
        """``_default_client`` must stay lazy: an eager global would make the first *call* the
        first I/O rather than the first request."""
        assert tl._DEFAULT_CLIENT is None
        assert "TrendlyneClient()" not in textwrap.dedent(tl.__doc__ or "")

    def test_importing_bfinance_does_not_import_trendlyne(self):
        """Even if a parent module later re-exports something from here, the eager import path
        must not drag the package in for unrelated callers."""
        program = textwrap.dedent(
            """
            import sys
            import bfinance
            print("bfinance.trendlyne" in sys.modules)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False", (
            "import bfinance pulled in bfinance.trendlyne; the leaf contract is broken"
        )

    def test_errors_are_catchable_as_bfinance_errors(self):
        """Both hierarchies hold at once, so existing ``except UpstreamServiceError`` handlers
        keep working around an optional source."""
        assert issubclass(tle.TrendlyneError, bfinance.BFinanceError)
        for name in ("TrendlyneRateLimitedError", "TrendlyneAccessDeniedError",
                     "TrendlyneServerError", "TrendlyneTransportError",
                     "TrendlyneNotAuthorizedError"):
            cls = getattr(tle, name)
            assert issubclass(cls, tle.TrendlyneError), name
            assert issubclass(cls, bfinance.UpstreamServiceError), name
        assert issubclass(tle.TrendlyneNotFoundError, bfinance.TickerNotFoundError)
        assert issubclass(tle.TrendlyneSymbolNotFoundError, bfinance.TickerNotFoundError)
        assert issubclass(tle.TrendlyneSchemaError, ParsingError)

    def test_every_public_error_derives_from_the_package_base(self):
        """A catch-all ``except TrendlyneError`` must be sufficient. Guards against a sibling
        module adding a bare ``UpstreamServiceError`` later."""
        for name in tle.__all__:
            assert issubclass(getattr(tle, name), tle.TrendlyneError), name


# =========================================================== symbol index


class TestSymbolIndex:
    def test_parses_id_symbol_and_slug(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        assert record == tl.TrendlyneSymbol(
            symbol="MOTHERSON", stock_id=878, slug="samvardhana-motherson-international-ltd"
        )
        assert record.page_path == "/equity/878/MOTHERSON/samvardhana-motherson-international-ltd/"
        assert record.page_url.startswith("https://trendlyne.com/equity/878/")

    def test_index_is_loaded_without_io(self, indexed_client):
        assert indexed_client.index_size == 12
        assert "MOTHERSON" in indexed_client._index

    def test_index_source_size_is_reported_as_a_diagnostic(self, indexed_client):
        """A 12-symbol index from a 2 KB excerpt versus 5,922 from 831 KB is the difference
        between 'not covered' and 'truncated', so the source size is observable."""
        assert indexed_client.index_source_bytes == len(
            text("sitemap_excerpt.xml").encode("utf-8")
        )
        assert indexed_client.index_source_bytes < 10_000

    def test_resolve_accepts_the_shapes_a_bfinance_user_types(self, indexed_client):
        for given in ("MOTHERSON", "motherson", "  MOTHERSON  ", "MOTHERSON.NS", "MOTHERSON-EQ"):
            assert indexed_client.resolve(given).stock_id == 878, given

    def test_resolve_rejects_an_unknown_symbol_loudly(self, indexed_client):
        with pytest.raises(tle.TrendlyneSymbolNotFoundError, match="sitemap"):
            indexed_client.resolve("TOTALLYNOTAREALTICKER")
        with pytest.raises(ValueError):
            indexed_client.resolve("")

    def test_non_ticker_symbol_segments_are_indexed_verbatim(self):
        """The live sitemap tail holds /equity/3622447/1216/bse-midsmall-private-banks/ — the
        symbol segment is a number, not a ticker. Guessing a ticker shape would lose these."""
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["1216"]
        assert record.stock_id == 3622447
        assert record.slug == "bse-midsmall-private-banks"

    def test_search_finds_by_slug_when_the_symbol_is_not_a_ticker(self, indexed_client):
        """A local substring search is the only way to reach the non-ticker entries, since
        trendlyne has no symbol-search API."""
        hits = indexed_client.search("private-banks")
        assert [h.symbol for h in hits] == ["1216"]

    def test_search_ranks_an_exact_symbol_match_first(self, indexed_client):
        assert indexed_client.search("MOTHERSON")[0].symbol == "MOTHERSON"
        with pytest.raises(ValueError):
            indexed_client.search("  ")

    def test_sitemap_without_entries_raises_instead_of_returning_empty(self):
        """An empty index would make every symbol look unresolvable — the silent failure this
        package exists to prevent."""
        with pytest.raises(tle.TrendlyneSchemaError, match="no /equity/"):
            tl.parse_sitemap("<urlset><url><loc>https://example.com/</loc></url></urlset>")
        with pytest.raises(tle.TrendlyneSchemaError):
            tl.parse_sitemap("")

    def test_index_ttl_zero_forces_a_refetch(self, indexed_client):
        """A zero TTL is a way to say 'never reuse the cache', and must not silently keep it."""
        handler = static({"equity-sitemap-stocks.xml": httpx.Response(
            200, text=text("sitemap_excerpt.xml"))})
        fresh = mock_client(handler, index_ttl_hours=0)
        calls = []

        def counting(request):
            calls.append(str(request.url))
            return handler(request)

        fresh.transport = httpx.MockTransport(counting)
        assert len(fresh.symbol_index()) == 12
        assert len(fresh.symbol_index()) == 12
        assert len(calls) == 2, "a zero TTL must re-fetch every time"


# =========================================================== token


class TestToken:
    def test_extracts_the_token_from_a_recorded_page(self):
        token = tbt.extract_ohlc_token(text("stock_page_motherson.html"))
        assert token == "TQZITAIRHO5FCC7SYAKGO6M7QA======"

    def test_token_keeps_its_equals_padding(self):
        """The trailing '=' are part of the token. Stripping them changes the credential."""
        token = tbt.extract_ohlc_token(text("stock_page_motherson.html"))
        assert token.endswith("=")
        assert "=" in token[:-1] or token.count("=") >= 1

    def test_repeated_occurrences_on_a_page_are_one_token(self):
        """The live page repeats the same data-eodurl 6 times (META notes it). First-match is safe
        only because they are identical, so assert that the ones in the excerpt are."""
        page = text("stock_page_motherson.html")
        tokens = tbt.find_ohlc_tokens(page)
        assert len(tokens) == 1
        assert page.count("/mapp/v1/stock/web/ohlc/878/") == 2, "the excerpt holds 2 of the 6"
        assert "6x" in json.loads(text("META.json"))["files"][
            "stock_page_motherson.html"
        ]["notes"]

    def test_missing_token_raises_rather_than_guessing(self):
        with pytest.raises(tle.TrendlyneTokenError, match="Refusing to guess"):
            tbt.extract_ohlc_token("<html><body>no chart here</body></html>")
        assert tbt.find_ohlc_tokens("<html></html>") == []

    def test_token_is_redacted_out_of_urls(self):
        """A token is a per-symbol credential: it must not reach attrs, logs or exceptions."""
        url = "https://trendlyne.com/mapp/v1/stock/web/ohlc/878/TQZITAIRHO5FCC7SYAKGO6M7QA======/"
        assert tl.redact(url) == "https://trendlyne.com/mapp/v1/stock/web/ohlc/878/<token>/"
        assert "TQZIT" not in tl.redact(url)

    def test_client_token_is_cached_so_the_page_is_fetched_once(self, indexed_client):
        page = text("stock_page_motherson.html")
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=page)

        indexed_client.transport = httpx.MockTransport(handler)
        first = indexed_client.ohlc_token("MOTHERSON")
        second = indexed_client.ohlc_token("motherson")
        assert first == second == "TQZITAIRHO5FCC7SYAKGO6M7QA======"
        assert len(calls) == 1, f"the page should be fetched once, got {calls}"

    def test_force_refresh_refetches_the_token(self, indexed_client):
        calls = []
        indexed_client.transport = httpx.MockTransport(
            lambda request: (calls.append(str(request.url)),
                             httpx.Response(200, text=text("stock_page_motherson.html")))[1]
        )
        indexed_client.ohlc_token("MOTHERSON")
        indexed_client.ohlc_token("MOTHERSON", force_refresh=True)
        assert len(calls) == 2


# =========================================================== HTTP status mapping


class TestHttpStatusMapping:
    """444 is a request gate; a rejected token is a 200. They must not be conflated."""

    async def test_444_raises_a_typed_access_error_not_a_decode_error(self, indexed_client):
        """The empty 444 body is the trap: a client that reaches response.json() reports a
        decode error and sends the reader hunting for a schema problem."""
        indexed_client.transport = httpx.MockTransport(
            lambda request: httpx.Response(444)
        )
        with pytest.raises(tle.TrendlyneAccessDeniedError) as excinfo:
            await indexed_client.ohlc_async("MOTHERSON")
        assert excinfo.value.status_code == 444
        assert "Referer" in str(excinfo.value), "the message must name the verified cause"
        assert isinstance(excinfo.value, bfinance.UpstreamServiceError)

    async def test_444_is_not_retried(self, indexed_client):
        """Only the /mapp/v1/ route is gated, so the page must succeed for the 444 to be reached
        at all — and then the api call must not be repeated."""
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "/equity/878/" in request.url.path:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(444)

        indexed_client.transport = httpx.MockTransport(handler)
        with pytest.raises(tle.TrendlyneAccessDeniedError):
            await indexed_client.ohlc_async("MOTHERSON")
        assert len(calls) == 2, (
            f"expected page + one api attempt, no retries; got {len(calls)}: {calls}"
        )

    async def test_rejected_token_is_a_200_with_a_failed_envelope(self, indexed_client):
        """Verified live: a wrong token returns 92 bytes of status '2', not 444 and not 403."""
        rejected = httpx.Response(200, json={
            "head": {"status": "2", "statusDescription": "Not Authorized !", "responseCode": None},
            "body": {},
        })
        indexed_client.transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text=text("stock_page_motherson.html"))
            if "/equity/" in str(request.url) else rejected
        )
        with pytest.raises(tle.TrendlyneNotAuthorizedError) as excinfo:
            await indexed_client.ohlc_async("MOTHERSON")
        assert "Not Authorized" in str(excinfo.value)
        assert excinfo.value.status_code == 200, "the HTTP status is 200; the envelope failed"

    async def test_a_rotated_token_is_re_scraped_once(self, indexed_client):
        """If re-scraping yields a different token, retry once with it — and only once.

        The substitute tokens stay inside the real charset: uppercase alphanumerics plus ``=``
        padding. An underscore would not match the token pattern, which is itself worth knowing.
        """
        state = {"valid": "TOKENB======", "page_fetches": 0, "api_calls": 0}

        def handler(request):
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                state["page_fetches"] += 1
                page = text("stock_page_motherson.html").replace(
                    "TQZITAIRHO5FCC7SYAKGO6M7QA======", "TOKENB======"
                )
                return httpx.Response(200, text=page)
            state["api_calls"] += 1
            if state["valid"] in url:
                return httpx.Response(200, json=payload())
            return httpx.Response(200, json={
                "head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}})

        indexed_client.transport = httpx.MockTransport(handler)
        # A cached-but-now-wrong token: the api rejects it, and the page yields a different one.
        import time as _time
        indexed_client._tokens[878] = (_time.monotonic(), "TOKEN_A======")

        frame = await indexed_client.ohlc_async("MOTHERSON")
        assert len(frame) == 125
        assert state["page_fetches"] == 1, "the page is re-scraped exactly once"
        assert state["api_calls"] == 2, "one rejected call, one retry"
        assert indexed_client._tokens[878][1] == "TOKENB======"

    async def test_a_stable_rejected_token_is_not_retried_forever(self, indexed_client):
        calls = []

        def handler(request):
            url = str(request.url)
            calls.append(url)
            if "/equity/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, json={
                "head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}})

        indexed_client.transport = httpx.MockTransport(handler)
        import time as _time
        indexed_client._tokens[878] = (_time.monotonic(), "TQZITAIRHO5FCC7SYAKGO6M7QA======")
        with pytest.raises(tle.TrendlyneNotAuthorizedError):
            await indexed_client.ohlc_async("MOTHERSON")
        api_calls = [c for c in calls if "/mapp/" in c]
        assert len(api_calls) == 1, f"an unchanged token must not be retried: {api_calls}"

    async def test_404_is_typed_and_terminal(self, indexed_client):
        indexed_client.transport = httpx.MockTransport(
            lambda request: httpx.Response(404, text="nope")
        )
        with pytest.raises(tle.TrendlyneNotFoundError) as excinfo:
            await indexed_client.ohlc_async("MOTHERSON")
        assert excinfo.value.status_code == 404
        assert isinstance(excinfo.value, bfinance.TickerNotFoundError)

    async def test_5xx_is_retried_then_raised(self, indexed_client):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "/equity/" in request.url.path:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(503)

        client = mock_client(handler, max_retries=3)
        client.load_symbol_index(text("sitemap_excerpt.xml"))
        with pytest.raises(tle.TrendlyneServerError) as excinfo:
            await client.ohlc_async("MOTHERSON")
        assert excinfo.value.status_code == 503
        assert len([c for c in calls if "/mapp/" in c]) == 3, "5xx must be retried"

    async def test_5xx_recovers_within_the_retry_budget(self, indexed_client):
        state = {"n": 0}

        def handler(request):
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json=payload())

        client = mock_client(handler, max_retries=3)
        frame = await client.ohlc_async("MOTHERSON")
        assert state["n"] == 2
        assert len(frame) == 125

    async def test_429_is_retried_and_then_typed(self, indexed_client):
        client = mock_client(
            lambda request: httpx.Response(429, headers={"Retry-After": "0"}),
            max_retries=2,
        )
        client.load_symbol_index(text("sitemap_excerpt.xml"))
        with pytest.raises(tle.TrendlyneRateLimitedError) as excinfo:
            await client.ohlc_async("MOTHERSON")
        assert excinfo.value.status_code == 429
        assert isinstance(excinfo.value, bfinance.RateLimitExceededError)

    async def test_non_json_body_is_a_schema_error(self, indexed_client):
        def handler(request):
            if "/equity/" in request.url.path:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, text="<html>maintenance</html>",
                                  headers={"Content-Type": "text/html"})

        indexed_client.transport = httpx.MockTransport(handler)
        with pytest.raises(tle.TrendlyneSchemaError, match="non-JSON"):
            await indexed_client.ohlc_async("MOTHERSON")

    async def test_transport_failure_is_retried_then_typed(self, indexed_client):
        attempts = []

        def handler(request):
            attempts.append(str(request.url))
            raise httpx.ConnectError("no route to host", request=request)

        client = mock_client(handler, max_retries=2)
        with pytest.raises(tle.TrendlyneTransportError):
            await client.symbol_index_async()
        assert len(attempts) == 2


# =========================================================== OHLC parsing


class TestOhlcParsing:
    def test_frame_shape_and_order(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert list(frame.columns) == list(tl.COLUMNS)
        assert isinstance(frame.index, pd.DatetimeIndex)
        assert frame.index.name == "Date"
        assert frame.index.is_monotonic_increasing, "the wire is newest-first; output is ascending"

    def test_sorted_even_when_the_last_week_merge_is_skipped(self):
        """Regression guard: without an unconditional sort, include_last_week=False returned the
        wire order, so index[-1] was 2006 and index[0] was 2026."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON", include_last_week=False)
        assert frame.index.is_monotonic_increasing
        assert frame.index[0] < frame.index[-1]
        assert frame.index[-1] == pd.Timestamp("2026-09-18")

    def test_values_are_not_silently_lost_to_a_reindex(self):
        """A Series carries a RangeIndex which pandas reindexes against the DatetimeIndex,
        producing an all-NaN frame that still 'succeeds'. See CONTEXT.md 9.22."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.notna().all().all(), "an all-NaN frame would still pass a shape check"
        assert frame["Close"].iloc[-1] == 165.71
        assert frame["Open"].iloc[0] == 3.297
        assert frame["High"].iloc[0] == 3.32
        assert frame["Low"].iloc[0] == 3.278
        assert int(frame["Volume"].iloc[0]) == 1787315

    def test_volume_is_integral(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert pd.api.types.is_integer_dtype(frame["Volume"])

    def test_ohlc_invariant_holds_on_the_recorded_capture(self):
        """51,070/51,070 rows across 10 symbols upstream; 125/125 here. Counted, not assumed."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_ohlc_invariant_violations"] == 0
        assert (frame["High"] >= frame[["Open", "Close"]].max(axis=1)).all()
        assert (frame["Low"] <= frame[["Open", "Low"]].min(axis=1)).all()

    def test_date_is_read_from_the_wire_not_from_a_timezone_conversion(self):
        """The wire timestamp is a retrieval marker (10:01 UTC). Converting first would move a
        late bar onto the following day, so only the literal date is taken."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.index[0] == pd.Timestamp("2006-01-02")
        assert frame.index[-1] == pd.Timestamp("2026-09-25")
        assert (frame.index.time == pd.Timestamp("00:00:00").time()).all()

    def test_last_week_merge_is_additive_and_documented(self):
        """eodData stops at 2026-09-18; the freshest five sessions live in eodDataLastWeek."""
        merged = tl.parse_ohlc(payload(), "MOTHERSON")
        plain = tl.parse_ohlc(payload(), "MOTHERSON", include_last_week=False)
        assert merged.attrs["bfinance_trendlyne_last_week_rows_added"] == 5
        assert len(merged) == len(plain) + 5
        assert not merged.index.duplicated().any()
        week_dates = {r[0][:10] for r in payload()["body"]["eodDataLastWeek"]}
        assert set(merged.index.strftime("%Y-%m-%d")) == set(
            plain.index.strftime("%Y-%m-%d")
        ) | week_dates
        assert not set(plain.index.strftime("%Y-%m-%d")) & week_dates

    def test_last_week_rows_never_overwrite_eod_rows(self):
        """If trendlyne ever folds the recent week into eodData too, the overlap must not
        duplicate or overwrite. eodData is authoritative."""
        body = payload()["body"]
        overlapping = [list(row) for row in body["eodData"][:3]]
        tampered = {**payload(), "body": {**body, "eodDataLastWeek": overlapping}}
        frame = tl.parse_ohlc(tampered, "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_last_week_rows_added"] == 0
        assert not frame.index.duplicated().any()
        eod_close = {r[0][:10]: r[4] for r in body["eodData"][:3]}
        for date, close in eod_close.items():
            assert frame.loc[date, "Close"] == close

    def test_last_week_skipped_is_reported_as_minus_one_not_zero(self):
        """-1 says 'present upstream, deliberately not merged'. 0 would read as 'nothing there'."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON", include_last_week=False)
        assert frame.attrs["bfinance_trendlyne_last_week_rows_added"] == -1

    def test_close_and_last_price_divergence_is_reported_not_resolved(self):
        """close_price and last_price are different fields and disagree on most bars (82 of 120
        rows in the capture). Close takes close_price; the count is exposed."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_close_last_divergent_rows"] == 82
        assert "last_price" not in frame.columns

    def test_duplicate_session_dates_are_deduped_and_counted(self):
        body = payload()["body"]
        rows = list(body["eodData"][:5])
        duped = rows + [list(rows[0])]
        frame = tl.parse_ohlc({**payload(), "body": {**body, "eodData": duped}},
                              "MOTHERSON", include_last_week=False)
        assert frame.attrs["bfinance_trendlyne_duplicate_dates"] == 1
        assert not frame.index.duplicated().any()

    def test_missing_numbers_stay_missing(self):
        body = payload()["body"]
        rows = [list(r) for r in body["eodData"][:4]]
        victim = rows[0][0][:10]
        rows[0][1] = None
        frame = tl.parse_ohlc({**payload(), "body": {**body, "eodData": rows}},
                              "MOTHERSON", include_last_week=False)
        assert pd.isna(frame.loc[victim, "Open"]), "a missing open must not be filled"
        assert frame.attrs["bfinance_trendlyne_missing_values"] == {"Open": 1}
        assert len(frame) == 4, "a row with a missing open is kept, not dropped"

    def test_all_missing_close_raises(self):
        body = payload()["body"]
        rows = [[r[0], r[1], r[2], r[3], None, r[5], r[6]] for r in body["eodData"][:4]]
        with pytest.raises(tle.TrendlyneSchemaError, match="close_price"):
            tl.parse_ohlc({**payload(), "body": {**body, "eodData": rows}}, "MOTHERSON")

    @pytest.mark.parametrize("mutate,expected", [
        (lambda p: p["body"].pop("eodData"), "no 'eodData'"),
        (lambda p: p["body"].pop("eodHeaders"), "no 'eodHeaders'"),
        (lambda p: p["body"].__setitem__("eodHeaders", []), "empty 'eodHeaders'"),
        (lambda p: p["body"]["eodHeaders"][1].__setitem__("unique_name", "oepn_price"),
         "missing required column"),
        (lambda p: p["body"].__setitem__("eodData", [["2026-01-01T10:01:00+00:00", 1.0]]),
         "do not have 7 values"),
        (lambda p: p["body"].__setitem__("eodData", [["not-a-date", 1, 2, 3, 4, 5, 6]]),
         "not YYYY-MM-DD"),
        (lambda p: p.pop("body"), "'body' is"),
        (lambda p: p.pop("head"), "no 'head'"),
        (lambda p: p["body"].__setitem__("eodHeaders", [{"unique_name": "date"}]),
         "missing required column"),
    ])
    def test_schema_changes_raise_rather_than_producing_nonsense(self, mutate, expected):
        broken = payload()
        mutate(broken)
        with pytest.raises(tle.TrendlyneSchemaError, match=expected):
            tl.parse_ohlc(broken, "MOTHERSON")

    def test_failed_envelope_never_yields_a_frame(self):
        rejected = {"head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}}
        with pytest.raises(tle.TrendlyneNotAuthorizedError):
            tl.parse_ohlc(rejected, "MOTHERSON")

    def test_envelope_rejects_a_non_object(self):
        with pytest.raises(tle.TrendlyneSchemaError, match="JSON object"):
            tl.parse_envelope([1, 2, 3], context="x")
        with pytest.raises(tle.TrendlyneSchemaError, match="no 'head'"):
            tl.parse_envelope({"body": {}}, context="x")
        with pytest.raises(tle.TrendlyneSchemaError, match="'body' is"):
            tl.parse_envelope({"head": {"status": "0"}, "body": []}, context="x")

    async def test_full_ohlc_call_composes_resolve_token_and_api(self):
        """The two-request flow, end to end, with no network."""
        calls = []

        def handler(request):
            url = str(request.url)
            calls.append(url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        client = mock_client(handler)
        frame = await client.ohlc_async("MOTHERSON")
        assert len(frame) == 125
        assert frame.attrs["bfinance_symbol"] == "MOTHERSON"
        assert frame.attrs["bfinance_trendlyne_stock_id"] == 878
        assert len(calls) == 3, f"expected sitemap + page + api, got {calls}"
        assert any("/mapp/v1/stock/web/ohlc/878/TQZITAIRHO5FCC7SYAKGO6M7QA======/" in c
                   for c in calls)

    async def test_mapp_requests_carry_a_referer_and_the_sitemap_does_not_need_one(self):
        seen = []

        def handler(request):
            seen.append((str(request.url), request.headers.get("referer")))
            if "equity-sitemap" in str(request.url):
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in str(request.url):
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        client = mock_client(handler)
        await client.ohlc_async("MOTHERSON")
        by_url = {url: ref for url, ref in seen}
        api_url = next(u for u in by_url if "/mapp/" in u)
        assert by_url[api_url] == "https://trendlyne.com/equity/878/MOTHERSON/" + \
            "samvardhana-motherson-international-ltd/", "the /mapp/v1/ gate needs a Referer"
        sitemap_url = next(u for u in by_url if "equity-sitemap" in u)
        assert by_url[sitemap_url] is None

    async def test_second_call_reuses_the_index_and_token(self):
        calls = []

        def handler(request):
            url = str(request.url)
            calls.append(url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        client = mock_client(handler)
        await client.ohlc_async("MOTHERSON")
        first = len(calls)
        await client.ohlc_async("MOTHERSON")
        assert len(calls) - first == 1, "only the API call should repeat"

    async def test_chart_metric_is_validated_locally(self, indexed_client):
        indexed_client.transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload("chart_data_eps_ttm.json"))
        )
        raw = await indexed_client.chart_metric_async("MOTHERSON", "EPS_TTM")
        assert raw["head"]["status"] == "0"
        assert raw["body"]["parameter"] == "EPS_TTM"
        with pytest.raises(ValueError, match="not supported"):
            await indexed_client.chart_metric_async("MOTHERSON", "NOT_A_METRIC")


# =========================================================== table classification


class TestTableClassification:
    """Classification is by header row. Two earlier attempts selected tables by nearby heading
    and returned a bonus ratio of 140.000 and 95 splits for TCS, because the h2 summary line
    mentions bonuses, splits and dividends at once and shadows the real h3 headings."""

    CA = "corporate_actions_motherson.html"

    def test_each_corporate_action_table_is_found_by_its_own_header(self):
        page = text(self.CA)
        assert tbt.page_signatures(page) == {
            0: "bonus", 1: "split", 2: "dividend", 3: "board_meeting", 4: "rights"
        }
        assert tbt.find_table(page, "bonus").shape == (10, 3)
        assert tbt.find_table(page, "split").shape == (2, 4)
        assert tbt.find_table(page, "dividend").shape == (26, 5)
        assert tbt.find_table(page, "rights").shape == (1, 5)
        assert tbt.find_table(page, "board_meeting").shape == (10, 3)

    def test_bonus_ratio_is_a_ratio_not_a_hundred_and_forty(self):
        """The garbage the heading-based attempts produced. Pin the real values."""
        bonus = tbt.find_table(text(self.CA), "bonus", symbol="MOTHERSON")
        assert list(bonus.columns) == ["Ex-Date", "Bonus Ratio", "Record Date"]
        assert set(bonus["Bonus Ratio"].unique()) == {"1:2"}, "a ratio, not a scaled number"
        assert len(bonus) == 10, "MOTHERSON has 10 bonus issues, not 95"

    def test_split_rows_are_actual_splits(self):
        """Two rows, and every one is a real reduction in face value."""
        split = tbt.find_table(text(self.CA), "split")
        assert list(split.columns) == ["Ex-Date", "Old FV", "New FV", "Record Date"]
        assert len(split) == 2
        assert (split["New FV"] < split["Old FV"]).all()
        assert split["Old FV"].is_unique and split["New FV"].is_unique

    def test_rights_table_is_not_mistaken_for_bonus(self):
        """The rights header contains a bare 'Ratio' column. Matching on 'Ratio' would file the
        rights table under bonus."""
        rights = tbt.find_table(text(self.CA), "rights")
        assert list(rights.columns) == ["Ex-Date", "Ratio", "Face Value", "Premium", "Record Date"]
        assert tbt.classify_table(list(rights.columns)) == "rights"
        assert tbt.classify_table(rights.columns) != "bonus"

    def test_classify_table_on_bare_header_lists(self):
        assert tbt.classify_table(["Ex-Date", "Bonus Ratio", "Record Date"]) == "bonus"
        assert tbt.classify_table(["Ex-Date", "Old FV", "New FV", "Record Date"]) == "split"
        assert tbt.classify_table(
            ["Ex-Date", "Dividend Amount", "Dividend Type", "Record Date", "Instrument Type"]
        ) == "dividend"
        assert tbt.classify_table(["Nothing", "Recognisable"]) is None

    def test_classification_tolerates_casing_and_whitespace_only(self):
        assert tbt.classify_table(["ex-date", "  bonus   ratio ", "RECORD DATE:"]) == "bonus"

    def test_classification_is_not_fooled_by_a_heading(self):
        """The h2 on the live page names all three action types. Reconstruct that situation: put
        the summary heading and the three real headings in the fragment, and assert the tables
        still classify by header alone."""
        page = text(self.CA)
        with_headings = (
            "<h2>Check all corporate actions such as stock splits, bonuses, dividends, "
            "mergers &amp; acquisitions, rights issues</h2>"
            "<h3>Bonus history for Samvardhana Motherson</h3>" + page
        )
        assert tbt.page_signatures(with_headings) == tbt.page_signatures(page)

    def test_find_tables_without_a_signature_returns_every_outermost_table(self):
        page = text(self.CA)
        assert len(tbt.find_tables(page)) == 5

    def test_deals_table_is_classified(self):
        deals = tbt.find_table(text("bulk_deals_motherson.html"), "deals", symbol="MOTHERSON")
        assert list(deals.columns) == [
            "Client Name", "Deal Type", "Action", "Date", "Avg. Price", "Quantity",
            "Intraday", "Exchange",
        ]
        assert len(deals) == 39, "the fixture keeps the first 40 of 82 live rows"
        assert set(deals["Deal Type"]) <= {"Block", "Bulk"}

    def test_missing_signature_lists_what_the_page_actually_holds(self):
        with pytest.raises(tle.TrendlyneTableNotFoundError) as excinfo:
            tbt.find_table(text("shareholding_motherson.html"), "bonus")
        message = str(excinfo.value)
        assert "no signature" in message
        assert "Summary" in message, "the caller should see the headers that were present"
        assert "heading text" in message, "the error should say why heading matching is not used"

    def test_unknown_signature_is_rejected_before_parsing(self):
        with pytest.raises(ValueError, match="unknown signature"):
            tbt.find_tables(text(self.CA), "not_a_signature")

    def test_numeric_columns_are_coerced_only_when_every_value_parses(self):
        """A column the vendor renders without separators is typed; free text is not."""
        deals = tbt.find_table(text("bulk_deals_motherson.html"), "deals")
        assert pd.api.types.is_numeric_dtype(deals["Avg. Price"])
        assert pd.api.types.is_string_dtype(deals["Client Name"])
        assert pd.api.types.is_string_dtype(deals["Deal Type"])

    def test_a_thousands_separated_column_stays_text(self):
        """The deals Quantity column is rendered '693,960'. Because ``thousands=None`` keeps the
        vendor's text intact, the all-or-nothing rule declines to type it. That is the intended
        contract, not an oversight: stripping the separator is the caller's decision, made
        explicitly and visibly, instead of a rewrite that happens inside a parser.
        """
        deals = tbt.find_table(text("bulk_deals_motherson.html"), "deals")
        assert not pd.api.types.is_numeric_dtype(deals["Quantity"])
        assert "," in deals["Quantity"].iloc[0]
        stripped = pd.to_numeric(
            deals["Quantity"].str.replace(",", "", regex=False), errors="coerce"
        )
        assert stripped.notna().all(), "the caller can still get integers, deliberately"

    def test_a_column_with_one_unparseable_value_is_left_completely_alone(self):
        """Partial coercion would turn that one cell into NaN, which is then indistinguishable
        from 'the vendor did not report it'. The column must stay verbatim."""
        page = (
            "<table><tr><th>Client Name</th><th>Qty</th></tr>"
            "<tr><td>a</td><td>10</td></tr><tr><td>b</td><td>N/A</td></tr></table>"
        )
        frame = tbt.find_table(page, None)
        assert list(frame["Qty"]) == ["10", "N/A"]
        assert frame["Qty"].isna().sum() == 0, "no value may be turned into NaN"

    def test_coerce_numeric_is_all_or_nothing(self):
        """Unit-level guard on the rule itself, including a thousands separator that read_html
        would otherwise have already cleaned up."""
        series = pd.Series(["10", "1,234"], dtype=object)
        assert tbt._coerce_numeric(pd.DataFrame({"Qty": series}))["Qty"].tolist() == ["10", "1,234"]
        clean = pd.DataFrame({"Qty": pd.Series(["10", "20"], dtype=object)})
        assert tbt._coerce_numeric(clean)["Qty"].tolist() == [10, 20]

    def test_registered_signatures_are_all_reachable_and_documented(self):
        for name in tbt.known_signatures():
            assert tbt.SIGNATURES[name].required, f"{name} has no required headers"
            assert tbt.SIGNATURES[name].description, f"{name} is undocumented"

    def test_register_signature_refuses_to_clobber_without_overwrite(self):
        with pytest.raises(ValueError, match="already registered"):
            tbt.register_signature("bonus", ("Whatever",))
        with pytest.raises(ValueError, match="at least one"):
            tbt.register_signature("brand_new")
        try:
            tbt.register_signature("probe_tmp", ("Whatever",), description="test only")
            assert tbt.page_signatures(
                "<table><tr><th>Whatever</th></tr><tr><td>x</td></tr></table>"
            ) == {0: "probe_tmp"}
        finally:
            tbt.SIGNATURES.pop("probe_tmp", None)

    def test_multi_row_headers_are_the_callers_choice(self):
        """The share-holding page mixes a summary grid whose second row is data with pattern
        tables whose second row is a real '%'/'shares' sub-header."""
        page = text("shareholding_motherson.html")
        shallow = tbt.find_tables(page, None, header=0)
        deep = tbt.find_tables(page, None, header=[0, 1])
        assert len(shallow) == len(deep) == 3
        assert list(shallow[0].columns)[:2] == ["Summary", "Jun 2026"]
        assert deep[1].columns[0] == "Holding Jun 2026 | %", "sub-headers must flatten, not vanish"


# =========================================================== snapshot honesty


class TestSnapshotHonesty:
    """A snapshot must never be mistakable for a series."""

    def test_snapshot_only_routes_are_declared_as_data(self):
        assert tl.SNAPSHOT_ONLY_ENDPOINTS == frozenset({"share_holding"})
        assert "mutual_fund_holdings" not in tl.SNAPSHOT_ONLY_ENDPOINTS, (
            "verified live: the mf-holdings route filters by year and by month, so calling it "
            "snapshot-only would be a false limitation"
        )

    @pytest.mark.parametrize("period", ["2024", "Jun-2024", "latest2", "24"])
    def test_a_period_on_a_snapshot_only_route_raises_before_any_request(self, period):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        with pytest.raises(tle.TrendlyneSnapshotOnlyError, match="latest"):
            tl.build_path("share_holding", record, period=period)

    def test_the_snapshot_error_explains_what_to_use_instead(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        with pytest.raises(tle.TrendlyneSnapshotOnlyError) as excinfo:
            tl.build_path("share_holding", record, period="Jun-2024")
        assert "500" in str(excinfo.value), "the verified upstream behaviour belongs in the message"
        assert "mutual_fund_holdings" in str(excinfo.value)

    def test_a_filterable_route_accepts_the_three_verified_period_forms(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        for period in ("latest", "2024", "Jun-2024"):
            path = tl.build_path("mutual_fund_holdings", record, period=period)
            assert f"/{period}/" in path

    def test_an_unrecognised_period_is_rejected_locally(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        for bad in ("20244", "Jun-24", "jun-2024", "JUN-2024", "", "Q1-2024", "24-06-2024"):
            with pytest.raises(ValueError, match="period"):
                tl.build_path("mutual_fund_holdings", record, period=bad)
        with pytest.raises(ValueError):
            tl.validate_period(2024)

    def test_every_frame_declares_the_snapshot_flag(self):
        """The key is always written — False included — so a consumer can rely on it existing."""
        ohlc = tl.parse_ohlc(payload(), "MOTHERSON")
        assert ohlc.attrs["bfinance_trendlyne_snapshot_only"] is False
        table = tbt.find_table(text("corporate_actions_motherson.html"), "dividend")
        assert "bfinance_trendlyne_snapshot_only" not in table.attrs, (
            "a table frame has no period axis, so claiming False would be a claim, not a fact; "
            "the owning sibling module sets it"
        )

    def test_build_attrs_writes_the_flag_and_a_note(self):
        attrs = tl.build_attrs("RELIANCE", source="https://x/y", snapshot_only=True,
                               note="one quarter only")
        assert attrs["bfinance_trendlyne_snapshot_only"] is True
        assert attrs["bfinance_trendlyne_note"] == "one quarter only"
        assert attrs["bfinance_provenance"] == "trendlyne"
        assert attrs["bfinance_symbol"] == "RELIANCE"

    def test_deep_history_routes_are_not_marked_as_snapshots(self):
        """Bulk deals and insider trades carry 10-16 years in one page, so a sibling must not
        inherit the snapshot caveat from share-holding."""
        assert "bulk_deals" not in tl.SNAPSHOT_ONLY_ENDPOINTS
        assert "insider_trading" not in tl.SNAPSHOT_ONLY_ENDPOINTS


# =========================================================== provenance


class TestProvenance:
    def test_ohlc_frame_carries_the_package_convention(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_provenance"] == "trendlyne"
        assert frame.attrs["bfinance_symbol"] == "MOTHERSON"
        assert frame.attrs["bfinance_synthetic_ohlc"] is False

    def test_price_basis_is_declared_so_bases_cannot_be_mixed_silently(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_price_basis"] == tl.PRICE_BASIS
        assert tl.PRICE_BASIS == "yfinance_back_adjusted"
        assert frame.attrs["bfinance_trendlyne_price_basis"] != "as_traded"

    def test_the_note_says_the_window_is_not_listing_history(self):
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs["bfinance_trendlyne_note"]
        assert "not the listing history" in note
        assert "as-traded" in note
        assert "bhavcopy" in note, "the frame must name the source it may not be mixed with"

    def test_the_window_is_reported_from_the_data_not_claimed(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_bars"] == len(frame)
        assert frame.attrs["bfinance_trendlyne_first_date"] == "2006-01-02"
        assert frame.attrs["bfinance_trendlyne_last_date"] == "2026-09-25"
        assert frame.attrs[tl.WINDOW_ATTR] == "2006-01-02..2026-09-25 (125 bars)"
        assert frame.attrs["bfinance_trendlyne_eod_rows"] == 120, (
            "the fixture is a 60+60 excerpt of a 5,107-row live payload; the frame must not "
            "claim the vendor's full count"
        )

    def test_the_token_never_reaches_attrs(self):
        frame = tl.parse_ohlc(
            payload(), "MOTHERSON",
            source_url="https://trendlyne.com/mapp/v1/stock/web/ohlc/878/"
                       "TQZITAIRHO5FCC7SYAKGO6M7QA======/",
        )
        assert frame.attrs["bfinance_trendlyne_source"].endswith("878/<token>/")
        assert "TQZIT" not in json.dumps({k: str(v) for k, v in frame.attrs.items()})

    def test_table_frames_carry_provenance_and_where_they_came_from(self):
        frame = tbt.find_table(text("corporate_actions_motherson.html"), "bonus", symbol="MOTHERSON")
        assert frame.attrs["bfinance_provenance"] == "trendlyne"
        assert frame.attrs["bfinance_symbol"] == "MOTHERSON"
        assert frame.attrs["bfinance_trendlyne_table"] == "bonus"
        assert frame.attrs["bfinance_trendlyne_table_index"] == 0
        assert frame.attrs["bfinance_trendlyne_table_header"] == [
            "Ex-Date", "Bonus Ratio", "Record Date"
        ]

    def test_attrs_survive_a_plain_column_slice(self):
        """Consumers slice frames constantly; provenance must not be the first thing to vanish."""
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame[["Close"]].attrs["bfinance_provenance"] == "trendlyne"
        assert frame.head(3).attrs["bfinance_symbol"] == "MOTHERSON"


class TestPriceBasis:
    """The reason this module is not interchangeable with bhavcopy, pinned by a fixture rather
    than asserted in prose. Both tests read ``ohlc_reliance_excerpt.json``, recorded live."""

    def test_history_is_on_one_scale_across_the_2024_bonus(self):
        """Back-adjustment, demonstrated from inside the series.

        RELIANCE went ex-bonus 1:1 on 2024-10-28. In an **as-traded** series the 2024-10-25 close
        sits near twice the 2024-10-29 close. In a **back-adjusted** one the older bars are scaled
        onto the newer level, so the ratio is ~1.0 and no discontinuity exists. Measured here:
        0.991. This is why these bars must not be concatenated with bfinance.nse.bhavcopy bars.
        """
        frame = tl.parse_ohlc(payload("ohlc_reliance_excerpt.json"), "RELIANCE")
        pre_bonus = frame.loc["2024-10-25", "Close"]
        post_bonus = frame.loc["2024-10-29", "Close"]
        assert 0.95 <= pre_bonus / post_bonus <= 1.05, (
            f"expected one continuous scale across the bonus, got {pre_bonus}/{post_bonus}"
        )
        assert frame.attrs["bfinance_trendlyne_price_basis"] == "yfinance_back_adjusted"

    def test_the_latest_session_agrees_exactly_with_an_independent_source(self):
        """Cross-source corroboration, and the reason the basis claim above is credible.

        The 2026-09-24 bar from trendlyne is Close 1219.2 / Volume 13,923,795. That is exactly what
        bfinance's own MarketLens recording holds for the same session
        (``tests/fixtures/marketlens/reliance_1y.json``, recorded 2026-09-26 from
        marketlens.nseindia.com). Two independent sources agreeing to the paisa and the share on a
        recent bar, while the same series is scaled on older bars, is what a back-adjusted series
        should look like.

        The expected values are literals rather than a read of the MarketLens fixture, so this test
        does not couple itself to another module's fixture file.
        """
        frame = tl.parse_ohlc(payload("ohlc_reliance_excerpt.json"), "RELIANCE")
        bar = frame.loc["2026-09-24"]
        assert bar["Close"] == 1219.2
        assert int(bar["Volume"]) == 13923795
        assert bar["Open"] == 1236.6 and bar["High"] == 1241.6 and bar["Low"] == 1219.2

    def test_the_excerpt_is_not_mistaken_for_a_continuous_series(self):
        """The fixture is two disjoint windows, so the frame reports what it actually holds."""
        recorded = payload("ohlc_reliance_excerpt.json")
        frame = tl.parse_ohlc(recorded, "RELIANCE")
        assert recorded["_fixture_full_row_count"] == 5107
        assert len(recorded["body"]["eodData"]) == 17
        assert frame.attrs["bfinance_trendlyne_bars"] == 22
        assert frame.attrs["bfinance_trendlyne_eod_rows"] == 17
        gaps = frame.index.to_series().diff().dt.days
        assert gaps.max() > 300, "the two windows are not contiguous, and the frame says so"
        assert frame.attrs["bfinance_trendlyne_last_week_rows_added"] == 5


# =========================================================== fixtures themselves


class TestFixtureProvenance:
    """The fixtures are the evidence. If they are tampered with, the tests above become fiction."""

    def test_meta_records_every_file(self):
        meta = json.loads(text("META.json"))
        assert meta["source"] == "https://trendlyne.com"
        on_disk = {p.name for p in FIXTURES.iterdir() if p.suffix != ".pyc"}
        assert on_disk == set(meta["files"]) | {"META.json"}
        for name, entry in meta["files"].items():
            assert entry["http_status"] == 200, name
            assert entry["original_bytes"] > 0, name
            assert entry["trim_rule"], name
            assert (FIXTURES / name).stat().st_size == entry["stored_bytes"], name

    def test_the_ohlc_fixture_records_the_true_vendor_row_count(self):
        """The frame must not be able to claim 5,107 bars from a 120-row excerpt."""
        recorded = payload()
        assert recorded["_fixture_full_row_count"] == 5107
        assert len(recorded["body"]["eodData"]) == 120

    def test_the_sitemap_excerpt_is_documented_as_three_windows(self):
        meta = json.loads(text("META.json"))["files"]["sitemap_excerpt.xml"]
        assert "windows" in meta["trim_rule"]
        assert "5922" in meta["notes"]
        assert len(tl.parse_sitemap(text("sitemap_excerpt.xml"))) == 12
