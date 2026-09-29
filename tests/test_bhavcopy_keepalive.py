"""
``BhavcopyClient`` must share one keep-alive connection, and a client is bound to a loop.

The defect
----------
``_get_archive`` opened a fresh ``httpx.AsyncClient`` per request, so a walk over N trading
days paid N TCP+TLS handshakes. Measured live against NSE over five trading-day archives:

    fresh client per request   19.94 s total   3.99 s per file
    one client, keep-alive      6.58 s total   1.32 s per file

2.67 s of every 3.99 s was connection setup, not NSE. The class docstring already claimed
sharing an instance was *preferred* while the implementation did the opposite; these tests
pin the implementation to the docstring.

The trap
--------
An ``httpx.AsyncClient`` is not a process-level object. Its connection pool, sockets and
transport belong to the event loop that first performs I/O with it. This matters here
because the module-level singleton ``_default_client()`` is also driven from
``market/ohlcv.py`` by way of ``await asyncio.to_thread(bhavcopy_range, ...)``, and
``_run_async`` answers that with a fresh ``asyncio.run()`` -- a new loop -- every single
call. A naive "stash one client on the instance" is therefore not merely untidy, it is
broken, and broken in a way a test written inside a single loop cannot see.

Both halves of that were measured on this machine (Python 3.13, Windows, Proactor loop)
against a real ``ThreadingTCPServer`` before any of this was implemented:

    run 1   200 b'HELLO'
    run 2   RuntimeError('Event loop is closed')
    run 3   200 b'HELLO'          <- recovers, because run 2 discarded the dead socket

The second failure is worse than the first, and is why the fix is shaped the way it is.
``await client.aclose()`` from the *new* loop raises the same error, because
``ProactorSocketTransport.close()`` calls ``loop.call_soon`` on the dead loop:

    File "anyio/_backends/_asyncio.py", line 1397, in aclose
        self._transport.close()
    File "asyncio/proactor_events.py", line 110, in close
        self._loop.call_soon(self._call_connection_lost, None)
    File "asyncio/base_events.py", line 557, in _check_closed
        raise RuntimeError('Event loop is closed')

So a client can only ever be closed by the loop that built it, and a loop that has died
takes its sockets with it -- ``ResourceWarning: unclosed socket`` and ``unclosed
transport`` at collection time, long after the call that leaked them returned. "Detect the
loop changed, then close the old client" is not a fix; it is the leak, with extra steps.

What the implementation does instead, and what these tests hold it to
--------------------------------------------------------------------
* The client is opened lazily, inside whichever loop is running, and keyed on that loop.
* It is closed by ``_keepalive``'s ``finally`` -- on that loop, while that loop is alive.
* Users are reference counted, so ``asyncio.gather`` over several calls on one loop cannot
  let the first one to finish close a client the others are still reading from.
* ``_get_archive`` is self-contained: one call, one acquire, one release. The range walk
  wraps its whole loop in an outer ``_keepalive``, which is where the N requests stop
  paying N handshakes.

Offline by construction. Every request in this file is served by a transport installed
under ``httpx.AsyncClient``, and the ones that read data replay whole archives recorded
from NSE under ``tests/fixtures/bhavcopy/`` through the real parser. Nothing here needs
the network, and no test is marked ``live``.
"""

import asyncio
import gc
import pathlib
import warnings

import httpx
import pytest

from bfinance.nse import bhavcopy as bc

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bhavcopy"

#: Whole archives as NSE served them. Provenance in ``tests/fixtures/bhavcopy/META.json``.
ARCHIVES = {
    "20260924": (FIXTURES / "udiff_cm_20260924_full.csv.zip").read_bytes(),
    "20260925": (FIXTURES / "udiff_cm_20260925_full.csv.zip").read_bytes(),
}

#: Five weekdays with two archived days and three 404s, so the walk is 5 requests and the
#: sparse case is covered by the same assertion as the dense one.
WALK = ("2026-09-21", "2026-09-25")

PAYLOAD = b"ZIP-BYTES-NOT-REALLY"


def fast_client(**kwargs) -> bc.BhavcopyClient:
    """A client with pacing disabled.

    ``min_request_interval_ms=0.0`` makes ``_throttle_pacing`` compute ``wait_sec = 0.0``
    unconditionally -- ``elapsed_ms`` is never negative -- so these tests neither sleep nor
    perturb the process-global pacing clock other tests share. The 250 ms default is a
    deliberate shipping decision and is not touched here.
    """
    kwargs.setdefault("min_request_interval_ms", 0.0)
    return bc.BhavcopyClient(**kwargs)


# --------------------------------------------------------------------- transports


class _ArchiveTransport(httpx.AsyncBaseTransport):
    """Serves recorded NSE archives by URL stamp. 404 for anything not in ``archives``.

    ``handle_async_request`` yields once, the way a real socket read does. This is not
    decoration: a handler that never suspends lets ``asyncio.gather`` run its tasks
    strictly one after another, so the concurrency test below would measure serial
    execution and pass for the wrong reason.
    """

    def __init__(self, archives):
        self.archives = dict(archives)
        self.urls = []
        self.headers = []

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        self.headers.append(dict(request.headers))
        await asyncio.sleep(0)
        for stamp, payload in self.archives.items():
            if stamp in str(request.url):
                return httpx.Response(200, content=payload, request=request)
        return httpx.Response(404, request=request)

    async def aclose(self):
        return None


class _LoopBoundTransport(httpx.AsyncBaseTransport):
    """Refuses to serve a second event loop, exactly as httpcore's pooled connections do.

    httpcore holds its connections in whatever loop first performed I/O, and ``asyncio.run``
    then closes that loop out from under them, so the next request raises
    ``RuntimeError('Event loop is closed')`` from ``BaseEventLoop._check_closed``. This
    raises the same error for the same reason, so the regression guarded below is the real
    one rather than a stand-in that merely resembles it.

    ``test_negative_control_a_process_wide_cache_really_does_fail`` proves it bites, by
    building the naive "one client, cached for the process" implementation and showing it
    break. If this transport ever stopped modelling the failure, that control goes red
    before the regression tests stop meaning anything.
    """

    def __init__(self, payload=PAYLOAD):
        self.payload = payload
        self.owner = None
        self.requests = 0

    async def handle_async_request(self, request):
        loop = asyncio.get_running_loop()
        if self.owner is None:
            self.owner = loop
        elif self.owner is not loop:
            raise RuntimeError("Event loop is closed")
        self.requests += 1
        return httpx.Response(200, content=self.payload, request=request)

    async def aclose(self):
        return None


class _Recorder:
    """The clients a test built, and the transport each one carries."""

    def __init__(self):
        self.built = []
        self.transports = []

    def requests(self):
        return [url for t in self.transports for url in getattr(t, "urls", [])]

    def transport_of(self, index):
        return self.transports[index]


def install(monkeypatch, factory):
    """Route every ``httpx.AsyncClient`` built in this test through ``factory()``.

    ``bhavcopy.py`` does ``import httpx`` and reaches ``httpx.AsyncClient`` at call time, so
    patching the attribute on the module is enough -- no seam is needed in the library, and
    the production code stays exactly as it ships.

    Each client gets its own transport, which is what a real ``httpx.AsyncClient`` does and
    what makes the loop-bound transport meaningful: a *reused* client brings its old,
    already-bound transport with it, which is precisely the bug.
    """
    recorder = _Recorder()
    real = httpx.AsyncClient

    class Counting(real):
        def __init__(self, **kwargs):
            transport = kwargs.pop("transport", None)
            if transport is None:
                transport = factory()
            super().__init__(transport=transport, **kwargs)
            recorder.built.append(self)
            recorder.transports.append(transport)

    monkeypatch.setattr(httpx, "AsyncClient", Counting)
    return recorder


# ------------------------------------------------------- 1. one client per walk


def test_range_walk_builds_one_client_for_every_request(monkeypatch):
    """The regression: 5 requests, 1 client.

    A client per request is 5 constructors and 5 TCP+TLS handshakes -- 2.67 s of every
    3.99 s per file, measured live.
    """
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))

    frame = fast_client().bhavcopy_range(*WALK)

    assert len(rec.built) == 1, f"expected one shared client, built {len(rec.built)}"
    assert len(rec.requests()) == 5, "the walk must still issue one request per weekday"
    assert len(frame) > 0


def test_single_day_still_builds_exactly_one_client(monkeypatch):
    """A one-day read is the degenerate walk: one request, one client."""
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))

    frame = fast_client().bhavcopy("2026-09-25")

    assert len(rec.built) == 1
    assert "RELIANCE" in frame.index.get_level_values("Symbol")


def test_negative_control_two_instances_build_two_clients(monkeypatch):
    """Two different instances must not share one client.

    Without this, the assertion above cannot tell "shared per instance" from "one client
    happened to be constructed"; with it, ``len(rec.built) == 1`` can only mean the reuse
    is per-instance.
    """
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))

    first = fast_client().bhavcopy("2026-09-25")
    second = fast_client().bhavcopy("2026-09-24")

    assert len(rec.built) == 2, "distinct instances must not share a cached client"
    assert len(rec.requests()) == 2
    assert "RELIANCE" in first.index.get_level_values("Symbol")
    assert "RELIANCE" in second.index.get_level_values("Symbol")


def test_repeated_sync_calls_close_their_client(monkeypatch):
    """Each self-contained call opens and closes exactly one client.

    A one-day read is one request, so holding a client open past it would buy nothing and
    would risk a socket outliving its loop. The range walk is the case that pays, and
    ``test_range_walk_builds_one_client_for_every_request`` covers it.
    """
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))
    client = fast_client()

    client.bhavcopy("2026-09-25")
    client.bhavcopy("2026-09-24")

    assert len(rec.built) == 2
    assert all(c.is_closed for c in rec.built), "no client may outlive the call that opened it"
    assert len(client._sessions) == 0, "no session may outlive its client"


# -------------------------------------------------- 2. the crux: a new loop each call


def test_negative_control_a_process_wide_cache_really_does_fail():
    """The loop transport must bite, or the tests below guard nothing.

    This is the implementation the brief warns about -- one client, cached for the life of
    the instance -- built literally, against the transport the regression tests use. If httpx
    ever became safe to reuse across loops, this goes red and the design could be
    simplified to a plain attribute.
    """
    transport = _LoopBoundTransport()
    naive = httpx.AsyncClient(transport=transport)

    async def get():
        return await naive.get("https://nsearchives.nseindia.com/x")

    assert asyncio.run(get()).status_code == 200
    with pytest.raises(RuntimeError, match="Event loop is closed"):
        asyncio.run(get())


def test_two_sequential_asyncio_runs_return_the_expected_bytes(monkeypatch):
    """Two ``asyncio.run`` calls, one instance, both succeed. This is the crux.

    ``_run_async`` answers every sync call with a fresh ``asyncio.run``, and
    ``market/ohlcv.py`` reaches the module that way through ``asyncio.to_thread``, so this is
    the production sequence and not a contrived one. A test that only ever runs inside a
    single loop cannot see the failure, which is why the loops here are separate and
    explicit.
    """
    rec = install(monkeypatch, lambda: _LoopBoundTransport())
    client = fast_client()

    first = asyncio.run(client._get_archive("https://nsearchives.nseindia.com/a.zip"))
    second = asyncio.run(client._get_archive("https://nsearchives.nseindia.com/b.zip"))

    assert first == PAYLOAD
    assert second == PAYLOAD
    assert len(rec.built) == 2, "a new loop cannot reuse the previous loop's client"
    assert all(c.is_closed for c in rec.built)
    assert len(client._sessions) == 0


def test_two_sequential_sync_calls_across_two_loops(monkeypatch):
    """The same thing through the public sync API, with real archives and the real parser.

    Each ``bhavcopy`` call is its own ``asyncio.run`` and therefore its own loop. Under a
    process-wide cache the second call raises ``RuntimeError('Event loop is closed')``.
    """
    install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))
    client = fast_client()

    first = client.bhavcopy("2026-09-25")
    second = client.bhavcopy("2026-09-24")
    third = client.bhavcopy("2026-09-25")

    for frame in (first, second, third):
        assert "RELIANCE" in frame.index.get_level_values("Symbol")
    assert first.attrs["bfinance_source_row_count"] > 0


def test_range_walk_over_two_consecutive_loops(monkeypatch):
    """``asyncio.to_thread(bhavcopy_range, ...)`` twice: the exact ohlcv.py path.

    The walk holds an outer keep-alive scope, so a whole multi-day download runs on one
    client -- and that client is still closed on its own loop before the loop dies.
    """
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))
    client = fast_client()

    async def driver():
        return [
            await asyncio.to_thread(client.bhavcopy_range, *WALK),
            await asyncio.to_thread(client.bhavcopy_range, *WALK),
        ]

    first, second = asyncio.run(driver())

    assert len(rec.built) == 2, "one client per loop, not one client per request"
    assert len(rec.requests()) == 10
    assert len(first) > 0 and len(first) == len(second)
    assert all(c.is_closed for c in rec.built)
    assert len(client._sessions) == 0


# ------------------------------------------------------------- 3. proxy is honoured


def test_proxy_is_passed_to_the_client(monkeypatch):
    """A configured proxy reaches ``httpx.AsyncClient`` verbatim.

    No transport is injected here on purpose: ``httpx.AsyncClient._init_transport`` returns
    an explicit ``transport=`` untouched, so injecting one would silently discard the proxy
    and the test would pass while the feature was broken. Only the constructor kwargs are
    observed, and no request is issued, so nothing dials out.
    """
    seen = []
    real = httpx.AsyncClient

    class Recording(real):
        def __init__(self, **kwargs):
            seen.append(dict(kwargs))
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    async def build(**kwargs):
        client = fast_client(**kwargs)
        async with client._keepalive() as built:
            return built

    asyncio.run(build(proxy="http://proxy.invalid:3128"))
    asyncio.run(build())

    assert seen[0]["proxy"] == "http://proxy.invalid:3128"
    assert seen[0]["timeout"] == 30.0
    assert seen[0]["follow_redirects"] is True
    # With no proxy configured and no env var set, the constructor sees None -- unchanged
    # from the per-request implementation.
    assert seen[1]["proxy"] is None


def test_proxy_from_environment_is_still_picked_up(monkeypatch):
    """``HTTPS_PROXY`` still reaches the client. ``__init__`` resolves it, unchanged."""
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env.invalid:8080")
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    assert fast_client().proxy == "http://from-env.invalid:8080"
    assert fast_client(proxy="http://explicit.invalid:1").proxy == "http://explicit.invalid:1"


# -------------------------------------------------- 4. concurrency and clean teardown


def test_concurrent_calls_share_a_client_without_closing_it_early(monkeypatch):
    """``asyncio.gather`` over several requests on one loop.

    The reference count is the only thing stopping the first task to finish from closing a
    client the rest are still reading; without it this fails with
    ``RuntimeError('Cannot send a request, as the client has been closed.')``.
    """
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))
    client = fast_client()
    stamps = ["20260924", "20260925"] * 4

    async def gather():
        return await asyncio.gather(
            *[
                client._get_archive(f"https://nsearchives.nseindia.com/cm{stamp}.zip")
                for stamp in stamps
            ]
        )

    payloads = asyncio.run(gather())

    assert len(payloads) == 8
    assert all(p is not None for p in payloads)
    assert len(rec.built) == 1, "concurrent requests on one loop must share one client"
    assert len(rec.requests()) == 8
    assert all(c.is_closed for c in rec.built)
    assert len(client._sessions) == 0


def test_a_failed_walk_still_closes_its_client(monkeypatch):
    """Teardown is in a ``finally``, so an exhausted retry budget does not leak a socket."""

    class Dead(httpx.AsyncBaseTransport):
        def __init__(self):
            self.hits = 0

        async def handle_async_request(self, request):
            self.hits += 1
            raise httpx.ConnectError("connection refused")

        async def aclose(self):
            return None

    rec = install(monkeypatch, Dead)
    client = fast_client(max_retries=2)

    with pytest.raises(bc.UpstreamServiceError):
        asyncio.run(client.bhavcopy_range_async(*WALK))

    assert len(rec.built) == 1
    assert all(c.is_closed for c in rec.built), "an aborted walk must not leak a client"
    assert len(client._sessions) == 0


def test_no_resource_warning_from_a_completed_walk(monkeypatch):
    """A finished walk leaves nothing for the collector to complain about.

    The failure this guards is silent and arrives after the fact: a client whose loop has
    died cannot be closed, so its sockets survive to raise ``ResourceWarning: unclosed
    socket`` and ``unclosed transport`` at an unrelated point in the process's life.
    """
    install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))
    client = fast_client()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.bhavcopy_range(*WALK)
        client.bhavcopy_range(*WALK)
        gc.collect()

    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert leaks == [], f"client sockets outlived their loop: {leaks}"


# --------------------------------------------------------- 5. unchanged behaviour


def test_status_mapping_and_retries_are_unchanged(monkeypatch):
    """The refactor moved the retry body, so pin what it still does."""

    class Scripted(httpx.AsyncBaseTransport):
        def __init__(self, codes):
            self.codes = list(codes)
            self.hits = 0

        async def handle_async_request(self, request):
            self.hits += 1
            code = self.codes.pop(0) if self.codes else 200
            return httpx.Response(code, request=request)

        async def aclose(self):
            return None

    def walk(codes, **kwargs):
        rec = install(monkeypatch, lambda: Scripted(codes))
        payload = asyncio.run(fast_client(**kwargs)._get_archive("https://x.invalid/a.zip"))
        return payload, rec.transports[0].hits

    # 404 -> None, and not retried.
    payload, hits = walk([404])
    assert payload is None and hits == 1

    # 5xx then 200 -> two requests, bytes returned.
    payload, hits = walk([503, 200], max_retries=3)
    assert payload == b"" and hits == 2

    # 5xx all the way -> UpstreamServiceError after max_retries.
    with pytest.raises(bc.UpstreamServiceError):
        walk([500, 500], max_retries=2)

    # 429 past the last attempt -> RateLimitExceededError.
    with pytest.raises(bc.RateLimitExceededError):
        walk([429, 429], max_retries=2)


def test_archive_requests_carry_the_documented_headers(monkeypatch):
    """No cookie priming, no landing page -- the same three headers as before."""
    rec = install(monkeypatch, lambda: _ArchiveTransport(ARCHIVES))

    fast_client().bhavcopy("2026-09-25")

    sent = rec.transport_of(0).headers[0]
    assert sent["user-agent"] in bc.USER_AGENTS
    assert sent["accept"] == "application/zip, application/octet-stream, */*"
    assert sent["accept-language"] == "en-US,en;q=0.9"
    assert "cookie" not in sent


def test_pacing_floor_is_untouched():
    """The 250 ms floor is a separate, deliberate question and is not changed here."""
    assert bc.BhavcopyClient().min_request_interval_ms == 250.0
