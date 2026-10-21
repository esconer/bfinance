"""The opt-in trendlyne section surface on ``Ticker``.

Eight members, one flag, one lazy client, and the properties this package treats as load-bearing
enough to assert directly.

**The flag costs nothing.** ``Ticker(symbol)`` must open no trendlyne socket and must not even
execute the leaf package: ``assert calls == []`` on the transport, and a **fresh-interpreter**
probe for ``"bfinance.trendlyne" in sys.modules`` after a default ``Ticker`` has been built and
used. In-process would be meaningless -- this file imports ``bfinance.trendlyne.client`` itself,
and pytest runs every module in one process, so ``tests/test_public_surface.py``'s technique
(``subprocess.run([sys.executable, "-c", ...])``) is the one used here, control included.

**The bridge is an ``importlib`` call, and this file is allowed to say so.**
``src/bfinance/ticker.py`` must not contain an ``Import``/``ImportFrom`` naming ``*trendlyne*``,
at module scope or inside a function body, because ``tests/test_trendlyne_core.py::TestIsolation``
AST-walks every ``src/`` module for exactly that. So the members reach the client through
``importlib.import_module``, and the seam under test is that call: :func:`_install_bridge`
intercepts ``importlib.import_module`` and hands back a mock-backed client. Interception that
quietly stopped matching would leave these tests reaching the real vendor, so the seam is asserted
to be narrow, to be the only reason the file is offline, and to be the exact name the bridge asks
for.

**The news shape is pinned against the one bfinance already ships.**
:meth:`bfinance.market.corporate.CorporateActionsEngine.build_news` -- the engine behind
:attr:`Ticker.news` -- is the yfinance-shaped anchor, and it is reachable offline. It is *not*
``tests/test_dropin_actions.py`` or ``tests/test_ticker_yfinance_compat.py``: both are fully
``live``-marked and collect zero tests under ``-m "not live"``, so neither can be verification.

**Nothing is fabricated, and every failure says so.** A section that cannot be fetched returns its
honest empty shape *and* records the reason through ``_degrade``, so "this company has no block
deals" stays distinguishable from "the page did not load" -- the distinction ``data_warnings``
exists for. Every shape test names the route it must have requested, every flag-off test is paired
with its flag-on counterpart, and the three probes (the import scan, the news-shape comparison and
the subprocess isolation check) each carry a control that turns them red.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import httpx
import pandas as pd
import pytest

from bfinance.market.corporate import CorporateActionsEngine
from bfinance.ticker import Ticker, _targets_from_reports
from bfinance.trendlyne import client as tl
from bfinance.utils.exceptions import BFinanceError

# The mock-transport seam and the fixture readers are shared with the core module rather than
# copied: two copies of a request stub drift into testing different things. `tests/` is not a
# package, so the module is imported by bare name -- exactly as pytest imports it.
from test_trendlyne_core import mock_client, payload, text

SRC = Path(__file__).resolve().parents[1] / "src"
TICKER_PY = SRC / "bfinance" / "ticker.py"

#: The string ``_section_client`` hands to ``importlib.import_module``. Spelled out here rather
#: than imported from ``bfinance.ticker`` so that renaming the module on either side is a
#: **failure** -- the seam stops matching and these tests would reach the real vendor -- instead
#: of a silent agreement between two copies of the same literal.
BRIDGE_MODULE = "bfinance.trendlyne.client"

#: The symbol these tests resolve. MOTHERSON (id 878), because that is what the recorded sitemap
#: excerpt carries; ``tests/test_trendlyne_sections.py``'s module docstring records the
#: substitution its mock makes -- the handler keys on the **route substring** and serves the TCS
#: bodies recorded 2026-10-10 under the MOTHERSON id. Reused here for the same reason, so the two
#: files resolve one symbol between them rather than two.
SYMBOL = "MOTHERSON"
SITEMAP = "sitemap_excerpt.xml"
RECORD = tl.parse_sitemap(text(SITEMAP))[SYMBOL]

#: Route substring -> fixture, **ordered longest needle first**. The members below do not reach
#: ``/equity/monthly-mutual-fund-share-holding/`` -- whose path contains ``share-holding`` -- but
#: the ordering is kept so a future member cannot be bitten by it.
ROUTES: Tuple[Tuple[str, str], ...] = (
    ("/equity/bulk-block-deals/", "bulk_deals_motherson.html"),
    ("/equity/share-holding/", "shareholding_tcs.html"),
    ("/equity/swot-buy-or-sell/", "swot_tcs.html"),
    ("/equity/api/stock/adv-technical-analysis/", "adv_technical_analysis_tcs.json"),
    ("/research-reports/stock/", "research_reports_tcs.html"),
    ("/latest-news/", "latest_news_tcs.html"),
    ("/tools/buy-sell-zone/", "buy_sell_zone_tcs.html"),
    ("/equity-sitemap-stocks.xml", SITEMAP),
)

#: Every member that returns a single frame, and the **route needle** its client method must
#: request. The value is a needle rather than a method name on purpose: a member wired to the
#: wrong client method asks a different route and fails here, which is the control that keeps the
#: shape assertions from passing on a frame that merely looks plausible.
FRAME_MEMBERS: Dict[str, str] = {
    "trendlyne_reports": "/research-reports/stock/",
    "trendlyne_shareholding": "/equity/share-holding/",
    "trendlyne_deals": "/equity/bulk-block-deals/",
    "trendlyne_technicals": "/equity/api/stock/adv-technical-analysis/",
}

#: The members returning a dict of frames, keyed the same way.
DICT_MEMBERS: Dict[str, str] = {
    "trendlyne_buy_sell_zone": "/tools/buy-sell-zone/",
    "trendlyne_swot": "/equity/swot-buy-or-sell/",
}

#: Every new callable member, for the flag-off sweep.
ALL_MEMBERS: Tuple[str, ...] = (
    "trendlyne_news",
    *sorted(FRAME_MEMBERS),
    *sorted(DICT_MEMBERS),
)


class Recorder:
    """A ``MockTransport`` handler that answers by route substring and logs every URL.

    ``overrides`` is consulted first and may hold a response or a callable taking the request,
    which is how the degradation tests fail one route -- or every route, with the ``trendlyne.com``
    needle -- without touching the rest. An unrecognised route answers **404**, never an empty
    200: a member that requested a route nobody registered fails loudly here instead of
    "succeeding" on a body it was not meant to parse.
    """

    def __init__(self, overrides: Dict[str, Any] | None = None) -> None:
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
        """Every requested URL with the host stripped, for comparison against the template."""
        return [url.replace(tl.BASE_URL, "") for url in self.calls]

    @property
    def needles(self) -> List[str]:
        """Which registered route each request matched, in order. The control on "wired to the
        right client method": an unmatched URL is reported as itself, never dropped."""
        return [
            next((n for n, _ in ROUTES if n in url), url) for url in self.calls
        ]


def _install_bridge(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    """Point ``_section_client``'s ``importlib.import_module`` at a mock-backed client.

    Interception is keyed on the exact module name the bridge asks for; every other name goes to
    the real ``importlib``, so this seam cannot quietly swallow an unrelated import. The client is
    built with the sitemap index already warm -- the index is cached in memory, and a cold one
    would add a request that has nothing to do with the route under test.

    ``cache_ttl_hours=0`` on every Ticker built here keeps the test off the user's disk, and the
    resulting ``cache=None`` is what proves Ticker forwards its own arguments down to the client.
    """
    real_import_module = importlib.import_module

    def build(**kwargs):
        client = mock_client(recorder, **kwargs)
        client.load_symbol_index(text(SITEMAP))
        return client

    def fake_import_module(name, *args, **kwargs):
        if name == BRIDGE_MODULE:
            return SimpleNamespace(TrendlyneClient=build)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> Recorder:
    _install_bridge(monkeypatch, recorder)
    return recorder


@pytest.fixture
def on(seam: Recorder) -> Ticker:
    """A Ticker with the opt-in on, wired to the mock transport."""
    return Ticker(SYMBOL, trendlyne_sections=True, cache_ttl_hours=0)


@pytest.fixture
def off(seam: Recorder) -> Ticker:
    """A Ticker with the opt-in **off**, wired to the same mock transport.

    Same seam, opposite flag: so "no request was made" below is a statement about the flag and not
    about the wiring being absent.
    """
    return Ticker(SYMBOL, cache_ttl_hours=0)


# ====================================================================== the flag


def test_the_flag_is_off_by_default():
    assert Ticker(SYMBOL, cache_ttl_hours=0).trendlyne_sections is False


def test_the_flag_is_a_separate_one_from_the_statement_fallback():
    """Different capability, different routes. Folding them into one flag would mean a caller who
    wants the sections also silently opened the statements fallback."""
    sections_only = Ticker(SYMBOL, cache_ttl_hours=0, trendlyne_sections=True)
    fallback_only = Ticker(SYMBOL, cache_ttl_hours=0, statement_fallback=True)

    assert (sections_only.trendlyne_sections, sections_only.statement_fallback) == (True, False)
    assert (fallback_only.trendlyne_sections, fallback_only.statement_fallback) == (False, True)


def test_tickers_can_set_the_flag_and_forwards_it_to_every_ticker():
    """``Tickers`` had no way to turn this on: ``trendlyne_sections=True`` was a ``TypeError``.

    It forwarded ``statement_fallback`` and had no parameter for this one, so a caller holding a
    collection had to rebuild every symbol by hand to reach a section — which reads as a broken
    member rather than a collection that cannot set the flag. Asserted on the constructed objects,
    not on the call, because the keyword reaches the real constructor through ``bfinance.tickers``'
    ``_apply_ticker_bool_shim`` wrapper and the attribute is what the members read.
    """
    import bfinance.tickers  # noqa: F401  -- applies the shim
    from bfinance.tickers import Tickers

    collection = Tickers(["TCS", "MOTHERSON"], cache_ttl_hours=0, trendlyne_sections=True)
    assert {sym: t.trendlyne_sections for sym, t in collection.tickers.items()} == {
        "TCS": True,
        "MOTHERSON": True,
    }
    # Reachable through __getitem__ too, which is how a caller actually gets one out.
    assert collection["MOTHERSON"].trendlyne_sections is True


def test_a_tickers_collection_defaults_to_both_flags_off():
    """Adding the keyword must not change what a default collection does.

    Two flags off, and the shim's own contract untouched: a bare bool in the second position is
    still ``multi_level_index`` and not a TTL.
    """
    import bfinance.tickers  # noqa: F401  -- applies the shim
    from bfinance.tickers import Tickers

    collection = Tickers("TCS,MOTHERSON", cache_ttl_hours=0)
    assert all(t.trendlyne_sections is False for t in collection.tickers.values())
    assert all(t.statement_fallback is False for t in collection.tickers.values())
    assert collection.multi_level_index is True
    assert Tickers(["TCS"], False).multi_level_index is False

    # Separate flags, same as on Ticker itself: a caller who wants the sections does not thereby
    # open the statements fallback.
    sections = Tickers(["TCS"], cache_ttl_hours=0, trendlyne_sections=True)["TCS"]
    fallback = Tickers(["TCS"], cache_ttl_hours=0, statement_fallback=True)["TCS"]
    assert (sections.trendlyne_sections, sections.statement_fallback) == (True, False)
    assert (fallback.trendlyne_sections, fallback.statement_fallback) == (False, True)


def test_a_collection_with_the_flag_on_reaches_a_section_member(seam):
    """End to end through the shim and the bridge, not just on the attribute.

    Without this, a keyword that was accepted and dropped would pass the two tests above: the
    attribute would have to be read back off the object for that to happen, but the *member*
    would still refuse. Here a member is called on a Ticker taken out of the collection and has to
    reach the mock transport.
    """
    import bfinance.tickers  # noqa: F401  -- applies the shim
    from bfinance.tickers import Tickers

    collection = Tickers([SYMBOL], cache_ttl_hours=0, trendlyne_sections=True)
    frame = collection[SYMBOL].trendlyne_shareholding()
    assert not frame.empty
    assert seam.needles == ["/equity/share-holding/"], seam.calls
    assert seam.calls, "the member never reached the transport"

    # And the same collection with the flag off refuses before any request, which is the default
    # this keyword must not have disturbed.
    other = Tickers([SYMBOL], cache_ttl_hours=0)
    seam.calls.clear()
    with pytest.raises(NotImplementedError, match="trendlyne_sections=True"):
        other[SYMBOL].trendlyne_shareholding()
    assert seam.calls == []


@pytest.mark.parametrize("member", ALL_MEMBERS)
def test_every_member_refuses_with_the_flag_off_and_costs_nothing(off, recorder, member):
    """The default is a refusal, not an empty answer.

    An empty frame is how "this symbol has no share-holding rows" reads. Handing one to a caller
    who never opted in would report a fact about the company that nobody went looking for, which
    is the fabrication this class refuses elsewhere -- so the member raises instead, and the
    transport is never touched.
    """
    with pytest.raises(NotImplementedError, match="trendlyne_sections=True"):
        getattr(off, member)()

    assert recorder.calls == [], f"{member} reached the vendor with the flag off: {recorder.calls}"
    assert off.data_warnings == (), "a refusal was recorded as a degradation"


def test_the_flag_off_path_does_not_even_build_a_client(off):
    """The refusal is decided before anything is constructed, so a default Ticker pays nothing:
    not a request, not an object, not an import."""
    assert off._section_client_instance is None


# ================================================== the bridge, and that it is only a bridge


def _imported_names(path: Path) -> set[str]:
    """Every module path an ``Import``/``ImportFrom`` node in ``path`` reaches.

    The rule ``TestIsolation`` applies, walked the same way -- ``ast.walk`` descends into
    function bodies, which is the half that matters here.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_ticker_names_no_trendlyne_import_anywhere_in_the_file():
    """The reason the bridge is an ``importlib`` call, asserted on the file this change edited,
    so the rule is stated where a future edit is most likely to break it."""
    offenders = sorted(name for name in _imported_names(TICKER_PY) if "trendlyne" in name)

    assert offenders == [], (
        f"src/bfinance/ticker.py imports {offenders}. TestIsolation walks every src/ module for an "
        f"Import/ImportFrom naming *trendlyne* -- including inside a function body -- so this is a "
        f"hard failure, not a style note. Use importlib.import_module instead."
    )


def test_the_import_scan_sees_an_import_inside_a_function_body(tmp_path):
    """The control for the scan above, and the half that is easy to get wrong.

    A scanner reading only ``tree.body`` would catch a module-level import and miss a
    function-local one -- which is exactly the form a "it only runs on the opt-in path" refactor
    would reach for. The synthetic files put the import at each depth, and the control has to be
    the answer the scan gives.
    """
    inside = tmp_path / "probe_inside.py"
    inside.write_text(
        '"""The forbidden import, in the least detectable place."""\n'
        "def _section_client():\n"
        "    from bfinance.trendlyne.client import TrendlyneClient\n"
        "    return TrendlyneClient()\n",
        encoding="utf-8",
    )
    top_level = tmp_path / "probe_top.py"
    top_level.write_text(
        '"""The same thing at module scope, which every scanner must also see."""\n'
        "import bfinance.trendlyne\n",
        encoding="utf-8",
    )

    assert "bfinance.trendlyne.client" in _imported_names(inside), (
        "the scan cannot see an import inside a function body, so the rule it enforces is not the "
        "rule the isolation test enforces"
    )
    assert "bfinance.trendlyne" in _imported_names(top_level)
    assert not any("trendlyne" in name for name in _imported_names(TICKER_PY))


def test_the_bridge_seam_intercepts_exactly_one_module_name(seam):
    """And it is the name ``_section_client`` actually asks for. If the two ever drifted, this
    suite would stop being offline and start talking to trendlyne.com -- a failure that looks
    like a flaky network rather than a broken seam."""
    substituted = importlib.import_module(BRIDGE_MODULE)

    assert substituted is not tl, "the seam did not intercept: this file would reach the network"
    assert substituted.TrendlyneClient is not tl.TrendlyneClient


def test_the_bridge_seam_lets_every_other_import_through(seam):
    """The control on the seam's breadth: a seam answering *every* import name would make the
    test above pass while breaking every other dynamic import in the process."""
    assert importlib.import_module("json") is json
    assert importlib.import_module("bfinance.ticker").__name__ == "bfinance.ticker"
    assert importlib.import_module("bfinance.ticker") is not None


def test_the_client_is_built_once_and_reused_across_members(on, recorder):
    """Seven members share one sitemap index, one per-symbol record and one pacing slot."""
    on.trendlyne_deals()
    first_client = on._section_client_instance
    on.trendlyne_reports()

    assert on._section_client_instance is first_client
    assert not any("equity-sitemap-stocks.xml" in path for path in recorder.paths), (
        f"the sitemap was fetched again: {recorder.paths}"
    )


def test_the_transport_configuration_reaches_the_client(seam):
    """``Ticker("X", timeout=..., proxy=...)`` means the same thing on the opt-in surface, or the
    flag would quietly hand the caller a client configured differently from the object they set
    up."""
    ticker = Ticker(
        SYMBOL, trendlyne_sections=True, cache_ttl_hours=0, timeout=3.5,
        proxy="http://proxy.invalid:8080",
    )
    client = ticker._section_client()

    assert client.timeout == 3.5
    assert client.proxy == "http://proxy.invalid:8080"
    assert client.cache is None, "cache_ttl_hours=0 means no cache, and it must stay None"


# ============================================================ runtime isolation (subprocess)


def _probe(flag: bool) -> str:
    """Ask "was the trendlyne leaf executed?" in a **fresh interpreter**, and return the answer.

    In-process this question is unaskable: this file imports ``bfinance.trendlyne.client`` at
    module scope and pytest runs every module in one process, so ``sys.modules`` was populated
    before the first test ran. ``tests/test_public_surface.py`` records the same pollution and
    reaches for a subprocess for the same reason.

    With the flag off the program builds a default Ticker, reads the flag, reads the analyst
    targets and calls every section member -- which must refuse. With it on, it only *builds* the
    client: constructing ``TrendlyneClient`` performs no I/O of its own, so the probe still
    touches no network while loading the leaf.
    """
    program = textwrap.dedent(
        f"""
        import sys
        from bfinance.ticker import Ticker

        ticker = Ticker({SYMBOL!r}, cache_ttl_hours=0, trendlyne_sections={flag})
        assert ticker.trendlyne_sections is {flag}
        if {flag}:
            assert ticker._section_client_instance is None
            ticker._section_client()
        else:
            assert ticker.analyst_price_targets is None
            for name in {ALL_MEMBERS!r}:
                try:
                    getattr(ticker, name)()
                except NotImplementedError:
                    continue
                raise SystemExit(f"{{name}} ran with the flag off")
        print("bfinance.trendlyne" in sys.modules, "bfinance.trendlyne.client" in sys.modules)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.strip()


def test_a_default_ticker_never_loads_the_trendlyne_leaf():
    """Constructed, flag read, analyst targets read, every member refused -- and the leaf is
    still not in ``sys.modules``. ``import bfinance`` executing it would charge every caller for
    a module almost none of them use."""
    assert _probe(False) == "False False"


def test_the_isolation_probe_goes_red_on_a_ticker_that_did_reach_the_leaf():
    """The control, and it is not optional. A probe that could only ever print ``False`` would
    leave the test above green whether or not the package loaded the leaf."""
    on_leaf = _probe(True)

    assert on_leaf == "True True", (
        f"the probe cannot see a Ticker that really did load the leaf, so it is not testing "
        f"anything: {on_leaf!r}"
    )
    assert on_leaf != _probe(False)


# ===================================================================== every member


@pytest.mark.parametrize("member", sorted(FRAME_MEMBERS))
def test_a_frame_member_costs_exactly_its_own_route(on, recorder, member):
    """One member, one request, and it is the route that member is wired to."""
    frame = getattr(on, member)()

    assert isinstance(frame, pd.DataFrame)
    assert len(recorder.calls) == 1, f"{member} made {len(recorder.calls)} request(s)"
    assert recorder.needles == [FRAME_MEMBERS[member]]


@pytest.mark.parametrize("member", sorted(DICT_MEMBERS))
def test_a_dict_member_costs_exactly_its_own_route(on, recorder, member):
    tables = getattr(on, member)()

    assert isinstance(tables, dict)
    assert len(recorder.calls) == 1
    assert recorder.needles == [DICT_MEMBERS[member]]


def test_the_reports_frame_is_the_vendors_table_with_its_aggregate_row(on):
    frame = on.trendlyne_reports()

    assert list(frame.columns) == [
        "Summary", "Date", "Stock", "Author", "LTP", "Target",
        "Price at reco (Change since reco%)", "Upside(%)", "Type", "Report",
        "Unnamed: 10", "Discuss",
    ]
    assert frame.attrs["bfinance_trendlyne_table"] == "research_report"
    assert frame.attrs["bfinance_trendlyne_rows"] == len(frame) == 21
    # The vendor's own aggregate row is kept, not filtered -- the member hands over what the page
    # carries. `analyst_price_targets` is what has to do something about that row.
    assert frame.iloc[0]["Author"] == "Consensus Share Price Target"


def test_the_reports_frame_is_not_another_sections_table(on):
    """The control on the frame test above: no other frame member may satisfy it, or the shape
    assertion would pass for a member wired to the wrong route."""
    reports, deals = on.trendlyne_reports(), on.trendlyne_deals()

    assert set(reports.columns) != set(deals.columns)
    assert "Target" not in deals.columns
    assert deals.attrs["bfinance_trendlyne_table"] == "deals"


def test_the_shareholding_frame_is_the_snapshot_grid(on):
    frame = on.trendlyne_shareholding()

    # Twelve labelled rows (five top-level categories plus the sub-rows the page hides behind a
    # click, which `read_html(displayed_only=False)` keeps) across thirteen quarter columns.
    assert list(frame.columns)[0] == "Row Type"
    assert list(frame.columns)[1] == "Jun 2026"
    assert frame.shape == (12, 13)
    # The one section route that cannot be walked backwards; no other member's frame carries it,
    # so a member wired elsewhere fails here.
    assert frame.attrs["bfinance_trendlyne_snapshot_only"] is True
    assert on.trendlyne_deals().attrs["bfinance_trendlyne_snapshot_only"] is False


def test_the_deals_frame_is_the_vendor_deals_table(on):
    frame = on.trendlyne_deals()

    assert frame.attrs["bfinance_trendlyne_table"] == "deals"
    assert {"Client Name", "Deal Type", "Quantity"} <= set(frame.columns)
    assert len(frame) == 39


def test_the_technicals_frame_is_the_tidy_signal_table(on):
    frame = on.trendlyne_technicals()

    assert list(frame.columns) == ["name", "value", "insight", "color", "parameter"]
    assert frame.iloc[0]["name"] == "5 Day"
    assert frame.attrs["bfinance_trendlyne_parameter_keys"] > frame["parameter"].nunique(), (
        "the frame must publish which payload keys it does not speak for"
    )


def test_the_buy_sell_zone_returns_both_bases_keyed_by_pane(on):
    grids = on.trendlyne_buy_sell_zone()

    assert set(grids) == {"standalone", "consolidated"}
    # Byte-identical headers, different numbers: this is the only thing that catches a swap.
    assert list(grids["standalone"].columns) == list(grids["consolidated"].columns)
    assert grids["standalone"].attrs["bfinance_trendlyne_current_pe"] == 14.9
    assert grids["consolidated"].attrs["bfinance_trendlyne_current_pe"] == 15.1


def test_the_swot_returns_five_tables_keyed_by_pane(on):
    tables = on.trendlyne_swot()

    assert list(tables) == [
        "tag_strengths", "tag_weakness", "tag_opportunity", "tag_threats", "tag_others",
    ]
    assert [len(frame) for frame in tables.values()] == [19, 6, 3, 3, 3]
    assert tables["tag_strengths"].attrs["bfinance_trendlyne_swot_heading"] == "Companies with no Debt"


def test_a_paginated_member_forwards_the_page_and_costs_one_request(on, recorder):
    on.trendlyne_news(page=2)
    on.trendlyne_reports(page=2)

    assert recorder.needles == ["/latest-news/", "/research-reports/stock/"]
    assert recorder.paths == [
        f"{tl.build_path('latest_news_page', RECORD)}?page=2",
        f"{tl.build_path('research_reports_page', RECORD)}?page=2",
    ]


def test_page_one_is_the_bare_page(on, recorder):
    """The first page is the page's own canonical URL; sending ``?page=1`` would be shaping a
    request for no reason. Built from the template, so a template change fails here instead of
    being agreed with by a hardcoded string on both sides."""
    on.trendlyne_news()

    assert recorder.paths == [tl.build_path("latest_news_page", RECORD)]


def test_a_bad_page_is_a_caller_bug_and_is_not_recorded_as_a_degraded_vendor(on, recorder):
    """``except (ValueError, KeyError)`` is how a misspelled argument becomes "the vendor is
    degraded". Only the package's typed errors degrade; a caller's ``page=0`` propagates and
    leaves ``data_warnings`` empty."""
    with pytest.raises(ValueError, match="page"):
        on.trendlyne_reports(page=0)

    assert recorder.calls == [], "the refusal is local; no request was spent being told"
    assert on.data_warnings == (), f"a caller bug was recorded as a degradation: {on.data_warnings}"


# ==================================================================== the news shape


def _same_shape(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """Do two news items carry the same two levels of keys?"""
    if set(left) != set(right):
        return False
    return set(left["content"]) == set(right["content"])


def test_the_news_shape_is_the_one_build_news_already_ships(on):
    """The anchor is :meth:`CorporateActionsEngine.build_news`, the engine behind
    :attr:`Ticker.news` -- reachable offline, unlike the two fully ``live``-marked files that name
    this shape but contribute zero tests under ``-m "not live"``."""
    items = on.trendlyne_news()
    anchor = CorporateActionsEngine.build_news([{"title": "a title", "url": "https://x.invalid/"}])

    assert len(items) == 10, "the recorded page carries ten cards"
    assert anchor, "the anchor produced nothing, so it is comparing against nothing"
    for item in items:
        assert set(item) == {"id", "content"}
        assert _same_shape(item, anchor[0])


def test_the_shape_comparison_can_go_red():
    """The control on the comparison above: a helper that answered ``True`` for anything would
    make it vacuous, so it is run against four deliberately wrong items."""
    anchor = CorporateActionsEngine.build_news([{"title": "t", "url": "u"}])[0]

    assert not _same_shape({"id": "x", "content": {}}, anchor), "a missing content key passed"
    assert not _same_shape({"id": "x"}, anchor), "a missing top-level key passed"
    assert not _same_shape({**anchor, "extra": 1}, anchor), "an extra top-level key passed"
    assert not _same_shape(
        {"id": "x", "content": {**anchor["content"], "nope": 1}}, anchor
    ), "an extra content key passed"
    assert _same_shape(anchor, anchor)


def test_the_news_items_carry_the_vendors_own_values(on):
    content = on.trendlyne_news()[0]["content"]

    assert on.trendlyne_news()[0]["id"] == "bfinance-trendlyne-p1-0"
    assert content["title"] == "IT stocks surge despite US immigration curbs"
    assert content["link"].startswith("https://economictimes.indiatimes.com/")
    assert content["canonicalUrl"] == {"url": content["link"]}
    assert content["publisher"] == {"displayName": "Economic Times"}
    assert content["provider"] == content["publisher"]
    # The vendor renders three different date strings on one page, so an epoch would be a guess
    # about which one parsed. The string it did render is published verbatim.
    assert content["time"] is None
    assert content["pubDate"] == "10 Oct, 2026 at 09:49 AM"


def test_the_news_ids_are_distinguishable_across_pages(on):
    """A positional id collides between two pages of one list; the page travels with it, so a
    caller holding page 1 and page 2 can tell the two apart."""
    assert on.trendlyne_news()[0]["id"] != on.trendlyne_news(page=2)[0]["id"]


def test_the_news_values_are_not_a_copy_of_the_anchor(on):
    """The control on the shape agreement: agreeing on keys is the contract, and agreeing on
    *everything* would mean the anchor had been cloned rather than matched."""
    item = on.trendlyne_news()[0]["content"]
    anchor = CorporateActionsEngine.build_news([{"title": "t", "url": "u"}])[0]["content"]

    assert item["publisher"] != anchor["publisher"]
    assert item["pubDate"] is not None and anchor["pubDate"] is None
    assert item["title"] != anchor["title"]


def test_the_paywalled_href_is_reported_and_not_rewritten(on):
    """Research-report cards link a login modal for anonymous readers. The vendor's own href is
    the real answer; the page behind the wall would be an invented address."""
    urls = [item["content"]["link"] for item in on.trendlyne_news()]

    assert any("loginmodal" in url for url in urls), urls


# ============================================================== analyst_price_targets


def test_the_targets_are_none_with_the_flag_off(off, recorder):
    """Unchanged behaviour, and it costs nothing: the property never reaches a client."""
    assert off.analyst_price_targets is None
    assert recorder.calls == []
    assert off.data_warnings == ()


def test_the_targets_are_derived_from_the_reports_target_column(on, recorder):
    targets = on.analyst_price_targets

    assert set(targets) == {
        "current", "low", "high", "mean", "median", "n_reports", "bfinance_analyst_source",
    }
    assert recorder.needles == ["/research-reports/stock/"], (
        f"the targets came from somewhere other than the reports table: {recorder.paths}"
    )
    # Twenty per-report rows on the recorded TCS page.
    assert targets["low"] == 1860.0
    assert targets["high"] == 3600.0
    assert targets["mean"] == pytest.approx(2690.05)
    assert targets["median"] == 2525.0
    assert targets["n_reports"] == 20
    assert targets["bfinance_analyst_source"] == "trendlyne_research_reports"
    assert on.data_warnings == (), "a served answer is not a degradation"


def test_the_vendors_aggregate_row_is_excluded_from_every_statistic(on):
    """The page's first row **is** the consensus -- an average of the rows beneath it. Averaging
    the column without excluding it folds the consensus into the mean, so the two numbers that
    ought to agree do not. Both are computed here, so the exclusion is load-bearing rather than
    asserted by comment."""
    targets = on.analyst_price_targets
    every_row = pd.to_numeric(on.trendlyne_reports()["Target"], errors="coerce").dropna()

    assert len(every_row) == 21
    assert targets["n_reports"] == len(every_row) - 1
    assert targets["mean"] != pytest.approx(float(every_row.mean())), (
        "excluding the aggregate row changed nothing, so the filter is not firing"
    )
    assert targets["mean"] == pytest.approx(float(every_row.iloc[1:].mean()))


def test_the_current_price_is_never_published_under_its_own_name(on):
    """The route carries an ``LTP`` column, and it is the price at the moment each report was
    published. Publishing it as ``current`` is the same relabelling the deleted multiplier-based
    synthesiser was deleted for, so the key is ``None`` and the value is not guessed."""
    targets = on.analyst_price_targets

    assert targets["current"] is None
    assert "LTP" in on.trendlyne_reports().columns, (
        "the frame must still carry the column the decision was made about"
    )


def test_no_targets_are_derived_from_an_empty_page(on, recorder):
    """A page that carries the header and no rows answers with a real, empty frame. There is
    nothing to publish, so ``None`` -- and the reason is written down, because a caller holding
    ``None`` must be able to tell "no analyst published a target" from "the fetch failed"."""
    header = (
        "<table><thead><tr>"
        + "".join(
            f"<th>{cell}</th>"
            for cell in (
                "Summary", "Date", "Stock", "Author", "LTP", "Target",
                "Price at reco (Change since reco%)", "Upside(%)", "Type", "Report", "", "Discuss",
            )
        )
        + "</tr></thead><tbody></tbody></table>"
    )
    recorder.overrides["/research-reports/stock/"] = httpx.Response(200, text=header)

    assert on.analyst_price_targets is None
    assert any("analyst price targets" in w for w in on.data_warnings), on.data_warnings


def test_a_failed_reports_fetch_is_recorded_once_and_still_returns_none(on, recorder):
    """The fetch failure is recorded by the one call that made it. A second warning naming the
    same cause would read as two failures, and the contract is "record what did not happen" --
    not "record it twice"."""
    recorder.overrides["/research-reports/stock/"] = httpx.Response(500, text="server error")

    assert on.analyst_price_targets is None
    research = [w for w in on.data_warnings if "research reports" in w]
    assert len(research) == 1, on.data_warnings
    assert "TrendlyneServerError" in research[0]


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame(),
        pd.DataFrame({"Author": ["a"], "Target": ["n/a"]}),
        pd.DataFrame({"Author": ["a"], "Target": [float("nan")]}),
        pd.DataFrame({"Author": ["a"], "Target": [float("inf")]}),
        pd.DataFrame({"Summary": ["x"], "Author": ["a"]}),
    ],
    ids=["no-rows", "not-a-number", "nan", "infinite", "no-target-column"],
)
def test_the_derivation_returns_none_rather_than_a_number(frame):
    """The pure function, over the five ways a frame can carry no target. None of them may
    produce a figure, because every figure on this page came out of a real Target cell."""
    assert _targets_from_reports(frame) is None


def test_the_derivation_is_a_pure_function_of_the_frame():
    """No fetch and no Ticker: the same frame twice gives the same answer, and a frame whose
    only row is the aggregate gives ``None`` rather than publishing the consensus as though it
    were a per-report target."""
    frame = pd.DataFrame({
        "Author": ["Consensus Share Price Target", "Broker A"],
        "Target": [2445.54, 2360.00],
    })

    assert _targets_from_reports(frame) == {
        "current": None,
        "low": 2360.0,
        "high": 2360.0,
        "mean": 2360.0,
        "median": 2360.0,
        "n_reports": 1,
        "bfinance_analyst_source": "trendlyne_research_reports",
    }
    assert _targets_from_reports(frame) == _targets_from_reports(frame)
    assert _targets_from_reports(frame.assign(Target=[2445.54, None])) is None


# ============================================== the two ways a target was being lost (m3)


def _reports_html(*rows: Tuple[str, str]) -> str:
    """A research-reports page carrying exactly ``(Author, Target)`` rows.

    The header is the recorded page's own, so the frame it produces classifies as
    ``research_report`` -- a stub with a different header would be refused by
    :func:`bfinance.trendlyne.tables.find_table` rather than parsed, and the test would prove
    nothing about the parser.
    """
    header = (
        "Summary", "Date", "Stock", "Author", "LTP", "Target",
        "Price at reco (Change since reco%)", "Upside(%)", "Type", "Report", "", "Discuss",
    )
    body = "".join(
        f"<tr><td></td><td>01 Oct 2026</td><td>TCS</td><td>{author}</td><td>2100</td>"
        f"<td>{target}</td><td>2100 (0)</td><td>5</td><td>Buy</td><td>report</td>"
        f"<td></td><td></td></tr>"
        for author, target in rows
    )
    return (
        "<table><thead><tr>" + "".join(f"<th>{cell}</th>" for cell in header)
        + "</tr></thead><tbody>" + body + "</tbody></table>"
    )


class TestAThousandsSeparatorIsATargetNotABlank:
    """``"2,450.54"`` is the number ``2455.54``, written the way the vendor writes it.

    ``pd.to_numeric(..., errors="coerce")`` reads it as ``NaN``, and a ``NaN`` is dropped -- so one
    formatted cell removed a real broker's target from ``low``, ``high``, ``mean``, ``median`` and
    ``n_reports`` at once, with nothing to say so. The three real targets below became two.
    """

    def test_all_three_targets_survive_the_separator(self, on, recorder):
        recorder.overrides["/research-reports/stock/"] = httpx.Response(
            200,
            text=_reports_html(
                ("Consensus Share Price Target", "2,450.54"),
                ("Broker A", "2,000.00"),
                ("Broker B", "3,000"),
                ("Broker C", "1,500.5"),
            ),
        )

        targets = on.analyst_price_targets

        assert targets["n_reports"] == 3, (
            "a target written with a thousands separator was dropped: n_reports counts what "
            "survived the coercion, and the aggregate is excluded by the Author column, not by "
            "the parse"
        )
        assert targets["low"] == 1500.5
        assert targets["high"] == 3000.0
        assert targets["mean"] == pytest.approx((2000.0 + 3000.0 + 1500.5) / 3)

    def test_the_aggregate_row_is_still_excluded_when_it_carries_a_separator(self, on, recorder):
        """The separator is repaired **before** the aggregate is excluded, so a formatted
        consensus cannot slip through the exclusion by failing to parse."""
        recorder.overrides["/research-reports/stock/"] = httpx.Response(
            200,
            text=_reports_html(
                ("Consensus Share Price Target", "2,450.54"),
                ("Broker A", "2,000.00"),
                ("Broker B", "2,100.00"),
            ),
        )

        targets = on.analyst_price_targets

        assert targets["n_reports"] == 2
        assert targets["mean"] == pytest.approx(2050.0)

    def test_control_the_separator_is_what_was_dropped_before(self):
        """The control, run against the coercion this replaced, so the test above is anchored to
        a real defect rather than to a shape someone imagined."""
        targets = pd.Series(["2,450.54", "2000.00", "3000"])

        without = pd.to_numeric(targets, errors="coerce")

        assert pd.isna(without.iloc[0]), (
            f"'2,450.54' parsed cleanly on this pandas build, so the control no longer shows the "
            f"defect: {without.tolist()}"
        )
        assert without.dropna().tolist() == [2000.0, 3000.0], (
            "the other two must parse, or this control is not isolating the separator"
        )


class TestARenamedAuthorColumnWithholdsTheStatistics:
    """Without ``Author`` the vendor's aggregate row cannot be told from a per-report one.

    The alternative was to compute the statistics anyway and hope the aggregate was not there. That
    is the defect this module is written against: a mean that folds the consensus into the mean of
    the rows beneath it is wrong by exactly one value and looks entirely plausible. So the absence
    **degrades** -- ``None`` plus a ``data_warnings`` entry naming the cause.
    """

    #: The recorded page's header with ``Author`` renamed, which is what an upstream change would
    #: look like from here.
    RENAMED_HTML = (
        "<table><thead><tr>"
        + "".join(
            f"<th>{cell}</th>"
            for cell in (
                "Summary", "Date", "Stock", "Broker", "LTP", "Target",
                "Price at reco (Change since reco%)", "Upside(%)", "Type", "Report", "", "Discuss",
            )
        )
        + "</tr></thead><tbody>"
          "<tr><td></td><td>01 Oct 2026</td><td>TCS</td>"
          "<td>Consensus Share Price Target</td><td>2100</td><td>2445.54</td>"
          "<td>2100 (0)</td><td>13</td><td>Buy</td><td>report</td><td></td><td></td></tr>"
          "<tr><td></td><td>01 Oct 2026</td><td>TCS</td><td>Broker A</td><td>2100</td>"
          "<td>2360</td><td>2100 (0)</td><td>12</td><td>Buy</td><td>report</td>"
          "<td></td><td></td></tr>"
        + "</tbody></table>"
    )

    def test_the_derivation_returns_none_rather_than_a_mean_that_includes_the_consensus(self):
        """The pure function: the frame still has every target, and the function still declines."""
        frame = pd.DataFrame({
            "Broker": ["Consensus Share Price Target", "Broker A"],
            "Target": [2445.54, 2360.00],
        })

        assert _targets_from_reports(frame) is None

    def test_the_property_returns_none_and_records_why(self, on, recorder):
        """Through the public property, because the reason is only observable there: the
        derivation is a pure function and has no Ticker to record a warning on."""
        recorder.overrides["/research-reports/stock/"] = httpx.Response(
            200, text=self.RENAMED_HTML
        )

        assert on.analyst_price_targets is None
        assert any("Broker" in w or "Author" in w for w in on.data_warnings), on.data_warnings
        assert any("author" in w.lower() for w in on.data_warnings), (
            f"the warning has to name the missing column: {on.data_warnings}"
        )

    def test_the_warning_does_not_claim_there_were_no_targets(self, on, recorder):
        """The distinction ``data_warnings`` exists for. "Carries no Target value" would be a
        false statement about a page full of them."""
        recorder.overrides["/research-reports/stock/"] = httpx.Response(
            200, text=self.RENAMED_HTML
        )

        on.analyst_price_targets

        assert any(
            "no Target value" not in w for w in on.data_warnings
        ), on.data_warnings
        assert any("cannot be told apart" in w for w in on.data_warnings), on.data_warnings

    def test_control_the_same_page_with_its_author_column_still_answers(self, recorder, seam):
        """Without the control, "returns None" could mean the page was never parsed at all."""
        recorder.overrides["/research-reports/stock/"] = httpx.Response(
            200,
            text=_reports_html(
                ("Consensus Share Price Target", "2445.54"),
                ("Broker A", "2360.00"),
            ),
        )
        ticker = Ticker(SYMBOL, trendlyne_sections=True, cache_ttl_hours=0)

        targets = ticker.analyst_price_targets

        assert targets is not None
        assert targets["n_reports"] == 1
        assert targets["low"] == 2360.0
        assert ticker.data_warnings == ()


# ======================================================================= degradation


@pytest.mark.parametrize("member", sorted(FRAME_MEMBERS))
def test_a_failing_frame_member_degrades_instead_of_returning_an_empty_frame(
    on, recorder, member
):
    """The rule this class is organised around: a gap is *recorded*, never turned into a quiet
    zero. The transport really was asked -- asserted on ``recorder.calls`` -- so the empty frame
    cannot be confused with a page that genuinely carried no rows, which is the one case where an
    empty frame is the right answer."""
    recorder.overrides["trendlyne.com"] = httpx.Response(500, text="server error")

    frame = getattr(on, member)()

    assert recorder.calls, "nothing was requested, so nothing failed"
    assert frame.empty, "a failed fetch produced rows"
    assert any("TrendlyneServerError" in w for w in on.data_warnings), (
        f"{member} failed and recorded nothing: {on.data_warnings}"
    )


@pytest.mark.parametrize("member", sorted(DICT_MEMBERS))
def test_a_failing_dict_member_degrades_into_an_empty_dict(on, recorder, member):
    recorder.overrides["trendlyne.com"] = httpx.Response(500, text="server error")

    assert getattr(on, member)() == {}
    assert any("TrendlyneServerError" in w for w in on.data_warnings), on.data_warnings


def test_a_failing_news_member_degrades_into_an_empty_list(on, recorder):
    recorder.overrides["trendlyne.com"] = httpx.Response(500, text="server error")

    assert on.trendlyne_news() == []
    assert any("trendlyne news" in w for w in on.data_warnings), on.data_warnings


def test_a_member_whose_route_answers_404_degrades_rather_than_guessing(on, recorder):
    """The recorder answers **404** for a route nobody registered, and the member reports that
    rather than handing back an empty frame that would read as "this company has no rows"."""
    recorder.overrides["/equity/share-holding/"] = httpx.Response(404, text="gone")

    assert on.trendlyne_shareholding().empty
    assert any("TrendlyneNotFoundError" in w for w in on.data_warnings), on.data_warnings

    # And the control, on the same member and the same transport: with the recorded page served
    # instead of a 404, the answer is a populated frame. Without it, an empty frame and a
    # failure are the same observation.
    del recorder.overrides["/equity/share-holding/"]
    served = on.trendlyne_shareholding()

    assert served.attrs["bfinance_trendlyne_rows"] == len(served) == 12


def test_a_served_section_records_no_warning_of_its_own(on):
    """The other half of the pair. ``_degrade`` says "what is **unavailable**", and a complete
    frame is not that -- reporting every healthy call there would tell a caller to distrust a
    frame that was in fact complete."""
    for member in ALL_MEMBERS:
        getattr(on, member)()

    assert on.data_warnings == (), on.data_warnings


def test_raise_errors_propagates_the_vendor_failure_instead_of_degrading(
    monkeypatch, recorder
):
    """Same contract as ``_read_statement``: a typed upstream failure raises for a caller who
    asked for exceptions and is recorded for one who did not."""
    _install_bridge(monkeypatch, recorder)
    recorder.overrides["trendlyne.com"] = httpx.Response(500, text="server error")
    ticker = Ticker(SYMBOL, trendlyne_sections=True, cache_ttl_hours=0, raise_errors=True)

    with pytest.raises(BFinanceError):
        ticker.trendlyne_deals()
    assert ticker.data_warnings == (), "a raising call also recorded a degradation"
