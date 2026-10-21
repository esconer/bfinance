"""
A1 — the real :class:`~bfinance.cache.sqlite_cache.CacheLookup` status on every cached frame.

**Why this file exists.** Five call sites in :mod:`bfinance.screener.client` read the cache with
:meth:`SQLiteCache.get`, which is ``get_result(...).value`` — the status is computed and then thrown
away. Every caller therefore had exactly one signal, ``cached is not None``, and that signal is about
*presence*, not *provenance*: a row holding JSON ``null``, an expired row and an unreadable row all
arrive as ``None``. Following every one of them with a refetch is the safe direction, which is why
nothing was ever *wrong* — but the frame the caller finally received could not say whether anything
had been cached at all.

:class:`~bfinance.ownership.OwnershipClient` already gets this right and owns the vocabulary
(``attrs['bfinance_cache_status']``, a ``CacheLookup.status`` string verbatim). These tests pin that
the screener client publishes **the same key with the same values**, and that the chart parser —
which used to set *no attrs at all*, making a cached chart frame indistinguishable from a live one —
now publishes provenance, so there is something for the status to sit beside.

Nothing here touches the network and nothing reads the user's ``~/.bfinance`` cache: every client is
built with the ``temp_cache`` fixture (a real on-disk :class:`SQLiteCache` under ``tmp_path``) or
with ``cache=None``.

**The negative controls at the bottom are not decoration.** :func:`assert_cache_status` and
:func:`assert_chart_provenance` are the only load-bearing assertions in this file and every test above
routes through them; the controls prove that removing the attrs turns them red, so a refactor that
stops publishing provenance or status cannot pass these tests by accident.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import textwrap

import pandas as pd
import pytest

from bfinance.cache.sqlite_cache import CacheLookup
from bfinance.models.company import CompanyProfile
from bfinance.screener import charts
from bfinance.screener.charts import (
    CAPABILITY as CHART_CAPABILITY,
    PROVENANCE as CHART_PROVENANCE,
    ScreenerChartEngine,
)
from bfinance.screener.client import ScreenerClient, extract_company_id

# The transport stub and the fixture reader are shared with the other screener test modules rather
# than copied: `_send_with_retry` is the single seam every request goes through, and two copies of it
# could drift into testing different things. `tests/` is not a package and pytest prepends its own
# directory to `sys.path`, so the module is imported by bare name, exactly as pytest imports it.
from test_screener_offline import Recordings, routes, text

CHART_METRIC = "price"
CHART_DAYS = 365

#: The names :class:`CacheLookup` defines. Every status a caller can ever observe is one of these, so
#: a frame publishing anything else would be a second vocabulary for the same fact.
LOOKUP_STATUSES = frozenset({"hit", "null", "miss", "expired", "corrupt"})

#: The attrs key the vocabulary is published under -- ownership's, which the screener client follows.
STATUS_KEY = "bfinance_cache_status"


# ------------------------------------------------------------------ the two load-bearing checks


def assert_cache_status(frame: pd.DataFrame, expected: str, where: str) -> None:
    """The one check this file is about, factored out so the control can attack exactly it.

    ``.get`` rather than ``[]`` on purpose: a *missing* key and a *wrong* value must fail the same
    way, and ``frame.attrs['bfinance_cache_status']`` would raise ``KeyError`` — a less readable
    failure than a message naming the call site.
    """
    actual = frame.attrs.get("bfinance_cache_status")
    assert actual == expected, (
        f"{where}: attrs['bfinance_cache_status'] is {actual!r}, expected {expected!r}. A frame "
        f"that cannot say whether it was cached is exactly what this test exists to prevent."
    )


def assert_chart_provenance(frame: pd.DataFrame, where: str) -> None:
    """The companion check: the chart parser's own claim on the frame, before any cache status."""
    assert frame.attrs.get("bfinance_provenance") == CHART_PROVENANCE, (
        f"{where}: attrs['bfinance_provenance'] is "
        f"{frame.attrs.get('bfinance_provenance')!r}, expected {CHART_PROVENANCE!r}"
    )
    assert frame.attrs.get("bfinance_capability") == CHART_CAPABILITY, where


def assert_profile_status(profile: CompanyProfile, expected: str, where: str) -> None:
    """The same claim as :func:`assert_cache_status`, for the one surface that has no ``attrs``."""
    actual = profile.cache_status
    assert actual == expected, (
        f"{where}: profile.cache_status is {actual!r}, expected {expected!r}. A profile that "
        f"cannot say whether it came from the cache is the gap that field exists to close."
    )


def _string_constant(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def count_cache_status_writes(obj) -> int:
    """How many places in **``obj``'s code** write :data:`STATUS_KEY`, read from its AST.

    The predicate this replaces was ``STATUS_KEY in inspect.getsource(obj)``, and it could not
    fail for the right reason: the key appears in the docstring of all three scanned methods
    (2/3/2 mentions), so deleting the actual assignment left it green. In a parsed body a docstring
    is a string constant and a comment does not exist at all, so only a real write can count here:
    a subscript target (``frame.attrs[...] = ...``) or a keyword/dict entry in an ``attrs.update``.

    Counted rather than merely detected, so a method that publishes once cannot silently become a
    method that mentions the key in a docstring and writes nothing.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    written = 0
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        written += sum(
            1
            for target in targets
            if isinstance(target, ast.Subscript) and _string_constant(target.slice) == STATUS_KEY
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "update":
                written += sum(1 for kw in node.keywords if kw.arg == STATUS_KEY)
                written += sum(
                    1
                    for arg in node.args
                    if isinstance(arg, ast.Dict)
                    and any(_string_constant(key) == STATUS_KEY for key in arg.keys)
                )
    return written


# ------------------------------------------------------------------- client, routes, fixtures


def cached_client(recorder: Recordings, cache) -> ScreenerClient:
    """A :class:`ScreenerClient` on an explicit cache with the transport replaced.

    ``offline_client`` in ``test_screener_offline`` hardcodes ``cache=None``; the point here is the
    other branch, so the cache is an argument.
    """
    client = ScreenerClient(cache=cache, min_request_interval_ms=0.0, max_retries=1)
    client._send_with_retry = recorder
    return client


#: Read off the recording rather than typed in, so a re-recorded fixture with a different id makes
#: this file fail loudly instead of silently addressing a route the stub does not serve.
ITC_ID = extract_company_id(text("company_itc.html"))


def chart_cache_key(comp_id: int = ITC_ID, metric: str = CHART_METRIC, days: int = CHART_DAYS) -> str:
    """The key ``get_chart_timeseries`` caches under, rebuilt from the same two expressions it uses.

    Written out here rather than pasted as a literal so a rename in the client turns this into a red
    test naming the side that moved, instead of a test that quietly plants a row nobody reads.
    """
    return f"screener_chart_{comp_id}_{ScreenerChartEngine.get_query_param(metric)}_{days}"


def company_id_cache_key(symbol: str = "ITC") -> str:
    """The key ``resolve_company_id`` caches the identifier under, spelled out for the same reason
    as :func:`chart_cache_key`: a copy that stops matching is a red test naming the side that moved,
    while importing the client's private helper would follow any rename silently.
    """
    return f"screener_company_id_{symbol}"


def chart_json() -> dict:
    return json.loads(text("chart_itc.json"))


def chart_recorder() -> Recordings:
    """The company page (for the id) and the chart API, both from the recorded fixtures."""
    return routes(("/company/ITC/", 200, text("company_itc.html")),
                  (f"/api/company/{ITC_ID}/chart/", 200, text("chart_itc.json")))


def profile_cache_key(symbol: str = "ITC") -> str:
    """The key ``get_company_profile`` caches under, spelled out for the same reason as
    :func:`chart_cache_key`: a copy that stops matching fails by name rather than planting a row
    nobody reads."""
    return f"screener_profile_{symbol}"


def profile_recorder() -> Recordings:
    """The consolidated company page (which carries the id) and the peers payload.

    The profile path fetches the *consolidated* page first and only falls back to the plain one
    when the ratio card is gone; ITC's recording has the card, so two requests is the whole cost
    of a live profile.
    """
    return routes(("/company/ITC/consolidated/", 200, text("company_itc.html")),
                  (f"/api/company/{ITC_ID}/peers/", 200, text("peers_itc.html")))


def leaf_path(code: str) -> str:
    return f"/market/{ScreenerClient._leaf_url(code).split('/market/', 1)[1]}"


def chart_calls(recorder: Recordings) -> list:
    """Just the chart-API requests. ``get_chart_timeseries`` resolves the company id *before* it
    builds the chart key, and that resolution is itself cached now (``company_id``, 168h), so the
    chart route's own count is the one that measures the chart cache directly: one request for a
    cold call, and zero added by the id on every warm one."""
    return [call for call in recorder.calls if "/chart/" in call]


# ============================================================ the chart parser, on its own


class TestChartFrameProvenance:
    """``parse_chart_json_to_dataframe`` set **no attrs at all** before this task.

    That is the gap that mattered: of the six cached payloads this client reads, the chart is the
    only one both parsed into a frame *and* re-served from cache, so for this one a cached answer and
    a live one were the same object to any consumer. A frame that carries nothing cannot be labelled,
    which is also why the plan said the chart cache had to stay banned until this closed.
    """

    def test_a_parsed_chart_frame_says_what_it_is(self):
        frame = ScreenerChartEngine.parse_chart_json_to_dataframe(
            {"datasets": [{"metric": "Price", "values": [["2026-01-01", 10.0], ["2026-01-02", 11.0]]}]}
        )
        assert_chart_provenance(frame, "one-dataset payload")
        assert frame.attrs["bfinance_source_row_count"] == 2
        assert frame.attrs["bfinance_columns"] == ("Price",)

    def test_the_provenance_survives_parsing_the_real_recording(self):
        frame = ScreenerChartEngine.parse_chart_json_to_dataframe(chart_json())
        assert_chart_provenance(frame, "recorded payload")
        assert frame.attrs["bfinance_columns"] == ("Price", "DMA50", "DMA200", "Volume")
        assert frame.attrs["bfinance_source_row_count"] == len(frame) == 8

    def test_an_empty_payload_still_carries_the_provenance(self):
        """The empty path is the one a caller is most likely to misread.

        An empty chart frame with no attrs is indistinguishable from "this was never cached and there
        is nothing to show", which are different facts and neither is inferable from ``len()``.
        """
        for payload in ({}, {"datasets": []}):
            frame = ScreenerChartEngine.parse_chart_json_to_dataframe(payload)
            assert frame.empty and frame.shape == (0, 0)
            assert_chart_provenance(frame, f"empty payload {payload!r}")
            assert frame.attrs["bfinance_source_row_count"] == 0
            assert frame.attrs["bfinance_columns"] == ()

    def test_the_parser_does_not_invent_a_cache_status(self):
        """It cannot know where its argument came from. The status belongs to the client that did
        the lookup.

        A parser that stamped ``"miss"`` would be *guessing* under the same key name as a fact, and
        the frame would then read as live even when the client had served it from cache — the exact
        failure this task exists to remove, reintroduced one layer down.
        """
        frame = ScreenerChartEngine.parse_chart_json_to_dataframe(chart_json())
        assert "bfinance_cache_status" not in frame.attrs

        empty = ScreenerChartEngine.parse_chart_json_to_dataframe({})
        assert "bfinance_cache_status" not in empty.attrs


# ========================================================== the status, on every cached frame


class TestCacheStatusIsTheRealOne:
    async def test_a_chart_frame_from_the_cache_says_hit_and_the_chart_api_is_not_re_requested(
        self, temp_cache
    ):
        recorder = chart_recorder()
        client = cached_client(recorder, temp_cache)

        live = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        assert_cache_status(live, "miss", "first call, fetched")
        assert temp_cache.get_result(chart_cache_key()).status == "hit", "the payload was stored"

        served = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        assert len(chart_calls(recorder)) == 1, f"the cached chart was re-fetched: {chart_calls(recorder)}"

        # The real status, not a hardcoded "hit": read it back off the cache for this exact key and
        # require the frame to agree. A client publishing any other string fails here.
        assert_cache_status(
            served, temp_cache.get_result(chart_cache_key()).status, "second call, cache read"
        )
        assert_cache_status(served, "hit", "second call, cache read")

    async def test_the_cached_and_live_frames_are_the_same_data_with_a_different_status(self, temp_cache):
        """The mission in one assertion: identical numbers, distinguishable provenance.

        ``assert_frame_equal`` ignores ``attrs`` (pandas 3.0.5's implementation never looks at them),
        which is exactly why a consumer reading only values could not have told these two apart
        before.
        """
        client = cached_client(chart_recorder(), temp_cache)
        live = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        served = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        pd.testing.assert_frame_equal(live, served)
        assert_cache_status(live, "miss", "live frame")
        assert_cache_status(served, "hit", "cached frame")
        assert_chart_provenance(served, "cached frame")

    async def test_a_client_with_no_cache_at_all_reports_the_miss(self):
        """``cache=None`` is the explicit "do not cache" argument.

        There is no lookup to report, so the status is the fetch's: ``"miss"``. Same convention as
        :class:`~bfinance.ownership.OwnershipClient`, and it means the key is never absent from a
        frame a caller can reach.
        """
        recorder = chart_recorder()
        client = cached_client(recorder, None)
        assert client.cache is None

        frame = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        assert_cache_status(frame, "miss", "cache disabled")
        assert len(chart_calls(recorder)) == 1

        again = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        assert_cache_status(again, "miss", "cache disabled, second call")
        assert len(chart_calls(recorder)) == 2, "with no cache every call must go out"

    async def test_force_refresh_reports_a_miss_and_re_requests(self, temp_cache):
        recorder = chart_recorder()
        client = cached_client(recorder, temp_cache)
        await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        forced = await client.get_chart_timeseries(
            "ITC", metric=CHART_METRIC, days=CHART_DAYS, force_refresh=True
        )
        assert_cache_status(forced, "miss", "force_refresh")
        assert len(chart_calls(recorder)) == 2

    async def test_an_expired_row_is_refetched_and_the_frame_does_not_say_hit(self, temp_cache):
        """Expire the row the way the passage of time would, by writing it with a negative TTL.

        The lookup answers ``"expired"``, the value is gone, so the client fetches — and the frame
        says ``"miss"``, because the frame it returns *was* fetched. What must never happen is a
        stale row reaching a frame labelled ``"hit"``.
        """
        recorder = chart_recorder()
        client = cached_client(recorder, temp_cache)
        await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        temp_cache.set(chart_cache_key(), chart_json(), category="chart", ttl_hours=-1.0)
        assert temp_cache.get_result(chart_cache_key()).status == "expired"

        refetched = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        assert_cache_status(refetched, "miss", "after expiry")
        assert len(chart_calls(recorder)) == 2

    async def test_a_stored_json_null_is_refetched_and_never_reported_as_a_hit(self, temp_cache):
        """The case ``cached is not None`` cannot express, and the reason the status is worth
        carrying: this row *exists*, is unexpired and was served — it holds nothing.

        :meth:`SQLiteCache.get` returns ``None`` for it, so the old code could not tell it from an
        absent key. :meth:`~bfinance.cache.sqlite_cache.SQLiteCache.get_result` can, and says
        ``"null"``; because there is no value, the frame is the fresh fetch, so the frame says
        ``"miss"``.
        """
        temp_cache.set(chart_cache_key(), None, category="chart", ttl_hours=1.0)
        assert temp_cache.get_result(chart_cache_key()).status == "null"
        assert temp_cache.get(chart_cache_key()) is None, "premise: get() cannot express a stored null"

        recorder = chart_recorder()
        frame = await cached_client(recorder, temp_cache).get_chart_timeseries(
            "ITC", metric=CHART_METRIC, days=CHART_DAYS
        )
        assert_cache_status(frame, "miss", "stored null")
        assert len(chart_calls(recorder)) == 1

    async def test_a_corrupt_row_is_refetched_and_never_reported_as_a_hit(self, temp_cache, cache_path):
        """The third outcome ``get()`` folds into ``None``: the row is there and unreadable.

        ``get_result`` answers ``"corrupt"`` and trusts nothing behind that key; the client refetches
        and the frame says ``"miss"``. Corrupt rows cannot be manufactured through the public API
        (``set`` writes strict JSON), so the malformed text is put in the database directly — which
        is also the realistic threat: the cache file is a file.
        """
        import sqlite3

        temp_cache.set(chart_cache_key(), chart_json(), category="chart", ttl_hours=1.0)
        with sqlite3.connect(cache_path, timeout=5.0) as conn:
            conn.execute(
                "UPDATE bfinance_cache SET payload = ? WHERE cache_key = ?",
                ("{not json", chart_cache_key()),
            )
            conn.commit()
        assert temp_cache.get_result(chart_cache_key()).status == "corrupt"

        recorder = chart_recorder()
        frame = await cached_client(recorder, temp_cache).get_chart_timeseries(
            "ITC", metric=CHART_METRIC, days=CHART_DAYS
        )
        assert_cache_status(frame, "miss", "corrupt row")
        assert len(chart_calls(recorder)) == 1

    async def test_the_market_overview_frame_carries_the_status(self, temp_cache):
        recorder = routes(("/market/", 200, text("market.html")))
        client = cached_client(recorder, temp_cache)

        live = await client.get_market_industries()
        assert_cache_status(live, "miss", "/market/ first call")

        served = await client.get_market_industries()
        assert len(recorder.calls) == 1, f"the cached overview was re-fetched: {recorder.calls}"
        assert_cache_status(served, "hit", "/market/ cache read")
        # Additive only: the attrs this frame already published are untouched.
        assert served.attrs["bfinance_source_url"] == "https://www.screener.in/market/"
        assert served.attrs["bfinance_market_single_request"] is True
        assert len(served) == 188

    async def test_the_industry_constituents_frame_carries_the_status(self, temp_cache):
        code = "IN040107001"
        recorder = routes((leaf_path(code), 200, text("industry_in040107001.html")))
        client = cached_client(recorder, temp_cache)

        live = await client.get_industry_constituents(code)
        assert_cache_status(live, "miss", "leaf first call")

        served = await client.get_industry_constituents(code)
        assert len(recorder.calls) == 1, f"the cached leaf page was re-fetched: {recorder.calls}"
        assert_cache_status(served, "hit", "leaf cache read")
        assert served.attrs["bfinance_constituents_pagination_robots_blocked"] is True
        assert len(served) == 5

    async def test_the_published_status_is_a_cachelookup_name_not_a_second_vocabulary(self, temp_cache):
        """``ownership`` publishes ``CacheLookup.status`` verbatim, so a consumer that learned the
        key from there works here. A private word for the same fact would make the instrument
        useless: two spellings of "cached" is the problem this task was raised to fix.
        """
        client = cached_client(chart_recorder(), temp_cache)
        live = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)
        served = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        for status in (live.attrs["bfinance_cache_status"], served.attrs["bfinance_cache_status"]):
            assert status in LOOKUP_STATUSES, f"{status!r} is not a CacheLookup name"
            assert CacheLookup(status).status == status, "it must be a name the type itself accepts"

    def test_this_client_and_ownership_publish_the_same_key(self):
        """``ownership`` is the reference implementation and already shipped this key.

        A source-level check, deliberately: the alternative is driving a second client with a
        second fixture battery to compare one string, which would test less for far more. It reads
        the **AST**, not the source text, because every one of these bodies *mentions* the key in
        prose: a substring check was green with the assignment at ``client.py:661`` deleted, which
        is the same as having no check. The predicate here counts a real write -- a subscript
        assignment or an ``attrs.update`` entry -- so a docstring cannot satisfy it, and it does
        not care *which* of the two forms a method uses. The behavioural proof that all four
        publish it is above; this one is the anti-drift guard for the case no fixture reaches.
        """
        from bfinance import ownership

        sites = {
            "OwnershipClient": ownership.OwnershipClient,
            "get_chart_timeseries": ScreenerClient.get_chart_timeseries,
            "get_market_industries": ScreenerClient.get_market_industries,
            "get_industry_constituents": ScreenerClient.get_industry_constituents,
        }
        for name, obj in sites.items():
            assert count_cache_status_writes(obj) >= 1, (
                f"{name} never writes attrs[{STATUS_KEY!r}]; a docstring mention is not a write"
            )


# ============================ the profile: a model is not a frame, and can still report


class TestCompanyProfileCacheStatus:
    """``get_company_profile`` was the one cacheable surface that could not report provenance.

    It returns a pydantic model, so there is no ``attrs`` to write to, and the profile TTL is an
    hour on a payload whose ``ratios.current_price`` and ``market_cap`` are a live quote. The model
    now carries ``cache_status`` -- excluded from serialisation, so the cached payload is unchanged
    -- and the client stamps it from the real ``CacheLookup``, exactly as the frame methods do.
    """

    async def test_the_cached_call_says_hit_and_the_first_call_says_miss(self, temp_cache):
        recorder = profile_recorder()
        client = cached_client(recorder, temp_cache)

        live = await client.get_company_profile("ITC")
        assert_profile_status(live, "miss", "first call, fetched")
        fetched = list(recorder.calls)
        assert len(fetched) == 2, fetched

        served = await client.get_company_profile("ITC")
        assert recorder.calls == fetched, f"the cached profile was re-fetched: {recorder.calls}"
        # The real status, not a hardcoded "hit": the row is read back for this exact key and the
        # profile is required to agree with it.
        assert temp_cache.get_result(profile_cache_key()).status == "hit"
        assert_profile_status(
            served, temp_cache.get_result(profile_cache_key()).status, "second call, cache read"
        )
        assert_profile_status(served, "hit", "second call, cache read")

        # The mission in one place: the same numbers with distinguishable provenance.
        assert served.company_id == live.company_id == ITC_ID
        assert served.name == live.name
        assert served.ratios.market_cap == live.ratios.market_cap

    async def test_the_status_is_never_part_of_the_cached_payload(self, temp_cache, cache_path):
        """``exclude=True`` is what makes this a status change rather than a cache-format change.

        ``get_company_profile`` caches ``profile.model_dump()`` and ``SQLiteCache.set`` stores
        ``json.dumps`` of that dict, so the field's fate is decided by the dump. Asserted against
        both: the dump and the JSON text actually sitting in the table.
        """
        import sqlite3

        profile = await cached_client(profile_recorder(), temp_cache).get_company_profile("ITC")
        assert_profile_status(profile, "miss", "payload test, live profile")

        assert "cache_status" not in profile.model_dump()
        assert "cache_status" not in json.loads(profile.model_dump_json())

        with sqlite3.connect(cache_path, timeout=5.0) as conn:
            stored = conn.execute(
                "SELECT payload FROM bfinance_cache WHERE cache_key = ?", (profile_cache_key(),)
            ).fetchone()[0]
        payload = json.loads(stored)

        assert "cache_status" not in payload, (
            f"the provenance leaked into the cache payload: {sorted(payload)}"
        )
        # The shape, not just the absence: every serialisable field is there and nothing else is,
        # so a second accidental addition cannot hide behind this one.
        assert set(payload) == set(CompanyProfile.model_fields) - {"cache_status"}, (
            f"the cached profile payload changed shape: {sorted(set(payload))}"
        )
        assert payload == profile.model_dump(), "the stored payload is not the model dump"

    def test_the_serialised_bytes_do_not_depend_on_the_status_value(self):
        """Two profiles with identical fields, one carrying provenance: identical serialisations.

        This is the byte-equality claim in its smallest form, and it fails the moment the field
        stops being excluded -- ``json.dumps`` of the two dumps would then differ by
        ``"cache_status": "hit"``.
        """
        fields = {"symbol": "ITC", "company_id": ITC_ID, "name": "ITC Ltd"}
        plain = CompanyProfile(**fields)
        stamped = CompanyProfile(**fields, cache_status="hit")

        assert stamped.cache_status == "hit" and plain.cache_status is None
        assert plain.model_dump() == stamped.model_dump()
        assert json.dumps(plain.model_dump(), allow_nan=False) == json.dumps(
            stamped.model_dump(), allow_nan=False
        )
        assert plain.model_dump_json() == stamped.model_dump_json()

    async def test_a_client_with_no_cache_reports_miss_not_disabled(self):
        """``cache=None`` means there was no lookup, so the profile was fetched: ``"miss"``.

        ``"disabled"`` is the store's word for "no lookup happened", and the same convention the
        frame paths use: it is never what a caller receives, because a caller that receives a
        profile received a fetch.
        """
        recorder = profile_recorder()
        client = cached_client(recorder, None)

        first = await client.get_company_profile("ITC")
        assert_profile_status(first, "miss", "cache disabled")
        assert first.cache_status != "disabled", "the store's word must not reach the caller"
        assert len(recorder.calls) == 2

        again = await client.get_company_profile("ITC")
        assert_profile_status(again, "miss", "cache disabled, second call")
        assert len(recorder.calls) == 4, "with no cache every call must go out"

    async def test_force_refresh_reports_miss_over_a_warm_row(self, temp_cache):
        recorder = profile_recorder()
        client = cached_client(recorder, temp_cache)
        await client.get_company_profile("ITC")
        warm = len(recorder.calls)

        forced = await client.get_company_profile("ITC", force_refresh=True)
        assert_profile_status(forced, "miss", "force_refresh")
        assert len(recorder.calls) > warm, "force_refresh must not be served from the cache"

    async def test_a_stored_null_profile_row_is_refetched_and_reported_as_miss(self, temp_cache):
        """The lookup's own word must not leak: a row holding ``null`` is not a profile."""
        temp_cache.set(profile_cache_key(), None, category="profile", ttl_hours=1.0)
        assert temp_cache.get_result(profile_cache_key()).status == "null"

        recorder = profile_recorder()
        profile = await cached_client(recorder, temp_cache).get_company_profile("ITC")

        assert_profile_status(profile, "miss", "stored null")
        assert len(recorder.calls) == 2, "a null row has no profile in it, so the page is fetched"
        assert profile.cache_status != "null", "a profile is never the lookup's 'null'"

    async def test_a_corrupt_profile_row_is_refetched_and_reported_as_miss(
        self, temp_cache, cache_path
    ):
        import sqlite3

        temp_cache.set(profile_cache_key(), {"symbol": "ITC"}, category="profile", ttl_hours=1.0)
        with sqlite3.connect(cache_path, timeout=5.0) as conn:
            conn.execute(
                "UPDATE bfinance_cache SET payload = ? WHERE cache_key = ?",
                ("{not json", profile_cache_key()),
            )
            conn.commit()
        assert temp_cache.get_result(profile_cache_key()).status == "corrupt"

        recorder = profile_recorder()
        profile = await cached_client(recorder, temp_cache).get_company_profile("ITC")

        assert_profile_status(profile, "miss", "corrupt row")
        assert len(recorder.calls) == 2
        assert profile.cache_status != "corrupt"


# ============================================================ negative controls: prove it bites


class TestNegativeControl:
    """The checks are load-bearing, demonstrated rather than asserted.

    Three mutations, because they attack different halves: removing the key from a real frame,
    putting the *wrong* value on it, and neutering the parser's stamping helper in memory so the
    provenance check has nothing to read.
    """

    async def test_removing_the_attr_from_a_frame_turns_the_assertion_red(self, temp_cache):
        frame = await cached_client(chart_recorder(), temp_cache).get_chart_timeseries(
            "ITC", metric=CHART_METRIC, days=CHART_DAYS
        )
        assert_cache_status(frame, "miss", "before the mutation")

        frame.attrs.pop("bfinance_cache_status")
        with pytest.raises(AssertionError, match="bfinance_cache_status"):
            assert_cache_status(frame, "miss", "after the mutation")

    async def test_a_wrong_value_turns_the_assertion_red_too(self, temp_cache):
        """A frame carrying the *wrong* status is the worse failure and must fail the same check."""
        frame = await cached_client(chart_recorder(), temp_cache).get_chart_timeseries(
            "ITC", metric=CHART_METRIC, days=CHART_DAYS
        )
        frame.attrs["bfinance_cache_status"] = "hit"

        with pytest.raises(AssertionError, match="expected 'miss'"):
            assert_cache_status(frame, "miss", "the frame claims to be cached when it was not")

    def test_neutering_the_parser_helper_turns_the_provenance_check_red(self, monkeypatch):
        """The mutation a refactor would actually make: the stamping helper stops stamping.

        Both the status tests and the provenance tests read the frame through the two helpers, so
        this takes out the whole file's subject at once.
        """
        monkeypatch.setattr(charts, "_publish", lambda frame: frame)
        frame = ScreenerChartEngine.parse_chart_json_to_dataframe(chart_json())
        assert len(frame) == 8, "the parse itself is untouched; only the stamping was removed"

        with pytest.raises(AssertionError, match="bfinance_provenance"):
            assert_chart_provenance(frame, "provenance helper neutered")

    async def test_neutering_the_client_cache_read_turns_the_hit_assertion_red(
        self, temp_cache, monkeypatch
    ):
        """A ``_cache_read`` that returns the wrong status fails every hit assertion.

        This is the "guess instead of measure" mutation: the client still caches and still avoids the
        second chart request, but the frame lies about where it came from.
        """
        client = cached_client(chart_recorder(), temp_cache)
        await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        monkeypatch.setattr(
            ScreenerClient, "_cache_read", lambda self, key: (self.cache.get(key), "probably cached")
        )
        served = await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        assert served.attrs["bfinance_cache_status"] == "probably cached"
        with pytest.raises(AssertionError, match="bfinance_cache_status"):
            assert_cache_status(served, "hit", "guessing _cache_read")


# ============================== the silent no-op this task must not accidentally change


class TestUnserialisablePayloadStillLogsAndReturns:
    """``SQLiteCache.set`` swallowing an unserialisable payload is load-bearing, not untidy.

    A ``DataFrame`` cannot be JSON-encoded, so every attempt to cache one logs a warning and writes
    no row — and the next call reads it back as a miss and refetches. That is the known silent no-op
    the storage blocker (B1) is being built to fix, and until it lands the behaviour must stay exactly
    as it is: **log and return**, never raise.

    Making it raise is the tempting "fix" and it is wrong: ``set`` has many callers whose payloads are
    not frames, and turning a metrics-level complaint into an exception turns a warm cache miss into
    a total outage for every one of them. These tests pin the no-op so nobody "corrects" it by
    accident.
    """

    def test_a_dataframe_payload_is_logged_and_dropped_not_raised(self, temp_cache, caplog):
        frame = pd.DataFrame({"Close": [1.0, 2.0]})

        with caplog.at_level(logging.WARNING, logger="bfinance"):
            assert temp_cache.set("ohlcv_TCS_1y", frame) is None, "set must return, not raise"

        assert "Skipping cache set" in caplog.text
        assert "ohlcv_TCS_1y" in caplog.text, f"the log must name the key it refused: {caplog.text!r}"
        assert temp_cache.get_result("ohlcv_TCS_1y").status == "miss"

    def test_the_cache_is_still_usable_after_a_refused_payload(self, temp_cache):
        """The refusal is local to the one call. If it poisoned the connection, every later cache use
        in the process would fail and the symptom would be a silent, total loss of caching."""
        temp_cache.set("ohlcv_TCS_1y", pd.DataFrame({"Close": [1.0]}), category="price")
        temp_cache.set("profile_TCS", {"price": 2082.0}, category="profile", ttl_hours=1.0)

        assert temp_cache.get_result("profile_TCS").status == "hit"
        assert temp_cache.get("profile_TCS") == {"price": 2082.0}

    @pytest.mark.parametrize(
        "payload",
        [
            pd.DataFrame({"Close": [1.0]}),      # TypeError branch: not encodable
            {"ratios": pd.DataFrame({"pe": [20.0]})},  # TypeError, nested
            {"a", "b"},                           # a set is not JSON
            object(),                              # no JSON form whatsoever
            float("nan"),                          # ValueError branch (allow_nan=False)
        ],
        ids=["frame", "frame_nested", "set", "object", "nan"],
    )
    def test_no_payload_form_escapes_the_no_op_handler(self, temp_cache, payload):
        """Both handlers are covered: ``TypeError`` (not encodable) and ``ValueError`` (encodable but
        not representable — NaN, or a circular reference). Neither may raise."""
        assert temp_cache.set("payload", payload) is None
        assert temp_cache.get_result("payload").status == "miss"

    def test_a_circular_reference_is_refused_not_raised(self, temp_cache, caplog):
        cycle: dict = {}
        cycle["self"] = cycle

        with caplog.at_level(logging.ERROR, logger="bfinance"):
            assert temp_cache.set("cyclic", cycle) is None

        assert "cyclic" in caplog.text
        assert temp_cache.get_result("cyclic").status == "miss"

    async def test_the_no_op_is_why_the_chart_frame_is_never_cached_as_a_frame(self, temp_cache,
                                                                                monkeypatch):
        """The measured blocker this whole plan rests on, pinned from the cache's own side.

        The screener client caches the chart's **raw JSON**, not the parsed frame, and that is not an
        accident of style: a frame cannot be stored in this cache at all. If a future change made
        ``set`` silently succeed on a frame, the caching decision above would start hiding a real
        defect instead of avoiding one — so what lands in the cache is asserted to be a ``dict``.
        On a cold cache the identifier row is written first (``resolve_company_id``, also not a
        frame), which is why the ``dict`` claim below is scoped to the chart key.
        """
        client = cached_client(chart_recorder(), temp_cache)
        stored: list = []
        monkeypatch.setattr(temp_cache, "set", _recording_set(stored))

        await client.get_chart_timeseries("ITC", metric=CHART_METRIC, days=CHART_DAYS)

        # Two writes on a cold cache, in this order: the identifier row (``resolve_company_id``,
        # read off the same company page) and then the chart's raw JSON. Asserted as the exact pair,
        # categories included, so a third writer on this path still fails here.
        wrote = [(key, category) for key, category, _ in stored]
        assert wrote == [
            (company_id_cache_key(), "company_id"),
            (chart_cache_key(), "chart"),
        ], f"expected the id row and then the chart row, got {wrote}"
        # The dict claim is about what the chart key holds, which is this test's subject; the id
        # row above is an ``int`` and is pinned by its own tests.
        chart_values = [value for key, _, value in stored if key == chart_cache_key()]
        kinds = [type(value).__name__ for value in chart_values]
        assert kinds == ["dict"], f"the chart cache must hold the raw JSON payload, got {kinds}"


def _recording_set(sink: list):
    """A stand-in for ``SQLiteCache.set`` that records what it was asked to store, and writes
    nothing — so the test observes the client's decision rather than the table."""

    def record(cache_key, payload, category="general", ttl_hours=None):
        sink.append((cache_key, category, payload))

    return record