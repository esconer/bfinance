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
import hashlib
import json
import re
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
from bfinance.trendlyne import sectors as tls
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
    """A client with pacing disabled (the real 1 s robots floor would add nothing to a mock)."""
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
        # The cache entry is (fetched_at, token, classification) — the token and the sector come
        # off the same page fetch, so they are cached together.
        import time as _time
        indexed_client._tokens[878] = (_time.monotonic(), "TOKEN_A======", {})

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
        indexed_client._tokens[878] = (
            _time.monotonic(), "TQZITAIRHO5FCC7SYAKGO6M7QA======", {})
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

    def test_the_snapshot_only_policy_table_and_its_error_are_gone(self):
        """``SNAPSHOT_ONLY_ENDPOINTS`` and ``TrendlyneSnapshotOnlyError`` were removed as dead API.

        The set had to be a subset of ``ENDPOINTS`` to be enforceable, nothing in the package
        fetched a snapshot-only route, so it was an empty ``frozenset()`` and the guard in
        ``build_path`` could never fire. A public exception that cannot be raised is the same
        category of problem as the 14 endpoint templates deleted for exactly that reason: it makes
        the package look bigger than the code that runs.

        This asserts the removal is **total** - not merely that the names are unimportable, which a
        stale re-export in ``__all__`` would survive - and the negative control at the end is what
        stops it passing for the wrong reason.
        """
        import bfinance.trendlyne as pkg

        assert not hasattr(tl, "SNAPSHOT_ONLY_ENDPOINTS"), (
            "the policy table is back. A snapshot-only name that is not an addressable route can "
            "never be enforced, so an empty set plus a guard that cannot fire is exactly what the "
            "removal was for. Keep the 500 fact in the module docstring instead."
        )
        assert not hasattr(tle, "TrendlyneSnapshotOnlyError")
        assert not hasattr(pkg, "TrendlyneSnapshotOnlyError")
        assert "TrendlyneSnapshotOnlyError" not in tle.__all__
        assert "TrendlyneSnapshotOnlyError" not in pkg.__all__
        assert "SNAPSHOT_ONLY_ENDPOINTS" not in pkg.__all__
        # The attrs flag is a DIFFERENT thing and must survive: it is written on every frame and
        # says the frame is a single snapshot. Only the policy table and its error went.
        assert hasattr(tl, "SNAPSHOT_ONLY_ATTR")
        assert "SNAPSHOT_ONLY_ATTR" in pkg.__all__

        # Negative control. The two names asserted absent above really are the ones that were
        # removed: client.py still *mentions* the error by name, as a documented decision, while
        # errors.py must not define it. A name that never existed is still reported absent, so a
        # typo in the assertions above fails here instead of passing silently.
        client_source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
        assert "TrendlyneSnapshotOnlyError" in client_source, (
            "the removal should be visible in client.py as a documented decision, not a silent gap"
        )
        assert "TrendlyneSnapshotOnlyError" not in (
            PACKAGE_DIR / "errors.py"
        ).read_text(encoding="utf-8")
        assert not hasattr(tl, "SNAPSHOT_ONLY_ENDPOINTZ")
    def test_the_share_holding_500_is_recorded_where_the_route_can_be_wired(self):
        """The measured fact must survive the removal, or the next agent spends two requests
        rediscovering a guaranteed HTTP 500 and then has to re-invent the guard that went with it.
        """
        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
        assert "HTTP 500" in source
        assert "share-holding" in source, (
            "the snapshot-only route and its verified failure belong in the docstring that tells "
            "the next agent to add it to ENDPOINTS and restore a local guard"
        )
        assert "monthly-mutual-fund-share-holding" in source, (
            "the counter-example matters as much as the rule: that route genuinely filters by "
            "period, so a blanket snapshot guard would break it"
        )
        assert tl.SNAPSHOT_PERIOD == "latest"
        assert tl.validate_period("latest") == "latest"
    @pytest.mark.parametrize("period", ["2024", "Jun-2024", "latest2", "24"])
    def test_a_deleted_route_is_rejected_before_any_request(self, period):
        """A route the site serves but this package does not is an error, not a minted URL."""
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        with pytest.raises(ValueError) as excinfo:
            tl.build_path("share_holding", record, period=period)
        assert "share_holding" in str(excinfo.value)

    def test_the_unknown_endpoint_message_names_what_this_package_does_address(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        with pytest.raises(ValueError) as excinfo:
            tl.build_path("bulk_deals", record)
        message = str(excinfo.value)
        for name in ("stock_page", "ohlc", "chart_metric"):
            assert name in message, f"{name} is addressable and the message must say so"

    @pytest.mark.parametrize("name", ["stock_page", "ohlc", "chart_metric"])
    def test_every_addressable_route_builds(self, name):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        kwargs = {"token": "T"} if name == "ohlc" else {"metric": tl.METRICS[0]} \
            if name == "chart_metric" else {}
        path = tl.build_path(name, record, **kwargs)
        assert path.startswith("/") and path.endswith("/")

    def test_the_sitemap_only_fallback_is_not_mistaken_for_a_template(self):
        """``_SYMBOL_SEARCH_PATH`` is a query-string route, deliberately outside ``ENDPOINTS``."""
        assert "{query}" in tl._SYMBOL_SEARCH_PATH
        assert tl._SYMBOL_SEARCH_PATH not in tl.ENDPOINTS.values()

    def test_period_is_rejected_locally_on_every_addressable_route(self):
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        for bad in ("20244", "Jun-24", "jun-2024", "JUN-2024", "", "Q1-2024", "24-06-2024"):
            with pytest.raises(ValueError, match="period"):
                tl.build_path("stock_page", record, period=bad)
        with pytest.raises(ValueError):
            tl.validate_period(2024)

    def test_the_three_verified_period_forms_are_still_accepted(self):
        """``latest`` / a year / ``Mon-YYYY`` — the forms the site's period routes accept.

        No addressable route takes a period today, so this exercises :func:`validate_period`
        directly rather than through a deleted template. Wiring ``mutual_fund_holdings``, which
        genuinely filters by all three, is the first caller that needs it again.
        """
        for period in ("latest", "2024", "Jun-2024"):
            assert tl.validate_period(period) == period

    def test_every_frame_declares_the_snapshot_flag(self):
        """The key is always written — False included — so a consumer can rely on it existing."""
        ohlc = tl.parse_ohlc(payload(), "MOTHERSON")
        assert ohlc.attrs["bfinance_trendlyne_snapshot_only"] is False
        table = tbt.find_table(text("corporate_actions_motherson.html"), "dividend")
        assert "bfinance_trendlyne_snapshot_only" not in table.attrs, (
            "a table frame has no period axis, so claiming False would be a claim, not a fact; "
            "a caller composing fetch_text with find_table sets it via build_attrs"
        )

    def test_build_attrs_writes_the_flag_and_a_note(self):
        attrs = tl.build_attrs("RELIANCE", source="https://x/y", snapshot_only=True,
                               note="one quarter only")
        assert attrs["bfinance_trendlyne_snapshot_only"] is True
        assert attrs["bfinance_trendlyne_note"] == "one quarter only"
        assert attrs["bfinance_provenance"] == "trendlyne"
        assert attrs["bfinance_symbol"] == "RELIANCE"


# =========================================================== provenance


class TestProvenance:
    def test_ohlc_frame_carries_the_package_convention(self):
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_provenance"] == "trendlyne"
        assert frame.attrs["bfinance_symbol"] == "MOTHERSON"
        assert frame.attrs["bfinance_synthetic_ohlc"] is False

    def test_price_basis_names_what_is_applied_not_whose_convention_it_resembles(self):
        """The basis is a fact about this route, measured 2026-09-27 against bhavcopy.

        It used to be ``"yfinance_back_adjusted"``, which described a *source* rather than this
        route and was false for the whole demerger class. The label now says the two things that
        are true — as-traded, split/bonus adjusted — and the demerger exclusion is a separate
        boolean, because it is the part a consumer is most likely to get wrong.
        """
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_price_basis"] == tl.PRICE_BASIS
        assert tl.PRICE_BASIS == "as_traded_split_bonus_adjusted"
        assert frame.attrs["bfinance_trendlyne_demerger_applied"] is False
        assert tl.DEMERGER_APPLIED is False

    def test_price_basis_is_not_the_false_claim_it_used_to_be(self):
        """The regression guard for the actual bug: a false blanket claim, set unconditionally.

        ``"yfinance_back_adjusted"`` was wrong for every symbol measured. On ITC, the demerger
        name, trendlyne agrees with the exchange print (481.60 on 2025-01-03, same as bhavcopy)
        while yfinance says 463.74 — the trendlyne/yfinance close ratio is 1.038513 at every
        session of 2017, 2018, 2020, 2022 and 2024. So on a demerger it is the *exchange* that is
        unrewritten here. Anyone re-deriving the label must not land back on this string.
        """
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        basis = frame.attrs["bfinance_trendlyne_price_basis"]
        assert basis != "yfinance_back_adjusted"
        assert tl.PRICE_BASIS != "yfinance_back_adjusted"
        assert basis != "as_traded", (
            "bare 'as_traded' is also false: splits and bonuses ARE back-adjusted, which is why "
            "HDFCBANK and MOTHERSON miss bhavcopy on 0 of 62 sessions"
        )
        # The label must be machine-readable: lowercase, underscore-separated, no spaces.
        assert basis == basis.lower()
        assert " " not in basis and "-" not in basis

    def test_build_attrs_does_not_stamp_a_price_basis_it_never_measured(self):
        """The inverse hazard: a deals list or holdings grid has no price basis at all.

        ``build_attrs`` writes the snapshot flag unconditionally, but the basis keys belong to
        :func:`parse_ohlc` alone. Stamping ``as_traded_split_bonus_adjusted`` onto a table frame
        would be the same class of false claim as the value being corrected here.
        """
        attrs = tl.build_attrs("MOTHERSON", note="one quarter only")
        assert attrs["bfinance_trendlyne_snapshot_only"] is False
        assert tl.PRICE_BASIS_ATTR not in attrs
        assert tl.DEMERGER_APPLIED_ATTR not in attrs
        # ...and parse_ohlc, which does measure it, writes both.
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs[tl.PRICE_BASIS_ATTR] == tl.PRICE_BASIS
        assert frame.attrs[tl.DEMERGER_APPLIED_ATTR] is False

    def test_the_note_states_the_demerger_exclusion_and_which_joins_are_unsafe(self):
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs["bfinance_trendlyne_note"]
        assert "AS-TRADED" in note
        assert "DEMERGERS ARE NOT APPLIED" in note
        assert "split or bonus" in note
        assert "yfinance" in note, (
            "the note must name the source whose series it is *not* interchangeable with on a "
            "demerger, which is the case the old label got backwards"
        )
        assert "yfinance basis" not in note

    def test_the_note_does_not_claim_the_history_is_truncated_at_listing(self):
        """The note must claim neither a fixed vendor window NOR truncation at listing.

        This assertion has been wrong in both directions, once each, and the file is the
        record. It was first ``"not the listing history" in note``, which enforced a
        statement that was itself false. It was then swung to ``"truncated at the
        instrument's listing" in note``, justified by reasoning that was sound on the
        evidence then available: a 2023 IPO cannot have twenty years of bars, so the series
        must stop at the listing. That justification has now been falsified by direct
        measurement. Live on 2026-09-29, SBIN returned **2,222 bars and 45,561,075,413
        shares of volume dated before its 2015-01-15 NSE listing**, on a frame whose
        exchange attribute reads "NSE". PAYTM, IREDA and RELIANCE return zero pre-listing
        bars on the same route, so neither rule holds universally and the earlier claim was
        generalised from symbols that happened to agree with it.

        What is actually true is narrower: the start of the series is a vendor floor that
        this package has not verified against any exchange listing date, and for some
        instruments it sits before the NSE listing. The note has to say that much and no
        more. SBIN's own payload carries ``stockData[2] == "500112"`` as a BSE code beside
        the NSE symbol, so those bars are most plausibly BSE history filed under the NSE
        ticker -- plausible, **not verified**, and the note must not present them as either
        real or fabricated.
        """
        note = tl.parse_ohlc(payload(), "MOTHERSON").attrs["bfinance_trendlyne_note"]

        # The retired claim, in both directions.
        assert "truncated at the instrument's listing" not in note
        assert "not the listing history" not in note
        assert "vendor-fixed window" not in note

        # What replaces it: an unverified floor, named as such.
        assert "vendor floor" in note
        assert "NOT verified" in note
        assert "NOT the listing date" in note
        assert "Do not treat index[0] as the listing" in note

        # The measurement that retired the old claim has to travel with the note, or the
        # next reader generalises from whichever symbol they happened to test.
        assert "SBIN" in note
        assert "2,222" in note
        assert "45,561,075,413" in note
        assert "2015-01-15" in note

        # Unchanged from the previous revision, and still load-bearing: the bar count is
        # symbol-dependent, the basis label is as-traded, and the frame names the source it
        # must not be spliced with.
        assert "symbol-dependent" in note
        assert "as-traded" in note
        assert "bhavcopy" in note, "the frame must name the source it may not be mixed with"

    def test_the_frame_measures_pre_listing_rows_only_when_given_a_listing_date(self):
        """``-1`` and ``0`` are different answers and must not be collapsed.

        ``-1`` means *not measured*: the caller supplied no listing date, so the package has
        no basis for the claim. ``0`` means *measured, and there were none*. Collapsing them
        is how the original defect stood -- nothing on the frame ever contradicted the
        note, because the frame carried no counter-evidence at all.
        """
        frame = tl.parse_ohlc(payload(), "MOTHERSON")
        assert frame.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == -1
        assert frame.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] == -1
        assert frame.attrs[tl.LISTING_DATE_ATTR] is None

        # MOTHERSON listed on NSE on 2021-08-18 and the captured fixture holds 60 bars
        # before that date, so this is a real measurement rather than a constant.
        dated = tl.parse_ohlc(payload(), "MOTHERSON", listing_date="2021-08-18")
        assert dated.attrs[tl.ROWS_BEFORE_LISTING_ATTR] == 60
        assert dated.attrs[tl.VOLUME_BEFORE_LISTING_ATTR] > 0
        assert dated.attrs[tl.LISTING_DATE_ATTR] == "2021-08-18"
        assert len(dated) == len(frame), (
            "the rows are never dropped: the series is split/bonus adjusted, so clipping "
            "the pre-listing span would leave a discontinuity at the cut rather than "
            "remove one"
        )

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
    """Why these bars are not interchangeable with every other source, pinned from inside the
    series rather than asserted in prose. All three read ``ohlc_reliance_excerpt.json``, recorded
    live 2026-09-27.

    The basis is **as-traded, back-adjusted for splits and bonuses only, demergers not applied**.
    The test below demonstrates the *first* two halves from inside one series. The third half —
    demergers — cannot be shown from a RELIANCE fixture, because RELIANCE is not a demerger name;
    it was measured over ITC against bhavcopy and yfinance and is recorded in
    ``docs/UPSTREAM_SOURCES.md`` and in the module docstring. :class:`TestProvenance` above pins
    the label that encodes all three.
    """

    def test_history_is_on_one_scale_across_the_2024_bonus(self):
        """Split/bonus back-adjustment, demonstrated from inside the series.

        RELIANCE went ex-bonus 1:1 on 2024-10-28. In an **as-traded** series the 2024-10-25 close
        sits near twice the 2024-10-29 close. In a **back-adjusted** one the older bars are scaled
        onto the newer level, so the ratio is ~1.0 and no discontinuity exists. Measured here:
        0.991. This is the split/bonus half of the basis, and it is why these bars must not be
        spliced with bfinance.nse.bhavcopy bars across the event.
        """
        frame = tl.parse_ohlc(payload("ohlc_reliance_excerpt.json"), "RELIANCE")
        pre_bonus = frame.loc["2024-10-25", "Close"]
        post_bonus = frame.loc["2024-10-29", "Close"]
        assert 0.95 <= pre_bonus / post_bonus <= 1.05, (
            f"expected one continuous scale across the bonus, got {pre_bonus}/{post_bonus}"
        )
        assert frame.attrs["bfinance_trendlyne_price_basis"] == "as_traded_split_bonus_adjusted"
        assert frame.attrs["bfinance_trendlyne_demerger_applied"] is False

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


# =========================================================== no promises about unwritten code


class TestNoDeadScaffolding:
    """A name in this package is a claim that bfinance can do that thing.

    ``ENDPOINTS`` once held 17 templates, 14 of which no method ever built, and three module
    docstrings named sibling modules (``ownership``, ``deals``, ``corporate``) that do not exist
    on disk. Both are promises about unwritten code, and both were wrong in the same direction:
    they made the package look bigger than the code that runs. The names are gone; these tests are
    the guard that keeps them gone.
    """

    def _endpoint_names_referenced_in_the_module(self) -> set[str]:
        """Endpoint names the module actually builds, found by AST rather than by substring.

        ``ENDPOINTS`` is spelled out in a dict literal and read back by key, so a regex would have
        to guess; walking the tree for ``build_path("x"...)`` and ``ENDPOINTS["x"]`` is exact.

        One wrinkle: the two group routes are built from a **computed** name
        (``build_path(f"{group.kind}_page")``), because one dispatch serves both kinds. A literal
        walk cannot see that, so f-string first arguments are collected as regex patterns - literal
        parts escaped, each hole spelled ``.*`` - and a declared name counts as referenced when it
        matches one. Without this the check reports the group routes as dead strings, which is
        exactly the false positive that would tempt someone to delete working code.
        """
        tree = ast.parse((PACKAGE_DIR / "client.py").read_text(encoding="utf-8"))
        names: set[str] = set()
        patterns: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "build_path"
                and node.args
            ):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
                elif isinstance(first, ast.JoinedStr):
                    # Escape the literals and spell a hole as ".*": a bare "*" is a quantifier
                    # with nothing to repeat, and f"{kind}_page" puts the hole at position 0.
                    patterns.append(
                        "".join(
                            re.escape(part.value) if isinstance(part, ast.Constant) else ".*"
                            for part in first.values
                        )
                    )
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ENDPOINTS"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                names.add(node.slice.value)
        for name in set(tl.ENDPOINTS):
            if any(re.fullmatch(pattern, name) for pattern in patterns):
                names.add(name)
        return names

    def test_the_endpoint_reference_walk_is_not_vacuous(self):
        """Negative control for the walk above: it must find the known build sites.

        A tree-walk that silently matched nothing would make
        ``test_no_endpoint_template_is_a_dead_string`` fail loudly rather than pass wrongly, so the
        risk is the opposite one - a walk so loose it matches everything. Both are checked here:
        the walk finds the three literal routes, its f-string pattern is collected, and a name
        that no build site can produce is not matched.
        """
        found = self._endpoint_names_referenced_in_the_module()
        assert {"stock_page", "ohlc", "chart_metric"} <= found
        assert {"sector_page", "industry_page"} <= found
        assert not found & {"share_holding", "bulk_deals", "rights", "latest_news"}
        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
        assert 'f"{group.kind}_page"' in source, (
            "the f-string build site this walk depends on has been renamed; update the walk or "
            "build the group routes with a literal name"
        )
    def test_no_endpoint_template_is_a_dead_string(self):
        """Every template is built somewhere, or it is a claim nobody backs."""
        declared = set(tl.ENDPOINTS)
        referenced = self._endpoint_names_referenced_in_the_module()
        assert declared == referenced, (
            f"unreferenced templates are promises about unwritten code: {declared - referenced}. "
            f"Either give the route a fetch method in the same change, or delete the template."
        )

    def test_the_addressed_routes_are_the_ones_with_fetch_methods(self):
        """The whole package, stated as a set: 5 templates, and a method behind each.

        Two are stock routes and two are group routes, and the split is not cosmetic: a group URL
        carries no symbol segment and puts the id first, so a single template with an optional
        branch would match ``/equity/sector/20/...`` as a stock without complaining.
        """
        assert set(tl.ENDPOINTS) == {
            "stock_page", "ohlc", "chart_metric", "sector_page", "industry_page",
        }
        for method in ("ohlc", "ohlc_token", "chart_metric", "group_page", "constituents",
                       "taxonomy", "industry_aggregate"):
            assert callable(getattr(tl.TrendlyneClient, method)), method
        assert tl.GROUP_ENDPOINTS == {"sector_page", "industry_page"}
        assert not (tl.GROUP_ENDPOINTS & {"stock_page", "ohlc", "chart_metric"})
    async def test_each_template_is_the_url_the_client_actually_requests(self):
        """The strongest available form of the same claim: drive the public API offline and check
        that the request matched the template, rather than trusting that a string is used."""
        calls = []

        def handler(request):
            url = str(request.url)
            calls.append(url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            if "chart-data" in url:
                return httpx.Response(200, json=payload("chart_data_eps_ttm.json"))
            return httpx.Response(200, json=payload())

        client = mock_client(handler)
        record = await client.resolve_async("MOTHERSON")
        await client.chart_metric_async("MOTHERSON", "EPS_TTM")
        await client.ohlc_async("MOTHERSON")
        requested = {url.replace("https://trendlyne.com", "") for url in calls}
        for name, kwargs in (
            ("stock_page", {}),
            ("ohlc", {"token": "TQZITAIRHO5FCC7SYAKGO6M7QA======"}),
            ("chart_metric", {"metric": "EPS_TTM"}),
        ):
            assert tl.build_path(name, record, **kwargs) in requested, (
                f"{name} builds a path the client never requests"
            )

    async def test_each_group_template_is_the_url_the_client_actually_requests(self):
        """The group routes are built from a computed name, so prove them by driving them.

        This is the same claim as the stock-route version above, in the only form that works when
        the route name is ``f"{kind}_page"``: an AST walk cannot see it, so the request log is the
        evidence. It is also the only proof that the computed dispatch resolves to the right
        template for each kind rather than always picking one.
        """
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=text("sector_20_automobiles.html"))

        client = mock_client(handler)
        sector = tls.TrendlyneGroup("sector", 20, "automobiles-auto-components", "Autos")
        industry = tls.TrendlyneGroup("industry", 9, "auto-parts-equipment", "Auto Parts")
        await client.constituents_async(sector)
        await client.constituents_async(industry)
        requested = {url.replace("https://trendlyne.com", "") for url in calls}
        assert tl.build_path("sector_page", group=sector) in requested
        assert tl.build_path("industry_page", group=industry) in requested
        assert len(requested) == 2, f"one request per group, not more: {requested}"

    def test_a_group_route_will_not_swallow_a_symbol_or_the_other_kind(self):
        """The group route shape is id-first with no symbol segment; a mismatch is an error."""
        sector = tls.TrendlyneGroup("sector", 20, "automobiles-auto-components", "Autos")
        industry = tls.TrendlyneGroup("industry", 9, "auto-parts-equipment", "Auto Parts")
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        with pytest.raises(ValueError, match="takes a sector or industry"):
            tl.build_path("sector_page", record)
        with pytest.raises(ValueError, match="requires kind='sector'"):
            tl.build_path("sector_page", group=industry)
        with pytest.raises(ValueError, match="requires kind='industry'"):
            tl.build_path("industry_page", group=sector)
        with pytest.raises(ValueError, match="takes a symbol"):
            tl.build_path("ohlc", record, group=sector, token="T")
        with pytest.raises(ValueError, match="takes a symbol"):
            tl.build_path("stock_page")
        # The shape itself: id first, no symbol segment. If this ever becomes
        # /equity/{symbol}/{id}/{slug}/ the ids would silently move.
        assert tl.build_path("sector_page", group=sector) == (
            "/equity/sector/20/automobiles-auto-components/"
        )
        assert tl.build_path("industry_page", group=industry) == (
            "/equity/industry/9/auto-parts-equipment/"
        )
    def test_no_docstring_names_a_module_that_does_not_exist(self):
        """``__init__.py`` and ``client.py`` used to point at ``ownership``, ``deals`` and
        ``corporate`` — modules that were never written. A cross-reference to unwritten code is a
        promise; the audit that found this one will not run again on its own."""
        pattern = re.compile(r"bfinance\.trendlyne\.([a-z_]+)")
        referenced: dict[str, set[str]] = {}
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text_body = path.read_text(encoding="utf-8")
            for target in set(pattern.findall(text_body)):
                referenced.setdefault(path.name, set()).add(target)
        for source, targets in referenced.items():
            for target in targets:
                assert (PACKAGE_DIR / f"{target}.py").exists(), (
                    f"{source} refers to bfinance.trendlyne.{target}, which does not exist"
                )

    def test_the_package_docstring_lists_exactly_the_modules_on_disk(self):
        """A 'what is here' section is a manifest; a manifest that overstates is a false claim."""
        on_disk = {p.stem for p in PACKAGE_DIR.glob("*.py")} - {"__init__"}
        init_text = (PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
        for stem in on_disk:
            assert f":mod:`~bfinance.trendlyne.{stem}`" in init_text, (
                f"{stem}.py is on disk but the package docstring does not list it"
            )
        listed = set(re.findall(r":mod:`~bfinance\.trendlyne\.([a-z_]+)`", init_text))
        assert listed == on_disk, (
            f"the manifest lists {listed - on_disk}, which is not a module in this package. It "
            f"used to list ownership/deals/corporate this way -- modules that were never written."
        )


# =========================================================== robots: the member route is gated


class TestRobotsGate:
    """A path trendlyne's robots.txt disallows must not be fetched by default.

    ``/member/api/ac_snames/?term=`` sits under the **broad** ``Disallow: */member/``. It was the
    ETF fallback inside ``resolve_any``, so it was reachable from any ``ohlc()`` call for a symbol
    the equity sitemap omitted. That is a live violation in shipped code, and closing it costs
    something real: the sitemap carries no ETF at all, so with the gate off those symbols are
    unreachable. The tests below pin both halves - the refusal and the cost - because closing a
    violation while hiding what it cost would just move the dishonesty.
    """

    def test_the_disallowed_path_and_its_rule_are_recorded_as_facts(self):
        path = tl._SYMBOL_SEARCH_PATH.split("?")[0]
        assert path in tl.ROBOTS_DISALLOWED_ROUTES
        assert tl.ROBOTS_DISALLOWED_ROUTES[path] == "Disallow: */member/"
        assert tl.MEMBER_SEARCH_FLAG == "allow_member_search"

    def test_the_narrow_member_rule_does_not_cover_this_path(self):
        """The broad rule is what forbids it - worth being exact about.

        robots.txt carries a *second* member rule, ``*/member/api/ac_snames/stock/``, which covers
        a sibling endpoint. It does not match ``?term=``. So citing it would be the wrong defence
        for the right behaviour, and the constant must cite the rule that actually applies.
        """
        path = tl._SYMBOL_SEARCH_PATH.split("?")[0]
        assert not path.endswith("/stock/")
        assert tl.ROBOTS_DISALLOWED_ROUTES[path] != "Disallow: */member/api/ac_snames/stock/"

    def test_the_gate_is_off_by_default(self):
        assert tl.TrendlyneClient().allow_member_search is False
        assert tl.TrendlyneClient(allow_member_search=False).allow_member_search is False
        assert tl.TrendlyneClient(allow_member_search=True).allow_member_search is True

    def test_search_remote_raises_a_typed_error_naming_the_conflict(self, client):
        with pytest.raises(tle.TrendlyneRobotsDisallowedError) as excinfo:
            client.search_remote("NIFTYBEES")
        message = str(excinfo.value)
        assert "/member/api/ac_snames/" in message
        assert "Disallow: */member/" in message
        assert tl.MEMBER_SEARCH_FLAG in message, (
            "the message must name the switch; a caller blocked by policy needs the way to unblock"
        )
        assert isinstance(excinfo.value, tle.TrendlyneError)
        assert isinstance(excinfo.value, bfinance.UpstreamServiceError)

    def test_the_refusal_costs_no_request(self):
        """Raised locally, before a socket opens - honouring robots has to be free."""
        calls = []
        client = mock_client(lambda r: calls.append(str(r.url)) or httpx.Response(200, json=[]))
        with pytest.raises(tle.TrendlyneRobotsDisallowedError):
            client.search_remote("NIFTYBEES")
        assert calls == [], f"a disallowed path was requested: {calls}"

    def test_it_raises_rather_than_returning_an_empty_list(self, client):
        """An empty list is the wrong answer twice over.

        trendlyne answers a genuine miss with ``[{"headline": "No matches found"}]``, so ``[]``
        would be indistinguishable from "no such instrument" - and the caller would conclude the
        ETF does not exist. It does.
        """
        with pytest.raises(tle.TrendlyneRobotsDisallowedError):
            client.search_remote("NIFTYBEES")

    def test_the_per_call_flag_overrides_a_closed_constructor(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json=[
                {"k": 1588, "id": "NIFTYBEES", "slugname": "niftybees"},
            ])

        client = mock_client(handler)  # constructor gate left closed
        with pytest.raises(tle.TrendlyneRobotsDisallowedError):
            client.search_remote("NIFTYBEES")
        assert calls == []
        found = client.search_remote("NIFTYBEES", allow_member_search=True)
        assert [r.symbol for r in found] == ["NIFTYBEES"]
        assert len(calls) == 1, "the opt-in costs exactly the one request it authorises"

    def test_the_constructor_flag_opens_the_route_for_resolve_any(self):
        def handler(request):
            return httpx.Response(200, json=[
                {"k": 1588, "id": "NIFTYBEES", "slugname": "niftybees"},
            ])

        client = mock_client(
            handler,
            allow_member_search=True,
        )
        client.load_symbol_index(text("sitemap_excerpt.xml"))
        record = client.resolve_any("NIFTYBEES")
        assert record.symbol == "NIFTYBEES"
        assert record.stock_id == 1588
        assert client.symbol_resolution == "member_search"

    def test_resolve_any_skips_the_fallback_explicitly_and_records_it(self, indexed_client):
        """The skip is a stated fact in two places, never a silent miss."""
        with pytest.raises(tle.TrendlyneSymbolNotFoundError) as excinfo:
            indexed_client.resolve_any("NIFTYBEES")
        message = str(excinfo.value)
        assert "NOT tried" in message, "the fallback was skipped, and that must be said out loud"
        assert "Disallow: */member/" in message
        assert "may well exist on trendlyne" in message, (
            "the caller must be able to tell 'we will not ask' from 'it does not exist'"
        )
        assert indexed_client.symbol_resolution == "skipped_robots_disallowed"

    def test_resolve_any_records_the_route_that_actually_worked(self, indexed_client):
        assert indexed_client.resolve_any("MOTHERSON").symbol == "MOTHERSON"
        assert indexed_client.symbol_resolution == "sitemap"

    def test_the_recorded_sitemap_excerpt_carries_no_etf(self, indexed_client):
        """What the 12-entry recorded excerpt can prove, which is not much - and that is the point.

        The strong claim (the *full* 5,922-entry document holds no ETF) rests on a measurement too
        large to fixture, so it is pinned as a documented fact instead. This asserts the weak half
        that the excerpt can carry, and the next test asserts the strong half is still written down
        where a caller will meet it.
        """
        index = tl.parse_sitemap(text("sitemap_excerpt.xml"))
        etf_like = [s for s in index if "BEES" in s or s.endswith("ETF") or s.startswith("SETF")]
        assert etf_like == [], f"the excerpt's 12 entries include an ETF: {etf_like}"

    def test_the_etf_coverage_cost_is_written_down_not_hidden(self):
        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
        assert "no ETF is reachable" in source, (
            "closing the robots violation makes every ETF unreachable on the default path. That "
            "is a real coverage loss and the module must keep saying so."
        )
        for name in ("NIFTYBEES", "BANKBEES", "GOLDBEES"):
            assert name in source
        # ...and the sitemap side of the claim, so the cost is traceable to a measurement.
        assert "5,922" in source

    def test_only_the_gated_method_can_reach_the_disallowed_path(self):
        """Structural: the template is formatted in exactly one function, and that one is gated.

        A new caller added later would show up here as a second ``.format()`` site, which is the
        point - this is the check that stops the gate being quietly bypassed, rather than a note
        that documents the bypass afterwards. It walks the AST for the enclosing function rather
        than matching names, so a rename cannot quietly satisfy it.
        """
        import ast as _ast

        source = (PACKAGE_DIR / "client.py").read_text(encoding="utf-8")
        tree = _ast.parse(source)
        parents = {}
        for node in _ast.walk(tree):
            for child in _ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing(node) -> str:
            while node in parents:
                node = parents[node]
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    return node.name
            return "<module>"

        format_sites = [
            node for node in _ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_SYMBOL_SEARCH_PATH"
        ]
        assert len(format_sites) == 1, (
            f"the disallowed template is built in {len(format_sites)} places; each needs the gate"
        )
        site = format_sites[0]
        assert enclosing(site) == "search_remote_async", (
            f"the fetch moved to {enclosing(site)!r}; that function must call "
            f"_require_member_search before it"
        )

        # ...and inside that function the gate comes first, the fetch second.
        fn = site
        while not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            fn = parents[fn]
        gate_lines = [
            n.lineno for n in _ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_require_member_search"
        ]
        fetch_lines = [
            n.lineno for n in _ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "fetch_json_async"
        ]
        assert len(gate_lines) == 1, "the gate must be checked exactly once in the fetch path"
        assert len(fetch_lines) == 1
        assert gate_lines[0] < fetch_lines[0], (
            "the gate has to run before the request, not after it"
        )
    async def test_the_ohlc_frame_records_how_the_symbol_was_resolved(self, indexed_client):
        """The degradation is visible on a frame that otherwise succeeded, not only on a failure."""
        def handler(request):
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson_breadcrumb.html")
                                      + text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        indexed_client.transport = httpx.MockTransport(handler)
        frame = await indexed_client.ohlc_async("MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_symbol_resolution"] == "sitemap"


# =========================================================== classification, for free


class TestSectorClassification:
    """Sector and industry, off the page the OHLC token is already read from."""

    def test_the_breadcrumb_carries_id_slug_and_name_for_both(self):
        found = tls.parse_classification(text("stock_page_motherson_breadcrumb.html"))
        assert set(found) == {"sector", "industry"}
        assert (found["sector"].group_id, found["sector"].slug) == (
            20, "automobiles-auto-components"
        )
        assert found["sector"].name == "AUTOMOBILES & AUTO COMPONENTS"
        assert (found["industry"].group_id, found["industry"].slug) == (9, "auto-parts-equipment")
        assert found["industry"].name == "AUTO PARTS & EQUIPMENT"

    def test_the_group_route_puts_the_id_first_and_has_no_symbol_segment(self):
        found = tls.parse_classification(text("stock_page_motherson_breadcrumb.html"))
        assert found["sector"].path == "/equity/sector/20/automobiles-auto-components/"
        assert found["industry"].path == "/equity/industry/9/auto-parts-equipment/"
        # The stock route is the opposite shape. If these ever unify, these are the assertions
        # that notice.
        record = tl.parse_sitemap(text("sitemap_excerpt.xml"))["MOTHERSON"]
        assert record.page_path == "/equity/878/MOTHERSON/samvardhana-motherson-international-ltd/"

    def test_the_classification_label_is_the_only_field_name(self):
        """There is no data-* attribute and no embedded JSON, so the label is the discriminator.

        Proved by deletion rather than asserted: strip the ``SECTOR :`` label and the sector
        disappears while the untouched industry survives. That is exactly why the label had to be
        the key - a parser keyed on the URL would still have found the sector, and would have
        found it by guessing.
        """
        page = text("stock_page_motherson_breadcrumb.html")
        assert "data-sector" not in page and "data-industry" not in page
        stripped = page.replace("SECTOR : AUTOMOBILES &amp; AUTO COMPONENTS", "AUTOMOBILES")
        assert stripped != page, "the label really was removed from the fixture"
        found = tls.parse_classification(stripped)
        assert "sector" not in found
        assert found["industry"].group_id == 9, "the untouched industry is still found"
    def test_the_non_classification_breadcrumbs_are_ignored(self):
        """MARKETS and the symbol's own item are breadcrumbs too, and neither is a group."""
        found = tls.parse_classification(text("stock_page_motherson_breadcrumb.html"))
        assert len(found) == 2
        assert all(g.kind in ("sector", "industry") for g in found.values())
        # The symbol's own breadcrumb has no label at all, so it cannot be mistaken for one.
        page = text("stock_page_motherson_breadcrumb.html")
        assert "<span property=\"name\">MARKETS</span>" in page
        assert "SAMVARDHANA MOTHERSON INTERNATIONAL LTD." in page

    def test_a_labelled_breadcrumb_whose_href_disagrees_raises(self):
        """A label saying SECTOR on an industry URL is a contradiction, not a hint to pick one."""
        contradictory = (
            '<ol class="tl_breadcrumb" vocab="https://schema.org/" typeof="BreadcrumbList">'
            '<li><a href="https://trendlyne.com/equity/industry/9/auto-parts-equipment/" '
            'property="item" typeof="WebPage">'
            '<span property="name">SECTOR : AUTO PARTS &amp; EQUIPMENT</span></a></li></ol>'
        )
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tls.parse_classification(contradictory)
        assert "sector" in str(excinfo.value)
        assert "id comes first" in str(excinfo.value)

    def test_the_two_sources_disagree_on_case_and_agree_on_id(self):
        """trendlyne spells the same sector two ways; the id is the thing to key on.

        The stock-page breadcrumb is caps, the sector-page select is title case. Both are kept
        verbatim - folding case would be inventing a spelling the site does not use - so the test
        pins the disagreement and the agreement rather than hiding it.
        """
        from_breadcrumb = tls.parse_classification(text("stock_page_motherson_breadcrumb.html"))
        from_select = tls.parse_taxonomy(text("sector_20_automobiles.html"))
        sector_breadcrumb, sector_select = from_breadcrumb["sector"], from_select["sector:20"]
        assert sector_breadcrumb.group_id == sector_select.group_id == 20
        assert sector_breadcrumb.slug == sector_select.slug
        assert sector_breadcrumb.name == "AUTOMOBILES & AUTO COMPONENTS"
        assert sector_select.name == "Automobiles & Auto Components"
        assert sector_breadcrumb.name != sector_select.name, (
            "if trendlyne ever unifies the casing this test should be revisited, not deleted"
        )

    async def test_classification_costs_no_extra_request(self, indexed_client):
        """The whole point: the page is already being fetched for the token."""
        def counting(breadcrumb: bool):
            calls = []

            def handler(request):
                url = str(request.url)
                calls.append(url)
                if "equity-sitemap" in url:
                    return httpx.Response(200, text=text("sitemap_excerpt.xml"))
                if "/equity/878/" in url:
                    # The breadcrumb goes FIRST: the token fixture is a mid-document fragment, so
                    # appending a complete <ol> after it nests wrongly and lxml drops it.
                    page = (
                        text("stock_page_motherson_breadcrumb.html")
                        if breadcrumb else ""
                    ) + text("stock_page_motherson.html")
                    return httpx.Response(200, text=page)
                return httpx.Response(200, json=payload())

            client = mock_client(handler)
            client.load_symbol_index(text("sitemap_excerpt.xml"))
            return client, calls

        with_breadcrumb, calls_with = counting(True)
        without_breadcrumb, calls_without = counting(False)
        frame = await with_breadcrumb.ohlc_async("MOTHERSON")
        await without_breadcrumb.ohlc_async("MOTHERSON")
        assert len(calls_with) == len(calls_without), (
            "reading the classification must not add a request: "
            f"{len(calls_with)} vs {len(calls_without)}"
        )
        assert frame.attrs["bfinance_trendlyne_classification"] == "present"

    async def test_the_ohlc_frame_carries_id_slug_and_name_for_each(self, indexed_client):
        def handler(request):
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson_breadcrumb.html")
                                      + text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        indexed_client.transport = httpx.MockTransport(handler)
        frame = await indexed_client.ohlc_async("MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_sector"] == {
            "id": 20, "slug": "automobiles-auto-components",
            "name": "AUTOMOBILES & AUTO COMPONENTS",
        }
        assert frame.attrs["bfinance_trendlyne_industry"] == {
            "id": 9, "slug": "auto-parts-equipment", "name": "AUTO PARTS & EQUIPMENT",
        }

    async def test_the_keys_are_written_even_when_the_breadcrumb_is_absent(self, indexed_client):
        """A missing key reads as 'not applicable'; ``None`` plus a status string does not."""
        def handler(request):
            url = str(request.url)
            if "equity-sitemap" in url:
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in url:
                return httpx.Response(200, text=text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        indexed_client.transport = httpx.MockTransport(handler)
        frame = await indexed_client.ohlc_async("MOTHERSON")
        assert frame.attrs["bfinance_trendlyne_classification"] == "absent"
        assert frame.attrs["bfinance_trendlyne_sector"] is None
        assert frame.attrs["bfinance_trendlyne_industry"] is None
        assert frame.attrs["bfinance_trendlyne_symbol_resolution"] == "sitemap"

    async def test_a_warm_token_still_reports_the_classification(self, indexed_client):
        """Token and classification are cached in one entry; caching the token alone would
        silently make the classification cost a second request on every later call."""
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "equity-sitemap" in str(request.url):
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            if "/equity/878/" in str(request.url):
                return httpx.Response(200, text=text("stock_page_motherson_breadcrumb.html")
                                      + text("stock_page_motherson.html"))
            return httpx.Response(200, json=payload())

        indexed_client.transport = httpx.MockTransport(handler)
        token, first = await indexed_client.ohlc_token_async("MOTHERSON")
        page_fetches = sum(1 for c in calls if "/equity/878/" in c)
        assert page_fetches == 1
        token2, second = await indexed_client.ohlc_token_async("MOTHERSON")
        assert sum(1 for c in calls if "/equity/878/" in c) == page_fetches, (
            "a warm cache must not re-fetch the page to answer the classification"
        )
        assert token2 == token
        assert first == second and first["sector"].group_id == 20

    async def test_the_strict_door_raises_where_the_ohlc_path_reports(self, indexed_client):
        """Two doors, two behaviours, on purpose: the product differs."""
        def handler(request):
            if "equity-sitemap" in str(request.url):
                return httpx.Response(200, text=text("sitemap_excerpt.xml"))
            return httpx.Response(200, text=text("stock_page_motherson.html"))

        indexed_client.transport = httpx.MockTransport(handler)
        with pytest.raises(tle.TrendlyneSchemaError, match="breadcrumb"):
            await indexed_client.classification_async("MOTHERSON")
        # The token itself is unaffected: the product of ohlc_token_async is the token.
        token, classification = await indexed_client.ohlc_token_async("MOTHERSON")
        assert token and classification == {}


# =========================================================== the taxonomy


class TestSectorTaxonomy:
    def test_the_vocabulary_is_harvested_whole_from_one_page(self):
        taxonomy = tls.parse_taxonomy(text("sector_20_automobiles.html"))
        assert tls.taxonomy_counts(taxonomy) == {
            "sectors": 31, "industries": 133, "total": 164,
        }
        sector_ids = sorted(int(k.split(":")[1]) for k in taxonomy if k.startswith("sector:"))
        industry_ids = sorted(int(k.split(":")[1]) for k in taxonomy if k.startswith("industry:"))
        assert sector_ids == list(range(1, 32)), "31 sectors, ids 1-31, contiguous"
        assert industry_ids[0] == 2 and industry_ids[-1] == 284
        assert industry_ids != list(range(2, 285)), "133 industries over ids 2-284, so not contiguous"
        assert len(industry_ids) == 133

    def test_every_option_becomes_a_group_with_a_clean_display_name(self):
        taxonomy = tls.parse_taxonomy(text("sector_20_automobiles.html"))
        assert taxonomy["sector:20"] == tls.TrendlyneGroup(
            "sector", 20, "automobiles-auto-components", "Automobiles & Auto Components",
        )
        assert taxonomy["industry:9"] == tls.TrendlyneGroup(
            "industry", 9, "auto-parts-equipment", "Auto Parts & Equipment",
        )
        # The "Sector:"/"Industry:" prefix is a field name, not part of the name.
        assert not any(g.name.lower().startswith(("sector", "industry")) for g in taxonomy.values())

    def test_no_id_is_hardcoded_the_ids_move_with_the_page(self):
        """Proof by mutation: renumber the select and the parser follows.

        A hardcoded id would survive this and keep returning 20. The parser has no way to know 20
        except by reading it, which is the property worth having.
        """
        page = text("sector_20_automobiles.html")
        renumbered = page.replace(
            "/equity/sector/20/automobiles-auto-components/", "/equity/sector/77/renamed-sector/"
        ).replace("/equity/industry/9/auto-parts-equipment/", "/equity/industry/88/renamed-industry/")
        moved = tls.parse_taxonomy(renumbered)
        assert "sector:77" in moved and "sector:20" not in moved
        assert moved["sector:77"].slug == "renamed-sector"
        assert "industry:88" in moved and "industry:9" not in moved
        # And the original page still says 20, so the substitution really did move it.
        assert "sector:20" in tls.parse_taxonomy(page)

    def test_the_group_a_page_is_about_is_the_option_it_marks_selected(self):
        assert tls.declared_group(text("sector_20_automobiles.html")) == tls.TrendlyneGroup(
            "sector", 20, "automobiles-auto-components", "Automobiles & Auto Components",
        )

    def test_a_page_with_no_select_raises_rather_than_returning_an_empty_vocabulary(self):
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tls.parse_taxonomy(text("corporate_actions_motherson.html"))
        assert tls.TAXONOMY_SELECT_ID in str(excinfo.value)
        assert "only place trendlyne publishes the group ids" in str(excinfo.value)

    def test_a_group_refuses_a_bad_kind_or_id(self):
        with pytest.raises(ValueError, match="kind must be"):
            tls.TrendlyneGroup("index", 1, "x", "X")
        with pytest.raises(ValueError, match="positive integer"):
            tls.TrendlyneGroup("sector", 0, "x", "X")

    async def test_the_vocabulary_costs_exactly_one_request(self, client):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=text("sector_20_automobiles.html"))

        client.transport = httpx.MockTransport(handler)
        sector = tls.TrendlyneGroup("sector", 20, "automobiles-auto-components", "Autos")
        taxonomy = await client.taxonomy_async(sector)
        assert len(calls) == 1, "the whole vocabulary must cost one request, not one per group"
        assert len(taxonomy) == 164
        assert calls[0].endswith("/equity/sector/20/automobiles-auto-components/")


# =========================================================== constituents


class TestSectorConstituents:
    @pytest.fixture
    def sector_page(self) -> str:
        return text("sector_20_automobiles.html")

    @pytest.fixture
    def constituents(self, sector_page):
        return tls.parse_constituents(sector_page)

    # ---------------------------------------------------------------- the two signatures

    def test_the_page_has_exactly_two_tables_and_each_gets_its_own_signature(self, sector_page):
        assert tbt.page_signatures(sector_page) == {
            0: "sector_constituents", 1: "sector_industry_aggregate",
        }
        assert tls.CONSTITUENTS_SIG != tls.AGGREGATE_SIG
        assert tls.CONSTITUENTS_SIG in tbt.SIGNATURES
        assert tls.AGGREGATE_SIG in tbt.SIGNATURES
        assert [s.name for s in tls.load_signatures()] == [
            tls.CONSTITUENTS_SIG, tls.AGGREGATE_SIG
        ]

    def test_the_signatures_are_mutually_exclusive_on_the_real_headers(self, sector_page):
        """Each requires a header group the other does not have, so neither can claim both tables.

        Both tables' first header cell is ``Name``-ish, so a signature keyed on that word would
        match both. Neither one uses it. This is the assertion that keeps them apart.
        """
        headers = [
            [c.get_text(" ", strip=True) for c in
             t.find("tr").find_all(["th", "td"])]
            for t in tbt.find_table_elements(sector_page, tls.CONSTITUENTS_SIG)
        ] + [
            [c.get_text(" ", strip=True) for c in
             t.find("tr").find_all(["th", "td"])]
            for t in tbt.find_table_elements(sector_page, tls.AGGREGATE_SIG)
        ]
        constituents_header, aggregate_header = headers[0], headers[1]
        assert tbt.classify_table(constituents_header) == tls.CONSTITUENTS_SIG
        assert tbt.classify_table(aggregate_header) == tls.AGGREGATE_SIG
        for signature in (tls.CONSTITUENTS_SIG, tls.AGGREGATE_SIG):
            groups = tbt.SIGNATURES[signature].required
            other = constituents_header if signature == tls.AGGREGATE_SIG else aggregate_header
            for group in groups:
                assert not any(alias in {tbt.normalize_header(h) for h in other} for alias in group), (
                    f"{signature} could also match the other table via {group}"
                )

    def test_a_bare_name_header_matches_neither(self):
        """The negative control for the mutual-exclusion claim above.

        ``Name`` alone - which is the aggregate table's actual first header - is not enough for
        either signature. Without this, a future loosening of either one would still pass the
        real-header test whenever the page kept its other columns.
        """
        assert tbt.classify_table(["Name", "Day Change %"]) is None
        assert tbt.classify_table(["Name"]) is None
        assert tbt.classify_table(["Stock Name"]) is None
        assert tbt.classify_table(
            ["Name", "Day Change %", "No. of Companies", "Advances", "Declines",
             "Advances/Declines Ratio", "Industry Score"]
        ) == tls.AGGREGATE_SIG
        assert tbt.classify_table(
            ["Stock Name", "3M Price Chart", "Market Cap", "LTP Last Traded Price"]
        ) == tls.CONSTITUENTS_SIG

    def test_heading_text_cannot_choose_between_them(self, sector_page):
        """Selecting by heading is this repo's known failure mode, so the page is checked for it.

        The sector page's ``<title>``/``<h1>`` say "Sector Analysis" once, for the whole page, and
        the trimmed fixture drops them - which is the point: a parser that needed them could not
        have produced this frame at all.
        """
        assert "Sector Analysis" not in sector_page
        assert tbt.classify_table(["Stock Name", "Market Cap", "LTP Last Traded Price"]) \
            == tls.CONSTITUENTS_SIG

    def test_the_aggregate_block_is_a_frame_of_its_own(self, sector_page):
        aggregate = tls.parse_industry_aggregate(sector_page)
        assert list(aggregate.columns)[:3] == ["Name", "Day Change %", "No. of Companies"]
        assert len(aggregate) == 5
        assert aggregate.attrs["bfinance_trendlyne_table"] == tls.AGGREGATE_SIG

    def test_an_industry_page_has_no_aggregate_block(self, sector_page):
        """The block is a sector-page thing; asking on the wrong page raises rather than lies."""
        without = sector_page.split("</table>")[0] + "</table>"
        with pytest.raises(tle.TrendlyneTableNotFoundError):
            tls.parse_industry_aggregate(without)

    # ---------------------------------------------------------------- the two countings

    def test_the_aggregate_block_sums_to_the_constituent_row_count(self, sector_page, constituents):
        """Two independent server-side counts agreeing, asserted.

        The block's per-industry company counts must sum to the constituent table's row count. If
        the page were paginated, capped, or assembled from a stale cache, these would drift - and
        the drift is the only symptom a truncated list has.
        """
        aggregate = tls.parse_industry_aggregate(sector_page)
        counts = [int(v) for v in aggregate["No. of Companies"]]
        assert counts == [7, 14, 147, 13, 29]
        assert sum(counts) == 210
        assert sum(counts) == constituents.row_count

    def test_the_aggregate_names_the_industry_the_recorded_stock_belongs_to(self, sector_page):
        """Ties the 147 to a symbol, using two small fixtures instead of a 1.6 MB industry page.

        MOTHERSON's own breadcrumb puts it in industry 9 / "Auto Parts & Equipment", and the sector
        page's aggregate block gives that industry 147 companies. Industry 9's own page was
        separately observed to list exactly 147 rows (recorded in META.json notes) - a second,
        independent server-side count, which would otherwise need its own fixture to assert.
        """
        group = tls.parse_classification(text("stock_page_motherson_breadcrumb.html"))["industry"]
        assert (group.group_id, group.name) == (9, "AUTO PARTS & EQUIPMENT")
        aggregate = tls.parse_industry_aggregate(sector_page)
        row = aggregate[aggregate["Name"] == "Auto Parts & Equipment"]
        assert len(row) == 1
        assert int(row["No. of Companies"].iloc[0]) == 147

    # ---------------------------------------------------------------- completeness

    def test_all_210_constituents_are_returned(self, constituents):
        assert constituents.row_count == 210
        assert len(constituents.frame) == 210
        assert constituents.frame["Symbol"].nunique() == 210
        assert list(constituents.frame.columns) == [
            "Symbol", "Company", "Stock ID", "Market Cap", "Weight %", "LTP", "Page", "Sparkline",
        ]

    def test_the_sector_total_is_the_sum_of_the_rows(self, constituents):
        assert constituents.sector_market_cap == pytest.approx(3660716.0526652, rel=1e-12)
        assert constituents.frame["Market Cap"].sum() == pytest.approx(
            constituents.sector_market_cap, rel=1e-12
        )

    def test_weights_are_exact_and_partition_the_sector(self, constituents):
        weights = constituents.frame.set_index("Symbol")["Weight %"]
        assert weights.sum() == pytest.approx(100.0, abs=1e-9)
        assert weights["MARUTI"] == pytest.approx(10.3621, abs=1e-4)
        assert weights["M&M"] == pytest.approx(10.3098, abs=1e-4)
        assert weights["BAJAJ-AUTO"] == pytest.approx(8.6131, abs=1e-4)
        assert weights["MOTHERSON"] == pytest.approx(4.7777, abs=1e-4)
        # The weight is exactly the ratio, not a rounded copy of it.
        row = constituents.frame.set_index("Symbol").loc["MARUTI"]
        assert row["Weight %"] == pytest.approx(row["Market Cap"] / 3660716.0526652 * 100)

    def test_the_top_four_are_the_four_largest(self, constituents):
        top4 = list(constituents.frame.nlargest(4, "Market Cap")["Symbol"])
        assert top4 == ["MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT"]
        assert constituents.frame["Weight %"].sum() > 34.0

    # ---------------------------------------------------------------- the & fix

    def test_unescaping_happens_before_percent_decoding(self):
        """The order is the whole fix, so it is asserted on the helper, not just on the frame."""
        raw = "https://trendlyne.com/equity/807/M&amp;M/mahindra-mahindra-ltd/"
        assert tls._decoded(raw) == "https://trendlyne.com/equity/807/M&M/mahindra-mahindra-ltd/"
        # Percent-decoding alone leaves the entity behind, which matches no sitemap symbol.
        from urllib.parse import unquote

        assert "M&amp;M" in unquote(raw)
        assert "M&M" in tls._decoded(raw)

    def test_the_four_hostile_symbols_survive(self, constituents):
        """Four rows share a trait that a naive pattern drops: two hold ``&``, two hold ``-``.

        The brief listed all four as escaped-ampersand cases. Measured on the page, only two hrefs
        actually carry an escaped ``&`` (``ARE&amp;M`` and ``M&amp;M``); ``BAJAJ-AUTO`` and
        ``MMRUBBR-B`` carry a hyphen and no ampersand at all. All four are dropped by a symbol
        pattern of ``[A-Z0-9]+``, so all four are asserted - and the *count* of escaped ones is 2,
        not 4.
        """
        symbols = set(constituents.frame["Symbol"])
        for name in ("ARE&M", "M&M", "BAJAJ-AUTO", "MMRUBBR-B"):
            assert name in symbols, f"{name} vanished from the sector"
        assert constituents.escaped_ampersand_rows == 2, (
            "exactly two hrefs carry an escaped ampersand; if this reads 0 or 4, re-measure "
            "rather than adjusting the assertion"
        )
        escaped = set(constituents.frame[constituents.frame["Symbol"].str.contains("&")]["Symbol"])
        assert escaped == {"ARE&M", "M&M"}

    def test_a_naive_symbol_pattern_would_lose_four_constituents(self, sector_page):
        """Negative control: prove the tolerance is load-bearing rather than decorative."""
        import re as _re

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sector_page, "lxml")
        table = tbt.find_table_elements(sector_page, tls.CONSTITUENTS_SIG)[0]
        anchors = table.find_all("a", class_="stockrow")
        assert len(anchors) == 210
        naive = _re.compile(r"/equity/\d+/([A-Z0-9]+)/")
        lost = [
            a["href"] for a in anchors
            if naive.search(tls._decoded(a["href"])) is None
        ]
        assert len(lost) == 4, f"expected 4 symbols a [A-Z0-9]+ pattern cannot read: {lost}"
        for href in lost:
            assert any(name in href for name in ("ARE&M", "M&M", "BAJAJ-AUTO", "MMRUBBR-B"))
        assert soup is not None

    def test_the_ampersand_rows_carry_the_right_ids(self, constituents):
        """The id comes from the href, so the two escaped rows must not be off by a segment."""
        rows = constituents.frame.set_index("Symbol")
        assert rows.loc["M&M", "Stock ID"] == 807
        assert rows.loc["ARE&M", "Stock ID"] == 68
        assert rows.loc["M&M", "Page"] == "/equity/807/M&M/mahindra-mahindra-ltd/"
        assert rows.loc["M&M", "Company"] == "Mahindra & Mahindra"

    # ---------------------------------------------------------------- the row-cap guard

    def test_a_real_count_is_not_flagged(self, constituents):
        assert constituents.suspiciously_capped is False
        assert 210 % tls.SUSPICIOUS_ROW_MULTIPLE != 0

    def test_a_round_count_is_flagged_because_the_site_caps_at_500(self):
        """The live precedent: the insider-trading page returns exactly 500 and may be capped.

        A truncated frame looks exactly like a complete one, so the count travels with the frame
        and the round number is flagged. A flag rather than a raise, because a genuine 500-name
        sector is possible and refusing to return a frame would be trading one wrong answer for a
        different one.
        """
        assert tls.SUSPICIOUS_ROW_MULTIPLE == 100
        result = tls.ConstituentResult(
            frame=tls.parse_constituents(text("sector_20_automobiles.html")).frame,
            group=None, row_count=500, sector_market_cap=1.0, escaped_ampersand_rows=2,
        )
        assert result.suspiciously_capped is True
        assert tls.ConstituentResult(
            frame=result.frame, group=None, row_count=0, sector_market_cap=1.0,
            escaped_ampersand_rows=0,
        ).suspiciously_capped is False, "an empty page is a different problem, not a cap"

    def test_a_500_row_page_is_flagged_end_to_end(self, sector_page):
        """The flag has to fire on a real parse, not just on a hand-built result."""
        page = _synthetic_sector_page(rows=500)
        result = tls.parse_constituents(page)
        assert result.row_count == 500
        assert result.suspiciously_capped is True
        attrs = tls.constituent_attrs(result)
        assert attrs["bfinance_trendlyne_constituent_rows"] == 500
        assert attrs["bfinance_trendlyne_rows_suspiciously_capped"] is True
        assert "capped" in attrs["bfinance_trendlyne_note"]

    def test_the_count_and_the_flag_travel_with_the_frame(self, constituents):
        attrs = tls.constituent_attrs(constituents, source_url="https://trendlyne.com/x/")
        assert attrs["bfinance_provenance"] == "trendlyne"
        assert attrs["bfinance_trendlyne_table"] == tls.CONSTITUENTS_SIG
        assert attrs["bfinance_trendlyne_constituent_rows"] == 210
        assert attrs["bfinance_trendlyne_rows_suspiciously_capped"] is False
        assert attrs["bfinance_trendlyne_weight_sum_percent"] == pytest.approx(100.0, abs=1e-9)
        assert attrs["bfinance_trendlyne_escaped_ampersand_rows"] == 2
        assert attrs["bfinance_trendlyne_group"] == "sector:20"
        assert attrs["bfinance_trendlyne_source"] == "https://trendlyne.com/x/"

    # ---------------------------------------------------------------- the market-cap unit

    def test_the_market_cap_unit_is_not_asserted(self, constituents):
        """The column header carries no unit, and the share-count test does not resolve one.

        Dividing the market cap by the row's own last-traded price must give a share count. It
        does not: the implied values span 0.13 to 1055 and 37 of 210 fall below 1 crore. So the
        unit is recorded as unverified and the weight is the contract.
        """
        attrs = tls.constituent_attrs(constituents)
        assert attrs["bfinance_trendlyne_market_cap_unit"] == "unverified"
        assert "UNIT is not asserted" in attrs["bfinance_trendlyne_note"]
        assert "Weight %" in attrs["bfinance_trendlyne_note"]
        # The price column is genuine, so the failure is the ratio's, not the price's.
        assert constituents.frame.set_index("Symbol").loc["MOTHERSON", "LTP"] == pytest.approx(
            165.71
        )

    def test_the_implied_share_count_really_is_unusable(self, constituents):
        """The measurement behind the decision, pinned so a future agent can see why."""
        frame = constituents.frame.dropna(subset=["LTP"])
        implied = frame["Market Cap"] / frame["LTP"]
        assert len(frame) == 210
        assert implied.min() < 1.0
        assert (implied < 1.0).sum() == 37, "37 of 210 are impossible as a crore share count"
        assert implied.max() > 1000.0
        # ...while the ranking is plainly a market-cap ranking, so the column means what it says.
        assert list(frame.nlargest(3, "Market Cap")["Symbol"]) == ["MARUTI", "M&M", "BAJAJ-AUTO"]

    # ---------------------------------------------------------------- the sparkline

    def test_every_row_carries_a_sparkline_left_uninterpreted(self, constituents):
        """The sparkline is weekly and its length is **not** a constant, so none is asserted.

        Measured on the recorded page: 183 of 210 rows carry 14 points and 27 carry fewer - 13, 12,
        11, 10, 9, 8, 7, 5 and 2, all of them recently-listed names (BIMETAL, PAEL, JAINEX,
        VELJAN, ...). A 14-point window cannot exist for a company that listed last month, so
        "every row has 14 points" would be false and a parser that padded to 14 would be
        fabricating history.
        """
        frame = constituents.frame
        lengths = [len(tls.sparkline_points(v)) for v in frame["Sparkline"]]
        assert min(lengths) >= 2, "a sparkline with fewer than 2 points is not a sparkline"
        assert max(lengths) == 14, "14 weekly points is the full window"
        assert lengths.count(14) == 183
        assert len(set(lengths)) > 1, (
            "if this ever became uniformly 14 the point-count distribution below should be "
            "re-measured, not deleted"
        )

    def test_the_last_sparkline_point_is_the_live_price_on_the_same_row(self, constituents):
        """A real relationship between two independently-rendered values, on every row."""
        for _, row in constituents.frame.iterrows():
            points = tls.sparkline_points(row["Sparkline"])
            assert points, row["Symbol"]
            assert points[-1] == pytest.approx(row["LTP"]), (
                f"{row['Symbol']}: sparkline ends at {points[-1]} but LTP is {row['LTP']}"
            )

    def test_the_payload_is_carried_verbatim_with_its_dates(self, constituents):
        """Both lists are preserved and neither is interpreted; the dates are the page's own."""
        import json as _json

        raw = constituents.frame.set_index("Symbol").loc["MOTHERSON", "Sparkline"]
        payload = _json.loads(raw)
        assert set(payload) == {"v", "d"}
        assert len(payload["v"]) == len(payload["d"]) == 14
        assert payload["d"][0] == "03-Jul-2026"
        assert payload["d"][-1] == "25-Sep-2026 03:31 PM (IST)", (
            "the final label is a live snapshot, not a session - carried through unchanged"
        )
        assert payload["v"][-1] == 165.71
        assert tls.sparkline_points(None) == []
        assert tls.sparkline_points("not json") == []
        assert tls.sparkline_points('{"v": "not a list"}') == []
    # ---------------------------------------------------------------- refusals

    def test_a_row_with_no_market_cap_raises(self, sector_page):
        page = sector_page.replace(' data-parameter="mcap_q"', ' data-x="mcap_q"', 1)
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tls.parse_constituents(page)
        message = str(excinfo.value)
        assert "mcap_q" in message
        assert "the total" in message, (
            "the reason to raise is that a dropped row would change the sector total every other "
            "weight divides by"
        )
    def test_a_duplicate_symbol_raises(self, sector_page):
        """Two rows for one symbol would double-count it in the sector total.

        The mutation gives ACE's row the MARUTI symbol while keeping its own id consistent, so the
        id-disagreement check does not fire first and the duplicate check is what is exercised.
        """
        page = sector_page.replace(
            "/equity/24/ACE/action-construction-equipment-ltd/",
            "/equity/24/MARUTI/action-construction-equipment-ltd/",
            1,
        )
        assert page != sector_page
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tls.parse_constituents(page)
        assert "twice" in str(excinfo.value)
        assert "double-count" in str(excinfo.value)
    def test_a_row_whose_two_ids_disagree_raises(self, sector_page):
        page = sector_page.replace('data-stockpk="842"', 'data-stockpk="99999"', 1)
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tls.parse_constituents(page)
        assert "data-stockpk" in str(excinfo.value)

    def test_an_unparseable_market_cap_raises(self, sector_page):
        page = sector_page.replace(
            'data-order="14606.745312000"', 'data-order="n/a"', 1
        )
        with pytest.raises(tle.TrendlyneSchemaError, match="not a number"):
            tls.parse_constituents(page)

    def test_a_header_with_no_body_raises(self, sector_page):
        head = sector_page[: sector_page.index("</thead>") + len("</thead>")]
        page = head + "<tbody></tbody></table>"
        with pytest.raises(tle.TrendlyneSchemaError, match="truncated page"):
            tls.parse_constituents(page)

    def test_the_aggregate_block_is_required_to_be_a_sector_page(self):
        client = tl.TrendlyneClient(min_request_interval_ms=0.0)
        industry = tls.TrendlyneGroup("industry", 9, "auto-parts-equipment", "Auto Parts")
        import asyncio

        with pytest.raises(ValueError, match="only appears on a sector page"):
            asyncio.run(client.industry_aggregate_async(industry))

    # ---------------------------------------------------------------- via the client

    async def test_the_client_returns_the_frame_with_its_attrs(self, client):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=text("sector_20_automobiles.html"))

        client.transport = httpx.MockTransport(handler)
        sector = tls.TrendlyneGroup("sector", 20, "automobiles-auto-components", "Autos")
        frame = await client.constituents_async(sector)
        assert len(frame) == 210
        assert frame.attrs["bfinance_trendlyne_constituent_rows"] == 210
        assert frame.attrs["bfinance_trendlyne_rows_suspiciously_capped"] is False
        assert frame.attrs["bfinance_trendlyne_market_cap_unit"] == "unverified"
        assert frame.attrs["bfinance_trendlyne_group"] == "sector:20"
        assert len(calls) == 1
        assert calls[0].endswith("/equity/sector/20/automobiles-auto-components/")

    def test_group_page_refuses_anything_that_is_not_a_group(self, client):
        import pytest as _pytest

        with _pytest.raises(ValueError, match="must be a TrendlyneGroup"):
            client.group_page("sector:20")


def _synthetic_sector_page(rows: int = 500) -> str:
    """A minimal page carrying the constituent signature and ``rows`` data rows.

    Built rather than trimmed, because the point is a row count the site would never produce. The
    market caps differ per row so the weights are not all identical, which keeps the parse honest.
    """
    body = "".join(
        f'<tr><td class="fs09rem" data-order="Synthetic {i} Ltd.">'
        f'<a class="stockrow" data-stockpk="{1000 + i}" '
        f'href="https://trendlyne.com/equity/{1000 + i}/SYN{i}/synthetic-{i}-ltd/">SYN{i}</a></td>'
        f"<td class=\"cen\"><div data-sparkline='{{\"v\": [1.0, 2.0], \"d\": [\"a\", \"b\"]}}'></div></td>"
        f'<td class="rightAlgn" data-order="{i + 1}.0" data-parameter="mcap_q" '
        f'data-stockpk="{1000 + i}"><span class="column-value">{i + 1}</span></td>'
        f'<td class="rightAlgn" data-order="10.0" data-parameter="currentPrice" '
        f'data-stockpk="{1000 + i}"><span class="column-value">10</span></td></tr>'
        for i in range(rows)
    )
    return (
        '<select id="JS_diviGroupSelector">'
        '<option selected value="https://trendlyne.com/equity/sector/20/automobiles-auto-components/">'
        "Sector: Automobiles &amp; Auto Components</option></select>"
        "<table><thead><tr>"
        "<th>Stock Name</th><th>3M Price Chart</th><th>Market Cap</th>"
        "<th>LTP Last Traded Price</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
    )

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
            # Assert the sha of the *normalised* content, never `stat().st_size`. Git checks these
            # out with CRLF on Windows and LF elsewhere, so a raw size assertion passes in the
            # working tree where Python wrote the file and fails on any fresh clone. The same trap
            # was found in the screener fixture set first, where a checkout turned 42,258 recorded
            # bytes into 43,906 on disk. `.gitattributes` pins `eol=lf` for tests/fixtures; this
            # stays correct even where that is not honoured, and the sha detects any real edit.
            raw = (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")
            assert hashlib.sha256(raw).hexdigest() == entry["stored_sha256"], (
                f"{name} does not match the content META.json describes"
            )
            assert len(raw) == entry["stored_bytes"], name
            assert entry["stored_line_endings"] == "lf", name

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
