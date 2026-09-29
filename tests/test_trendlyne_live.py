"""
Live tests for the Trendlyne path, which had none at all.

Every other networked source in this package has live coverage; Trendlyne had 18 files' worth
of markers elsewhere and zero here, so the ETF fallback and the fuzzy-resolution rule were
only ever verified by hand in a scratch script that was then thrown away. That is not
something you can deploy against.

Run with::

    uv run pytest tests/test_trendlyne_live.py -m live -p no:cacheprovider -q

They are excluded from the required CI job by ``-m "not live"``, and they are slow: each
instrument costs two or three requests at the site's ``Crawl-delay: 1``, so the file is a
deliberate, occasional run rather than something to put on every commit.

ROBOTS NOTE, AND IT MATTERS FOR WHO RUNS THIS
--------------------------------------------
The gate-off tests are ordinary live tests and touch only allowed paths. **The gate-on tests
request ``/member/api/ac_snames/``, which trendlyne's robots.txt disallows** under the broad
``Disallow: */member/`` rule. The package keeps that behind ``allow_member_search=True`` and
off by default; these tests open it deliberately, because the behaviour being verified is
unreachable otherwise. If your use does not authorise that request, run this file with
``-m "live and not etf"`` and the gate-on tests will be skipped rather than silently passing.

Pacing is the module's own 1000 ms floor, which is the site's published ``Crawl-delay: 1``.
Nothing here adds a sleep of its own, so this file cannot outrun the limit even if edited.
"""
from __future__ import annotations

import numpy as np
import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne.errors import (
    TrendlyneRobotsDisallowedError,
    TrendlyneSymbolNotFoundError,
)

pytestmark = pytest.mark.live

#: Instruments a user is likely to type, with the ticker each must actually resolve to. The
#: second column is the point: "nifty bees" returned BANKBEES before the matching rule, and
#: a test that only checked "some bars came back" would have passed while handing back the
#: wrong security.
TYPED_TO_TICKER = [
    ("niftybees", "NIFTYBEES"),
    ("nifty bees", "NIFTYBEES"),
    ("NIFTYBEES", "NIFTYBEES"),
    ("goldbees", "GOLDBEES"),
    ("juniorbees", "JUNIORBEES"),
    ("silverbees", "SILVERBEES"),
    ("selectipo", "SELECTIPO"),
]

ETFS = ["NIFTYBEES", "BANKBEES", "GOLDBEES", "JUNIORBEES", "SILVERBEES", "SELECTIPO"]


def open_gate() -> tl.TrendlyneClient:
    return tl.TrendlyneClient(allow_member_search=True)


# ------------------------------------------------------- allowed paths, no gate


class TestSitemapPathLive:
    """Ordinary equities, resolved from the allowed sitemap, with the gate shut."""

    @pytest.mark.parametrize("symbol", ["RELIANCE", "TCS", "INFY", "SBIN"])
    def test_an_equity_resolves_and_returns_bars(self, symbol):
        client = tl.TrendlyneClient()          # gate off, the shipped default
        record = tl._run_async(client.resolve_any_async(symbol))
        assert record.symbol == symbol
        assert client.symbol_resolution == "sitemap"

        frame = client.ohlc(symbol)
        assert len(frame) > 1000, f"{symbol}: only {len(frame)} bars"
        assert frame.attrs["bfinance_trendlyne_symbol_resolution"] == "sitemap"
        assert frame.attrs["bfinance_provenance"] == "trendlyne"

    def test_the_sitemap_is_loaded_and_large(self):
        client = tl.TrendlyneClient()
        index = client.symbol_index()
        assert len(index) > 5000, f"sitemap parsed to only {len(index)} entries"

    @pytest.mark.parametrize("ticker", ETFS)
    def test_no_etf_is_in_the_sitemap(self, ticker):
        """
        The premise of the whole gate. If an ETF ever appears in the sitemap, the gate stops
        being the only route to it and the coverage story in the docs needs rewriting.
        """
        index = tl.TrendlyneClient().symbol_index()
        assert ticker not in index, (
            f"{ticker} is now in the sitemap, so it is reachable with the gate off; "
            f"the documented consequence no longer holds"
        )


# ---------------------------------------------- the gate, on allowed paths only


class TestTheGateDefaultLive:
    def test_the_gate_is_off_by_default(self):
        assert tl.TrendlyneClient().allow_member_search is False

    def test_an_etf_is_refused_with_the_gate_shut(self):
        client = tl.TrendlyneClient()
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            client.ohlc("NIFTYBEES")
        message = str(excinfo.value)
        assert "disallowed" in message
        assert "robots.txt" in message
        assert client.symbol_resolution == "skipped_robots_disallowed"

    def test_the_refusal_names_the_switch_that_would_open_it(self):
        """A caller blocked by policy needs to be told the way to unblock, and the way is named."""
        client = tl.TrendlyneClient()
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            client.ohlc("NIFTYBEES")
        assert tl.MEMBER_SEARCH_FLAG in str(excinfo.value)

    def test_search_remote_raises_the_policy_error_itself(self):
        with pytest.raises(TrendlyneRobotsDisallowedError) as excinfo:
            tl.TrendlyneClient().search_remote("NIFTYBEES")
        message = str(excinfo.value)
        assert "Disallow: */member/" in message
        assert tl.MEMBER_SEARCH_FLAG in message


# -------------------------------------------- gate ON: requests a Disallowed route


@pytest.mark.etf
class TestEtfPathLive:
    """
    These request /member/api/ac_snames/, which robots.txt disallows. See the module note.

    Skipped, not silently passed, when run without the ``etf`` marker, so a deployment that
    does not authorise the route gets an honest skip rather than a green run that never
    exercised the thing being deployed.
    """

    @pytest.mark.parametrize("typed, ticker", TYPED_TO_TICKER)
    def test_a_typed_name_resolves_to_the_right_instrument(self, typed, ticker):
        """
        The regression, live. Before the matching rule, "nifty bees" resolved to BANKBEES
        because that is the vendor's top-ranked hit for a fuzzy query.
        """
        client = open_gate()
        record = tl._run_async(client.resolve_any_async(typed))
        assert record.symbol == ticker, (
            f"{typed!r} resolved to {record.symbol}, not {ticker}; a frame for the wrong "
            f"security is indistinguishable from one for the right"
        )
        assert client.symbol_resolution == "member_search"

    @pytest.mark.parametrize("ticker", ETFS)
    def test_an_etf_returns_a_usable_series(self, ticker):
        client = open_gate()
        frame = client.ohlc(ticker)
        assert len(frame) > 100, f"{ticker}: only {len(frame)} bars"
        assert frame.attrs["bfinance_symbol"] == ticker
        assert frame.attrs["bfinance_trendlyne_symbol_resolution"] == "member_search"
        assert frame.attrs["bfinance_trendlyne_demerger_applied"] is False
        assert not frame.empty and frame["Close"].notna().all()

    def test_an_ambiguous_prefix_refuses_rather_than_guessing(self):
        """Live confirmation that a bare prefix does not silently become one instrument."""
        client = open_gate()
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            tl._run_async(client.resolve_any_async("niftybeesxyz"))
        assert "resolved by neither route" in str(excinfo.value) or "NONE is" in str(excinfo.value)

    def test_a_misspelling_fails_rather_than_resolving_to_something_else(self):
        client = open_gate()
        with pytest.raises(TrendlyneSymbolNotFoundError):
            tl._run_async(client.resolve_any_async("juniourbees"))


# ------------------------------------------------------------------ data integrity


class TestLiveSeriesIntegrity:
    """
    Recomputed here rather than read from attrs, because the attrs are the code's own claim
    about itself and this is the check on that claim.
    """

    @pytest.mark.etf
    @pytest.mark.parametrize("ticker", ETFS)
    def test_an_etf_series_is_internally_consistent(self, ticker):
        frame = open_gate().ohlc(ticker)
        o = frame["Open"].to_numpy(float)
        h = frame["High"].to_numpy(float)
        low = frame["Low"].to_numpy(float)
        c = frame["Close"].to_numpy(float)
        vol = frame["Volume"].to_numpy(float)
        n = len(frame)

        assert int((h >= np.maximum(o, c) - 1e-9).sum()) == n, "High below Open/Close on some bar"
        assert int((low <= np.minimum(o, c) + 1e-9).sum()) == n, "Low above Open/Close on some bar"
        assert int((h >= low - 1e-9).sum()) == n, "High below Low on some bar"
        assert int((vol >= 0).sum()) == n, "negative volume"
        assert int((c > 0).sum()) == n, "non-positive close"
        assert int(frame.isna().sum().sum()) == 0, "NaN in the frame"
        assert frame.index.is_unique and frame.index.is_monotonic_increasing

    @pytest.mark.parametrize("symbol", ["RELIANCE", "SBIN"])
    def test_an_equity_series_is_internally_consistent(self, symbol):
        frame = tl.TrendlyneClient().ohlc(symbol)
        o, h = frame["Open"].to_numpy(float), frame["High"].to_numpy(float)
        low, c = frame["Low"].to_numpy(float), frame["Close"].to_numpy(float)
        n = len(frame)
        assert int((h >= np.maximum(o, c) - 1e-9).sum()) == n
        assert int((low <= np.minimum(o, c) + 1e-9).sum()) == n
        assert int(frame.isna().sum().sum()) == 0
        assert frame.index.is_unique

    def test_the_synthetic_ohlc_flag_is_never_set_on_this_source(self):
        """
        This source publishes real O/H/L. If the flag ever reads True, the frame was
        fabricated somewhere upstream and every other check here is beside the point.
        """
        frame = tl.TrendlyneClient().ohlc("RELIANCE")
        assert frame.attrs["bfinance_synthetic_ohlc"] is False
